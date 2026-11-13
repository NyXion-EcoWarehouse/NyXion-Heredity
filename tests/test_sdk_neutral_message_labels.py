"""Generic message validation leaves extension semantics to platform adapters."""

from copy import deepcopy
import unittest

from nyxion_heredity_sdk import (
    ActionRequest, ActionResult, BackendError, EventEnvelope, SessionIdentity, thaw_json,
)
from nyxion_heredity_sdk.onebot import cq_string, normalize_message


class NeutralMessageBoundaryTests(unittest.TestCase):
    def test_extension_target_fields_remain_opaque(self):
        for data in ({}, {"target": 0}, {"target": True}, {"target": None},
                     {"target": "participant-7"},
                     {"identity": {"namespace": "example", "id": "member-alpha"}}):
            with self.subTest(data=data):
                value = [{"type": "at", "data": data}]
                before = deepcopy(value)
                self.assertEqual(normalize_message(value), before)
                self.assertEqual(value, before)

    def test_extension_data_is_copied_without_changing_fields_or_values(self):
        value = [{"type": "extension", "data": {
            "target": "example:member-alpha",
            "payload": {"flags": [True, False, None], "count": 7},
            "empty": {},
        }}]
        before = deepcopy(value)
        result = normalize_message(value)
        result[0]["data"]["payload"]["flags"].append("changed")
        self.assertEqual(value, before)
        self.assertEqual(result[0]["data"]["target"], "example:member-alpha")
        self.assertEqual(result[0]["data"]["empty"], {})

    def test_flat_extension_parameters_round_trip_with_neutral_names(self):
        source = "[CQ:mention,target=example:member-alpha,label=a&#44;b&amp;c]"
        value = [{"type": "mention", "data": {
            "target": "example:member-alpha", "label": "a,b&c",
        }}]
        self.assertEqual(normalize_message(source), value)
        self.assertEqual(cq_string(value), source)

    def test_action_and_event_boundaries_preserve_opaque_message_data(self):
        payload = {"message": [{"type": "extension", "data": {
            "target": {"namespace": "example", "id": "member-alpha"},
            "flags": [True, None, 3],
        }}]}
        before = deepcopy(payload)
        session = SessionIdentity("example", "member-alpha", "session-one")
        request = ActionRequest("request-one", session, "send_msg", payload, 60.0)
        result = ActionResult("request-one", "completed", payload)
        event = EventEnvelope(session, "event-one", payload)
        payload["message"][0]["data"]["flags"].append("changed")
        self.assertEqual(thaw_json(request.params), before)
        self.assertEqual(thaw_json(result.data), before)
        self.assertEqual(thaw_json(event.event), before)

    def test_structural_text_and_reply_validation_keep_error_codes(self):
        for value in (
            [{"type": "extension", "data": []}],
            [{"type": "extension", "data": {}, "extra": True}],
            [{"type": "text", "data": {"text": ""}}],
            [{"type": "reply", "data": {"id": 0}}],
        ):
            with self.subTest(value=value), self.assertRaises(BackendError) as failure:
                normalize_message(value)
            self.assertEqual(failure.exception.retcode, 1400)


if __name__ == "__main__":
    unittest.main()
