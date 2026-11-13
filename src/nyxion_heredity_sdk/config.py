"""A deliberately specified JSON Schema subset, without third-party imports."""

from types import MappingProxyType

from .contracts import freeze_json
from .errors import ContractError


_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_ANNOTATIONS = frozenset({
    "$schema", "title", "description", "default", "examples", "writeOnly",
    "readOnly", "deprecated",
})
_KEYWORDS = _ANNOTATIONS | {
    "type", "properties", "required", "additionalProperties", "enum",
    "minimum", "maximum", "minLength", "maxLength", "items", "minItems", "maxItems",
}


def _json_equal(left, right):
    # JSON booleans are not numbers; Python's True == 1 must not admit a
    # configuration value excluded by its declared enum.
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if type(left) is MappingProxyType:
        return set(left) == set(right) and all(_json_equal(left[key], right[key]) for key in left)
    if type(left) is tuple:
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    return left == right


def validate_config_schema(schema):
    """Reject unsupported validation keywords instead of silently ignoring them."""
    schema = freeze_json(schema)

    def check(node, depth=0):
        if type(node) not in (dict, MappingProxyType) or depth > 16:
            raise ContractError("configuration schema must contain bounded schema objects")
        if set(node) - _KEYWORDS:
            raise ContractError("configuration schema uses unsupported keywords")
        kind = node.get("type")
        if not isinstance(kind, str) or kind not in _TYPES:
            raise ContractError("each configuration schema must declare one supported type")
        for annotation in ("$schema", "title", "description"):
            if annotation in node and not isinstance(node[annotation], str):
                raise ContractError("configuration schema annotation is invalid")
        for annotation in ("writeOnly", "readOnly", "deprecated"):
            if annotation in node and type(node[annotation]) is not bool:
                raise ContractError("configuration schema annotation must be boolean")
        if "enum" in node and (type(node["enum"]) is not tuple or not node["enum"]):
            raise ContractError("configuration enum must be a nonempty array")
        for low, high in (("minimum", "maximum"), ("minLength", "maxLength"),
                          ("minItems", "maxItems")):
            for key in (low, high):
                if key in node:
                    allowed = type(node[key]) in (int, float) if low == "minimum" else type(node[key]) is int
                    if not allowed or (low != "minimum" and node[key] < 0):
                        raise ContractError("configuration schema bound is invalid")
            if low in node and high in node and node[low] > node[high]:
                raise ContractError("configuration schema bounds are reversed")
        if any(key in node for key in ("properties", "required", "additionalProperties")):
            if kind != "object":
                raise ContractError("object schema keywords require type object")
        if any(key in node for key in ("minItems", "maxItems", "items")) and kind != "array":
            raise ContractError("array schema keywords require type array")
        if any(key in node for key in ("minLength", "maxLength")) and kind != "string":
            raise ContractError("string schema keywords require type string")
        if any(key in node for key in ("minimum", "maximum")) and kind not in {"integer", "number"}:
            raise ContractError("numeric schema keywords require a numeric type")
        properties = node.get("properties", {})
        if type(properties) not in (dict, MappingProxyType):
            raise ContractError("configuration properties must be an object")
        required = node.get("required", ())
        if (type(required) is not tuple or any(type(key) is not str for key in required)
                or len(required) != len(set(required)) or set(required) - set(properties)):
            raise ContractError("configuration required names must declare unique properties")
        if "additionalProperties" in node and type(node["additionalProperties"]) is not bool:
            raise ContractError("additionalProperties must be boolean")
        for child in properties.values():
            check(child, depth + 1)
        if "items" in node:
            check(node["items"], depth + 1)

    check(schema)
    return schema


def validate_config(manifest_or_schema, config):
    """Validate values without mutating them or applying schema defaults."""
    schema = getattr(manifest_or_schema, "config_schema", manifest_or_schema)
    schema = validate_config_schema(schema)
    value = freeze_json(config)
    if type(value) is not MappingProxyType:
        raise ContractError("plugin configuration must be a JSON object")

    def inspect(node, item):
        kind = node["type"]
        matches = {
            "object": type(item) is MappingProxyType,
            "array": type(item) is tuple,
            "string": type(item) is str,
            "integer": type(item) is int,
            "number": type(item) in (int, float),
            "boolean": type(item) is bool,
            "null": item is None,
        }[kind]
        if not matches:
            raise ContractError("plugin configuration value has an invalid type")
        if "enum" in node and not any(_json_equal(item, candidate) for candidate in node["enum"]):
            raise ContractError("plugin configuration value is outside its enum")
        if kind == "object":
            properties = node.get("properties", {})
            if any(key not in item for key in node.get("required", ())):
                raise ContractError("plugin configuration omitted a required property")
            if node.get("additionalProperties", True) is False and set(item) - set(properties):
                raise ContractError("plugin configuration contains an unknown property")
            for key, child in item.items():
                if key in properties:
                    inspect(properties[key], child)
        elif kind == "array":
            if len(item) < node.get("minItems", 0) or len(item) > node.get("maxItems", len(item)):
                raise ContractError("plugin configuration array length is invalid")
            if "items" in node:
                for child in item:
                    inspect(node["items"], child)
        elif kind == "string":
            if len(item) < node.get("minLength", 0) or len(item) > node.get("maxLength", len(item)):
                raise ContractError("plugin configuration string length is invalid")
        elif kind in {"integer", "number"}:
            if item < node.get("minimum", item) or item > node.get("maximum", item):
                raise ContractError("plugin configuration numeric value is invalid")

    inspect(schema, value)
    return value
