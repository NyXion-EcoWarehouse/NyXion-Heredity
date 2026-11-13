import json
import unittest
from unittest.mock import patch

from heredity.backends.base import BackendError
from heredity.providers import (
    AttachedProcessProvider,
    CoreProtocolProvider,
    ExecutionLevel,
    FullClientProvider,
    UiDriverProvider,
    placeholder_descriptors,
)


PROVIDER_TYPES = (
    UiDriverProvider,
    AttachedProcessProvider,
    FullClientProvider,
    CoreProtocolProvider,
)


class PlaceholderDescriptorTests(unittest.TestCase):
    def test_four_distinct_descriptors_are_truthfully_unavailable(self):
        descriptors = placeholder_descriptors()
        self.assertIsInstance(descriptors, tuple)
        self.assertEqual(
            [item.provider_id for item in descriptors],
            ["l0-ui", "l1-attached", "l2-client", "l3-core"],
        )
        self.assertEqual(
            [item.execution_level for item in descriptors], list(ExecutionLevel)
        )
        self.assertEqual(
            [item.availability for item in descriptors], ["unimplemented"] * 4
        )
        for item in descriptors:
            with self.subTest(provider=item.provider_id):
                self.assertFalse(item.selectable)
                self.assertEqual(item.supported_actions, ())
                self.assertTrue(item.reason)
                self.assertEqual(item.level_provenance, "declared")
                json.dumps(item.to_dict(), allow_nan=False)

    def test_l0_slot_requires_an_external_execution_implementation(self):
        item = placeholder_descriptors()[0]
        self.assertEqual(item.target_memory_access, "read")
        self.assertFalse(item.in_process_code)
        self.assertIn("外部适配器", item.reason)
        self.assertEqual(item.receipt_evidence, ())


class PlaceholderProviderTests(unittest.TestCase):
    def test_entrypoints_keep_separate_identities_and_never_report_online(self):
        for provider_type, expected in zip(PROVIDER_TYPES, placeholder_descriptors()):
            with self.subTest(provider=provider_type.__name__):
                provider = provider_type(self_id=123)
                self.assertEqual(provider.self_id, 123)
                self.assertFalse(provider.online)
                self.assertIs(provider.descriptor, expected)
                provider.close()
                self.assertFalse(provider.online)

    def test_login_and_actions_fail_without_effect_or_private_argument_echo(self):
        for provider_type in PROVIDER_TYPES:
            provider = provider_type(self_id=123)
            for action in ("get_login_info", "send_private_msg", "get_status"):
                with self.subTest(provider=provider_type.__name__, action=action):
                    with self.assertRaises(BackendError) as caught:
                        provider.call(action, {"access_token": "fixture-secret"})
                    failure = caught.exception
                    self.assertEqual(failure.retcode, 1503)
                    self.assertEqual(failure.details["provider_id"], provider.descriptor.provider_id)
                    self.assertFalse(failure.details["selectable"])
                    self.assertFalse(failure.details["implemented"])
                    self.assertFalse(failure.details["action_completed"])
                    self.assertFalse(failure.details["closed"])
                    rendered = json.dumps(failure.details, ensure_ascii=False)
                    self.assertNotIn("fixture-secret", str(failure) + rendered)
            provider.close()

    def test_close_is_idempotent_and_calls_remain_unavailable(self):
        for provider_type in PROVIDER_TYPES:
            with self.subTest(provider=provider_type.__name__):
                provider = provider_type()
                provider.close()
                provider.close()
                with self.assertRaises(BackendError) as caught:
                    provider.call("get_login_info", {})
                self.assertEqual(caught.exception.retcode, 1503)
                self.assertTrue(caught.exception.details["closed"])

    def test_invalid_account_ids_are_rejected_before_initialization(self):
        for provider_type in PROVIDER_TYPES:
            for self_id in (-1, True, "123", None):
                with self.subTest(provider=provider_type.__name__, self_id=self_id):
                    with self.assertRaises(ValueError):
                        provider_type(self_id=self_id)

    def test_construction_does_not_load_native_or_start_network_or_processes(self):
        with (
            patch("ctypes.CDLL", side_effect=AssertionError("native load")),
            patch("ctypes.WinDLL", side_effect=AssertionError("native load"), create=True),
            patch("socket.create_connection", side_effect=AssertionError("network")),
            patch("subprocess.Popen", side_effect=AssertionError("process launch")),
        ):
            for provider_type in PROVIDER_TYPES:
                provider = provider_type(self_id=123)
                provider.close()


if __name__ == "__main__":
    unittest.main()
