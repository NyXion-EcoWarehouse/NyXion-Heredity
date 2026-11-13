from contextlib import redirect_stdout
from dataclasses import FrozenInstanceError, replace
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from nyxion_heredity_sdk import (
    ActionRequest, ActionResult, BackendError, Challenge, CompatibilityError,
    ContractError, DeadlineExceeded, EventEnvelope, Heartbeat, HostHello,
    Plugin, PluginContext, PluginHello, PluginManifest, Pong, PublishAck,
    SessionIdentity, SessionMismatch, SessionUpdate, freeze_json, require_session,
    thaw_json, validate_action_result, validate_config, validate_config_schema,
    validate_handshake, validate_pong,
)
from nyxion_heredity_sdk.conformance import main as conformance_main, run_conformance
from nyxion_heredity_sdk.example import OfflineExamplePlugin


def manifest(**overrides):
    return PluginManifest(**({
        "plugin_id": "test.adapter", "name": "Test Adapter", "version": "1.0.0",
        "platform": "example", "actions": ("get_login_info", "echo"),
    } | overrides))


def session(**overrides):
    return SessionIdentity(**({
        "platform": "example", "self_id": "10000", "session_epoch": "epoch-1",
    } | overrides))


class ManifestTests(unittest.TestCase):
    def test_exact_round_trip_uses_ordinary_json(self):
        value = manifest()
        encoded = json.dumps(value.to_dict())
        self.assertEqual(PluginManifest.from_dict(json.loads(encoded)), value)

    def test_unknown_missing_and_malformed_fields_are_rejected(self):
        data = manifest().to_dict()
        for change in ({"unknown": True}, {"plugin_id": "../module"},
                       {"execution": "isolated"}, {"api_major": True},
                       {"api_minor": -1}, {"version": "latest"},
                       {"actions": ["echo", "echo"]}, {"actions": "echo"},
                       {"capabilities": ["bad capability"]}, {"schema_version": True},
                       {"host_api_min_minor": 1, "host_api_max_minor": 0},
                       {"platform": "Example"}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                PluginManifest.from_dict(data | change)
        data.pop("name")
        with self.assertRaises(ContractError):
            PluginManifest.from_dict(data)

    def test_minor_compatibility_requires_plugin_needs_and_host_range(self):
        value = manifest(api_minor=2, host_api_min_minor=1, host_api_max_minor=3)
        self.assertFalse(value.supports_host(1, 1))
        self.assertTrue(value.supports_host(1, 2))
        self.assertTrue(value.supports_host(1, 3))
        self.assertFalse(value.supports_host(1, 4))
        self.assertFalse(value.supports_host(2, 2))
        self.assertFalse(value.supports_host(True, 2))

    def test_manifest_is_frozen_and_schema_does_not_alias_caller(self):
        schema = {"type": "object", "properties": {"token": {"type": "string", "writeOnly": True}}}
        value = manifest(config_schema=schema)
        schema["properties"]["token"]["writeOnly"] = False
        self.assertTrue(value.config_schema["properties"]["token"]["writeOnly"])
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            value.name = "changed"
        with self.assertRaises(TypeError):
            value.config_schema["properties"]["token"]["type"] = "integer"

    def test_sdk_import_needs_neither_host_nor_cryptography(self):
        code = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if (fullname == 'heredity' or fullname.startswith('heredity.')
                or fullname.startswith(('cryptography', 'wasmtime'))
                or (fullname.startswith('nyxion_heredity_')
                    and not fullname.startswith('nyxion_heredity_sdk'))):
            raise AssertionError('SDK attempted a host/platform import')
sys.meta_path.insert(0, Block())
import nyxion_heredity_sdk
import nyxion_heredity_sdk.example
import nyxion_heredity_sdk.conformance
from nyxion_heredity_sdk import SandboxModuleManifest
assert SandboxModuleManifest('example.adapter', 'Adapter', '1.0.0', '0.3.0').module == 'module.wasm'
assert not any(name == 'heredity' or name.startswith('heredity.') for name in sys.modules)
"""
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.schema = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 10},
                "count": {"type": "integer", "minimum": 1, "maximum": 3},
                "mode": {"type": "string", "enum": ["a", "b"]},
                "enabled": {"type": "boolean"},
                "ports": {"type": "array", "minItems": 1, "maxItems": 2,
                          "items": {"type": "integer", "minimum": 1}},
                "nested": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            "required": ["name", "count"],
        }

    def test_subset_validates_and_returns_owned_immutable_values(self):
        data = {"name": "test", "count": 2, "ports": [1, 2], "nested": {}}
        result = validate_config(self.schema, data)
        data["ports"].append(3)
        self.assertEqual(result["ports"], (1, 2))
        self.assertEqual(thaw_json(result), {"name": "test", "count": 2, "ports": [1, 2], "nested": {}})

    def test_types_required_unknown_bounds_and_enum(self):
        good = {"name": "n", "count": 2}
        invalid = ({}, good | {"other": 1}, good | {"count": True},
                   good | {"name": ""}, good | {"name": "x" * 11},
                   good | {"count": 4}, good | {"mode": "c"},
                   good | {"enabled": 1}, good | {"ports": []},
                   good | {"ports": [1, 2, 3]}, good | {"ports": [0]},
                   good | {"nested": {"secret": "not-in-error"}})
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ContractError) as error:
                validate_config(self.schema, data)
            self.assertNotIn("not-in-error", str(error.exception))

    def test_schema_unknown_keywords_and_invalid_constraints_fail_closed(self):
        invalid = (
            {"type": "object", "pattern": ".*"},
            {"type": "object", "required": ["missing"]},
            {"type": "string", "minimum": 0},
            {"type": "array", "minItems": -1},
            {"type": "integer", "minimum": 2, "maximum": 1},
            {"type": "object", "additionalProperties": {}},
            {"type": ["string", "null"]},
            {"type": "object", "properties": {"child": {"enum": [1]}}},
        )
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ContractError):
                validate_config_schema(data)

    def test_defaults_are_annotations_not_mutations(self):
        config = {}
        result = validate_config(
            {"type": "object", "properties": {"name": {"type": "string", "default": "x"}}},
            config)
        self.assertEqual(thaw_json(result), {})
        self.assertEqual(config, {})

    def test_enum_does_not_confuse_boolean_with_number(self):
        for kind, value, candidates in (("boolean", True, [1]),
                                        ("integer", 1, [True])):
            schema = {"type": "object", "properties": {
                "value": {"type": kind, "enum": candidates}}}
            with self.subTest(kind=kind), self.assertRaises(ContractError):
                validate_config(schema, {"value": value})
        accepted = validate_config({"type": "object", "properties": {
            "value": {"type": "number", "enum": [1.0]}}}, {"value": 1})
        self.assertEqual(accepted["value"], 1)


class PayloadTests(unittest.TestCase):
    def test_deep_freeze_prevents_aliasing_without_json_encoding(self):
        data = {"list": [{"name": "private-body"}]}
        with patch("json.dumps", side_effect=AssertionError("serialization")):
            request = ActionRequest("r1", session(), "echo", data, time.monotonic() + 1)
        data["list"][0]["name"] = "changed"
        self.assertEqual(request.params["list"][0]["name"], "private-body")
        self.assertNotIn("private-body", repr(request))
        with self.assertRaises(TypeError):
            request.params["list"][0]["name"] = "mutated"
        mutable = thaw_json(request.params)
        mutable["list"][0]["name"] = "new"
        self.assertEqual(request.params["list"][0]["name"], "private-body")

    def test_cycles_depth_non_json_and_nonfinite_are_rejected(self):
        cycle = {}
        cycle["self"] = cycle
        deep = {}
        for _ in range(40):
            deep = {"next": deep}
        for value in (cycle, deep, {1: "key"}, {"value": object()},
                      {"n": float("nan")}, {"n": float("inf")},
                      {"n": 2**65}, {"bad": "\ud800"}):
            with self.subTest(kind=type(value)), self.assertRaises(ContractError):
                freeze_json(value)

    def test_size_limits_prevent_unbounded_message_trees(self):
        for value in ({"large": "x" * (1024 * 1024)}, [None] * 32769):
            with self.assertRaises(ContractError):
                freeze_json(value)

    def test_payloads_and_nonces_do_not_appear_in_reprs(self):
        values = [
            HostHello("instance", "private-nonce"),
            PluginHello("instance", "private-nonce", "test.adapter"),
            Heartbeat("instance", "private-nonce", 1, 1),
            Pong("instance", "private-nonce", 1),
            ActionResult("request", "completed", {"secret": "private-value"}),
            EventEnvelope(session(), "event", {"secret": "private-value"}),
            Challenge("challenge", "qr", "Scan", {"image": "private-value"}),
        ]
        for value in values:
            self.assertNotIn("private-nonce", repr(value))
            self.assertNotIn("private-value", repr(value))


class BindingTests(unittest.TestCase):
    def test_handshake_validates_instance_nonce_id_and_version(self):
        value = manifest()
        hello = HostHello("instance-1", "nonce-1")
        reply = PluginHello("instance-1", "nonce-1", value.plugin_id)
        self.assertIs(validate_handshake(value, hello, reply), reply)
        for change in ({"instance_id": "instance-2"}, {"nonce": "nonce-2"},
                       {"plugin_id": "other.plugin"}, {"api_major": 2},
                       {"api_minor": 1}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_handshake(value, hello, replace(reply, **change))
        with self.assertRaises(CompatibilityError):
            validate_handshake(manifest(api_major=2), hello, reply)

    def test_heartbeat_requires_current_probe_and_deadline(self):
        heartbeat = Heartbeat("instance-1", "nonce-1", 7, 20.0)
        pong = Pong("instance-1", "nonce-1", 7)
        self.assertIs(validate_pong(heartbeat, pong, now=19), pong)
        with self.assertRaises(DeadlineExceeded):
            validate_pong(heartbeat, pong, now=20)
        for change in ({"instance_id": "instance-2"}, {"nonce": "nonce-2"}, {"sequence": 6}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_pong(heartbeat, replace(pong, **change), now=19)

    def test_action_deadline_never_implies_retry(self):
        request = ActionRequest("request", session(), "echo", {}, 20)
        request.require_live(now=19)
        with self.assertRaises(DeadlineExceeded):
            request.require_live(now=20)
        self.assertEqual(request.request_id, "request")

    def test_account_platform_and_epoch_are_all_identity_components(self):
        expected = session()
        self.assertIs(require_session(expected, expected), expected)
        for change in ({"self_id": "10001"}, {"platform": "other"},
                       {"session_epoch": "epoch-2"}):
            with self.subTest(change=change), self.assertRaises(SessionMismatch):
                require_session(session(**change), expected)

    def test_result_correlation_and_completion_are_unambiguous(self):
        request = ActionRequest("request", session(), "echo", {}, 20)
        result = ActionResult("request", "completed", {"yes": True})
        self.assertTrue(validate_action_result(request, result).succeeded)
        with self.assertRaises(ContractError):
            validate_action_result(request, replace(result, request_id="another"))
        for outcome in ("not_started", "unknown"):
            with self.subTest(outcome=outcome), self.assertRaises(ContractError):
                ActionResult("request", outcome)
        for outcome in ("not_started", "completed", "unknown"):
            failure = ActionResult("request", outcome, error_code=1502)
            self.assertFalse(failure.succeeded)
        with self.assertRaises(ContractError):
            ActionResult("request", "completed", error_code=True)

    def test_invalid_scalar_shapes_use_contract_errors(self):
        for factory in (
            lambda: ActionResult("request", []),
            lambda: SessionUpdate(None, []),
            lambda: Challenge("c", [], "t", {}),
            lambda: HostHello("instance", "nonce", api_major=True),
            lambda: Heartbeat("instance", "nonce", True, 1),
            lambda: SessionIdentity("example", 123, "epoch"),
            lambda: ActionRequest("r", session(), "echo", [], 1),
        ):
            with self.subTest(factory=factory), self.assertRaises(ContractError):
                factory()

    def test_online_requires_bound_session(self):
        with self.assertRaises(ContractError):
            SessionUpdate(None, "online")
        self.assertEqual(SessionUpdate(None, "login_required").state, "login_required")


class ContextAndConformanceTests(unittest.TestCase):
    def test_context_ack_is_correlated_and_typed(self):
        with tempfile.TemporaryDirectory() as directory:
            updates, challenges = [], []
            context = PluginContext(
                "instance", Path(directory).resolve(),
                publish_event=lambda event: PublishAck(event.event_id, True),
                update_session=updates.append, publish_challenge=challenges.append)
            event = EventEnvelope(session(), "event", {"type": "test"})
            self.assertTrue(context.publish_event(event).persisted)
            context.update_session(SessionUpdate(session(), "online"))
            context.publish_challenge(Challenge("c", "notice", "Ready", {}))
            self.assertEqual(len(updates), 1)
            self.assertEqual(len(challenges), 1)
            self.assertFalse(hasattr(context, "runtime"))
            self.assertFalse(hasattr(context, "database"))
            with self.assertRaises(ContractError):
                context.publish_event({})
            with self.assertRaises(ContractError):
                context.update_session({})

    def test_wrong_ack_and_relative_state_directory_are_rejected(self):
        with self.assertRaises(ContractError):
            PluginContext("i", Path("relative"), publish_event=lambda e: None,
                          update_session=lambda s: None, publish_challenge=lambda c: None)
        with tempfile.TemporaryDirectory() as directory:
            context = PluginContext(
                "instance", Path(directory).resolve(),
                publish_event=lambda event: PublishAck("different-event", True),
                update_session=lambda update: None, publish_challenge=lambda challenge: None)
            with self.assertRaises(ContractError):
                context.publish_event(EventEnvelope(session(), "event", {}))
        with self.assertRaises(ContractError):
            PublishAck("event", False, duplicate=True)

    def test_offline_example_passes_executable_conformance_without_network(self):
        with patch("socket.socket", side_effect=AssertionError("network")):
            report = run_conformance(OfflineExamplePlugin)
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["checks"]), 13)
        self.assertFalse(report["network_required"])

    def test_conformance_cli_reports_success_and_sanitizes_failures(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(conformance_main([]), 0)
        self.assertTrue(json.loads(output.getvalue())["passed"])
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(conformance_main(["--plugin", "does.not.exist:factory"]), 1)
        report = json.loads(output.getvalue())
        self.assertFalse(report["passed"])
        self.assertNotIn("does.not.exist", output.getvalue())

    def test_example_requires_handshake_and_rejects_stale_ping_and_session(self):
        plugin = OfflineExamplePlugin()
        self.assertIsInstance(plugin, Plugin)
        hello = HostHello("instance", "nonce")
        with tempfile.TemporaryDirectory() as directory:
            updates = []
            context = PluginContext(
                hello.instance_id, Path(directory).resolve(),
                publish_event=lambda event: PublishAck(event.event_id, True),
                update_session=updates.append, publish_challenge=lambda challenge: None)
            with self.assertRaises(ContractError):
                plugin.start(context, {"self_id": "10000"})
            plugin.handshake(hello)
            plugin.start(context, {"self_id": "10000"})
            heartbeat = Heartbeat("instance", "nonce", 1, time.monotonic() + 1)
            plugin.ping(heartbeat)
            with self.assertRaises(ContractError):
                plugin.ping(heartbeat)
            with self.assertRaises(SessionMismatch):
                plugin.call(ActionRequest(
                    "r", session(session_epoch="wrong"), "get_login_info", {},
                    time.monotonic() + 1))
            plugin.stop("test")
            plugin.stop("test")
            self.assertEqual(updates[-1].state, "stopped")

    def test_backend_error_keeps_existing_public_shape(self):
        error = BackendError("failed", 1502, details={"action_outcome": "unknown"})
        self.assertEqual(error.retcode, 1502)
        self.assertEqual(error.details, {"action_outcome": "unknown"})
        self.assertEqual(str(error), "failed")


if __name__ == "__main__":
    unittest.main()
