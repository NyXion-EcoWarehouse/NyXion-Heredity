import unittest
from unittest.mock import patch

from heredity.providers.registry import profile_registration


class ProviderMechanismTests(unittest.TestCase):
    def profile(self):
        return {"self_id": 123, "base_url": "http://127.0.0.1:17001",
                "actions": {"get_login_info": {"path": "/get_login_info",
                                                "response_path": ["data"]}}}

    def test_l2_is_a_first_party_client_with_protocol_evidence(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No network")):
            _, registration = profile_registration(
                self.profile(), level="L2",
                mechanism={"receipt_evidence": ["business_response"]})
        descriptor = registration.descriptor
        self.assertEqual(descriptor.execution_level.value, "L2")
        self.assertFalse(descriptor.in_process_code)
        self.assertEqual(descriptor.host_kind, "full_client")
        self.assertEqual(descriptor.session_owner, "heredity")
        self.assertEqual(descriptor.level_provenance, "declared")
        self.assertEqual(descriptor.receipt_evidence, ("business_response",))

    def test_l0_rejects_conflicting_mechanism_fields(self):
        for mechanism in ({"in_process_code": True}, {"target_memory_access": "write"},
                          {"event_sources": ["packet_decoder"]}):
            with self.subTest(mechanism=mechanism), self.assertRaises(ValueError):
                profile_registration(self.profile(), level="L0", mechanism=mechanism)

    def test_mechanism_cannot_override_availability_or_action_support(self):
        for mechanism in ({"availability": "ready"}, {"source_kind": "native"},
                          {"supported_actions": ["send_private_msg"]}):
            with self.subTest(mechanism=mechanism), self.assertRaises(ValueError):
                profile_registration(self.profile(), level="L2", mechanism=mechanism)

    def test_l2_rejects_attached_process_mechanisms(self):
        for mechanism in ({"in_process_code": True},
                          {"target_memory_access": "write"},
                          {"business_request_builder": "mixed"}):
            with self.subTest(mechanism=mechanism), self.assertRaises(ValueError):
                profile_registration(self.profile(), level="L2", mechanism=mechanism)


if __name__ == "__main__":
    unittest.main()
