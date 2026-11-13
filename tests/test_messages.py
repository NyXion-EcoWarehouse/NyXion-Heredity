from copy import deepcopy
import unittest

from heredity.backends.base import BackendError
from heredity.messages import cq_string, message_id, normalize_message, positive_id


class IdentifierTests(unittest.TestCase):
    def test_positive_identifiers_accept_only_integers_and_numeric_strings(self):
        for value, expected in ((1, 1), ("42", 42), (" +9 ", 9)):
            self.assertEqual(positive_id(value), expected)
        for value in (True, False, 1.0, 1.5, "1.0", "x", None, 0, -1):
            with self.subTest(value=value), self.assertRaises(BackendError) as caught:
                positive_id(value)
            self.assertEqual(caught.exception.retcode, 1400)

    def test_message_identifiers_preserve_signed_boundaries(self):
        for value in (-2147483648, -17, 1, 2147483647):
            self.assertEqual(message_id(value), value)
            self.assertEqual(message_id(str(value)), value)
        for value in (0, True, False, -2147483649, 2147483648, 1.0, "-1.0", None):
            with self.subTest(value=value), self.assertRaises(BackendError):
                message_id(value)


class MessageTests(unittest.TestCase):
    def test_mixed_cq_message_and_parameter_escaping(self):
        result = normalize_message(
            "Hello &amp; &#91;world&#93;, [CQ:mention,target=participant-7]"
            "[CQ:reply,id=-29][CQ:image,file=a&#44;b&amp;c]"
        )
        self.assertEqual(
            result,
            [
                {"type": "text", "data": {"text": "Hello & [world], "}},
                {"type": "mention", "data": {"target": "participant-7"}},
                {"type": "reply", "data": {"id": "-29"}},
                {"type": "image", "data": {"file": "a,b&c"}},
            ],
        )

    def test_auto_escape_leaves_cq_literal(self):
        source = "[CQ:mention,target=everyone] &amp; text"
        self.assertEqual(
            normalize_message(source, auto_escape=True),
            [{"type": "text", "data": {"text": source}}],
        )

    def test_entities_are_not_recursively_decoded(self):
        normalized = normalize_message("&amp;#91; &#44;[CQ:image,file=&amp;#44;]")
        self.assertEqual(normalized[0]["data"]["text"], "&#91; &#44;")
        self.assertEqual(normalized[1]["data"]["file"], "&#44;")

    def test_array_input_remains_unchanged_and_nested_data_is_retained(self):
        original = [
            {"type": "text", "data": {"text": "unchanged"}},
            {"type": "custom", "data": {"nested": {"values": [True, None, 3]}}},
        ]
        before = deepcopy(original)
        normalized = normalize_message(original)
        self.assertEqual(original, before)
        self.assertEqual(normalized, original)
        normalized[1]["data"]["nested"]["values"].append("new")
        self.assertEqual(original, before)

    def test_known_segment_fields_are_validated(self):
        valid = [
            {"type": "text", "data": {"text": " "}},
            {"type": "mention", "data": {"target": "everyone"}},
            {"type": "mention", "data": {"target": "participant-7"}},
            {"type": "reply", "data": {"id": -100}},
        ]
        self.assertEqual(normalize_message(valid), valid)
        for segment in (
            {"type": "text", "data": {"text": 1}},
            {"type": "text", "data": {"text": ""}},
            {"type": "reply", "data": {"id": 0}},
            {"type": "reply", "data": {"id": 1.0}},
        ):
            with self.subTest(segment=segment), self.assertRaises(BackendError):
                normalize_message([segment])

    def test_invalid_shapes_and_cq_are_rejected_without_silent_loss(self):
        invalid = (
            "", [], {}, None, 1, [None], [{}],
            [{"type": "", "data": {}}],
            [{"type": "text"}],
            [{"type": "custom", "data": {}, "discarded": True}],
            [{"type": "custom", "data": []}],
            "[CQ:mention,target=everyone",
            "[CQ:]",
            "[CQ:mention,target=participant-7,target=participant-8]",
            "[CQ:image,file=x,broken]",
            "[CQ:text,text=]",
            "valid prefix[CQ:reply,id=0]",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(BackendError) as caught:
                normalize_message(value)
            self.assertEqual(caught.exception.retcode, 1400)

    def test_non_json_nonfinite_cycles_and_nesting_are_rejected(self):
        circular = []
        circular.append(circular)
        nested = {}
        for _ in range(66):
            nested = {"child": nested}
        for value in (float("nan"), float("inf"), {"not-json"}, circular, nested, {1: "key"}):
            with self.subTest(kind=type(value).__name__), self.assertRaises(BackendError):
                normalize_message([{"type": "custom", "data": {"value": value}}])

    def test_excessive_message_size_and_nonboolean_auto_escape_are_rejected(self):
        with self.assertRaises(BackendError):
            normalize_message("x" * (1024 * 1024))
        for value in ("true", 1, None):
            with self.assertRaises(BackendError):
                normalize_message("text", auto_escape=value)

    def test_cq_string_uses_text_and_parameter_specific_escaping(self):
        segments = [
            {"type": "text", "data": {"text": "a,b & [literal]"}},
            {"type": "image", "data": {"file": "a,b & [x]"}},
            {"type": "mention", "data": {"target": "everyone"}},
            {"type": "reply", "data": {"id": "-88"}},
        ]
        encoded = cq_string(segments)
        self.assertEqual(
            encoded,
            "a,b &amp; &#91;literal&#93;"
            "[CQ:image,file=a&#44;b &amp; &#91;x&#93;]"
            "[CQ:mention,target=everyone][CQ:reply,id=-88]",
        )
        self.assertEqual(normalize_message(encoded), segments)

    def test_cq_string_does_not_silently_drop_unrepresentable_fields(self):
        for value in (
            [{"type": "custom", "data": {"nested": {}}}],
            [{"type": "text", "data": {"text": "x", "extra": "y"}}],
            [{"type": "custom", "data": {"bad,key": "x"}}],
        ):
            with self.assertRaises(BackendError):
                cq_string(value)

    def test_generic_segments_and_scalar_parameters_are_kept(self):
        segments = [{"type": "custom_segment", "data": {"flag": True, "n": 3, "other": None}}]
        self.assertEqual(
            cq_string(segments), "[CQ:custom_segment,flag=true,n=3,other=null]"
        )
        self.assertEqual(normalize_message("[CQ:custom_segment]"), [
            {"type": "custom_segment", "data": {}}
        ])


if __name__ == "__main__":
    unittest.main()
