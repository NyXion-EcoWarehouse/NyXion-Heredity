from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from heredity.backends.base import BackendError
from heredity.providers.contracts import ExecutionLevel
from heredity.providers.registry import (
    build_manager,
    demo_registration,
    execution_payload,
    profile_registration,
    read_registry,
)
from heredity.storage import MessageStore


def profile_config(self_id=123):
    return {
        "self_id": self_id,
        "base_url": "http://127.0.0.1:19001",
        "actions": {
            "get_login_info": {"path": "/identity"},
            "send_private_msg": {"path": "/send"},
        },
    }


class ProviderRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.store = MessageStore()
        self.addCleanup(self.store.close)

    def write_json(self, name, value):
        path = self.directory / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def registry(self, entries=None, default="first"):
        return {
            "schema_version": 1,
            "self_id": 123,
            "default_provider": default,
            "providers": entries or [{"id": "first", "kind": "demo"}],
        }

    def build(self, registry, **kwargs):
        manager = build_manager(
            self.store, 123, registry=registry, registry_directory=self.directory, **kwargs
        )
        self.addCleanup(manager.close)
        return manager

    def test_raw_demo_and_profile_sources_do_not_infer_execution_level(self):
        demo = demo_registration(self.store, 123).descriptor
        account, registered = profile_registration(profile_config())
        self.assertEqual(account, 123)
        for item, source in ((demo, "demo"), (registered.descriptor, "profile")):
            with self.subTest(source=source):
                self.assertEqual(item.source_kind, source)
                self.assertIsNone(item.execution_level)
                self.assertEqual(item.level_provenance, "none")

    def test_explicit_l2_configuration_is_declared_not_implemented(self):
        self.write_json("first.json", profile_config())
        registry = self.registry([{
            "id": "first", "kind": "http_json", "profile": "first.json",
            "execution_level": "L2",
        }])
        manager = self.build(registry, activate=False)
        item = next(item for item in manager.snapshot()["providers"] if item["provider_id"] == "first")
        self.assertEqual(item["execution_level"], ExecutionLevel.L2.value)
        self.assertEqual(item["level_provenance"], "declared")
        self.assertEqual(item["source_kind"], "profile")
        self.assertEqual(item["host_kind"], "full_client")
        self.assertEqual(item["session_owner"], "heredity")
        self.assertIsNone(manager.active_descriptor)

    def test_inspection_never_calls_http_or_activates_requested_provider(self):
        self.write_json("first.json", profile_config())
        registry = self.registry([{
            "id": "first", "kind": "http_json", "profile": "first.json",
        }])
        with (
            patch("heredity.backends.http_json.HTTPJSONBackend.call",
                  side_effect=AssertionError("inspection performed an action")) as call,
            patch("urllib.request.OpenerDirector.open",
                  side_effect=AssertionError("inspection opened a network connection")) as opening,
        ):
            manager = self.build(registry, activate=False, initial_provider_id="first")
            payload = execution_payload(manager)
            call.assert_not_called()
            opening.assert_not_called()
        self.assertEqual(payload["state"]["state"], "unconfigured")
        self.assertIsNone(payload["state"]["active_provider_id"])
        self.assertEqual([item["level"] for item in payload["levels"]], ["L0", "L1", "L2", "L3"])
        self.assertEqual(len(payload["state"]["providers"]), 5)

    def test_demo_inspection_does_not_construct_demo_provider(self):
        with patch(
            "heredity.providers.registry.MockBackend",
            side_effect=AssertionError("inspection activated demo"),
        ) as factory:
            manager = build_manager(self.store, 123, demo=True, activate=False)
            self.addCleanup(manager.close)
            factory.assert_not_called()
        self.assertIsNone(manager.active_descriptor)
        demo = next(item for item in manager.snapshot()["providers"] if item["provider_id"] == "demo")
        self.assertTrue(demo["selectable"])
        self.assertIsNone(demo["execution_level"])

    def test_read_registry_rejects_unknown_kind_and_demo_profile_mixing(self):
        invalid = [
            self.registry([{"id": "first", "kind": "native"}]),
            self.registry([{"id": "first", "kind": "unknown"}]),
            self.registry([
                {"id": "first", "kind": "demo"},
                {"id": "second", "kind": "http_json", "profile": "second.json"},
            ]),
        ]
        for index, value in enumerate(invalid):
            with self.subTest(value=value):
                path = self.write_json(f"invalid-{index}.json", value)
                with self.assertRaises(ValueError):
                    read_registry(path)

    def test_registry_schema_and_default_provider_are_explicit(self):
        for index, changes in enumerate((
            {"schema_version": True},
            {"schema_version": 2},
            {"providers": []},
            {"providers": [None]},
            {"default_provider": None},
        )):
            value = {**self.registry(), **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                read_registry(self.write_json(f"schema-{index}.json", value))
        registry = read_registry(self.write_json("unknown-default.json", self.registry(default="unknown")))
        with self.assertRaises(BackendError) as caught:
            self.build(registry)
        self.assertEqual(caught.exception.retcode, 1404)

    def test_provider_id_collisions_cannot_replace_native_or_existing_entry(self):
        for entries in (
            [{"id": "l0-ui", "kind": "demo"}],
            [{"id": "first", "kind": "demo"}, {"id": "first", "kind": "demo"}],
        ):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                self.build(self.registry(entries), activate=False)

    def test_profiles_must_bind_same_account_as_runtime(self):
        wrong = profile_config(999)
        with self.assertRaises(ValueError):
            build_manager(self.store, 123, profile=wrong, activate=False)
        self.write_json("wrong-account.json", wrong)
        registry = self.registry([{
            "id": "first", "kind": "http_json", "profile": "wrong-account.json",
        }])
        with self.assertRaises(ValueError):
            self.build(registry, activate=False)

    def test_missing_profile_and_unknown_declared_level_are_rejected(self):
        registry = self.registry([{
            "id": "first", "kind": "http_json", "profile": "missing.json",
        }])
        with self.assertRaises(OSError):
            self.build(registry, activate=False)
        with self.assertRaises(ValueError):
            profile_registration(profile_config(), level="L4")
        with self.assertRaises(ValueError):
            self.build(self.registry([{
                "id": "first", "kind": "demo", "execution_level": "L0",
            }]), activate=False)

    def test_missing_identity_action_disabled_or_missing_credentials_are_unavailable(self):
        no_identity = profile_config()
        del no_identity["actions"]["get_login_info"]
        needs_token = profile_config()
        needs_token["access_token_env"] = "HEREDITY_TEST_PROVIDER_TOKEN"
        cases = [
            (no_identity, {}),
            (profile_config(), {"enabled": False}),
            (needs_token, {}),
        ]
        with patch.dict(os.environ, {"HEREDITY_TEST_PROVIDER_TOKEN": ""}):
            for value, options in cases:
                with self.subTest(value=value, options=options):
                    account, registration = profile_registration(value, **options)
                    self.assertEqual(account, 123)
                    self.assertEqual(registration.descriptor.availability, "configuration_required")
                    self.assertFalse(registration.descriptor.selectable)
        with self.assertRaises(ValueError):
            profile_registration(profile_config(), enabled="true")

    def test_invalid_http_settings_remain_visible_as_unavailable(self):
        for changes in (
            {"base_url": "https://example.invalid:443"},
            {"base_url": "http://user:pass@127.0.0.1:19001"},
            {"actions": {"get_login_info": {"path": "//not-a-path"}}},
        ):
            with self.subTest(changes=changes):
                _, registration = profile_registration({**profile_config(), **changes})
                self.assertEqual(registration.descriptor.availability, "configuration_required")
                self.assertFalse(registration.descriptor.selectable)

    def test_profile_credential_values_never_enter_public_registry_metadata(self):
        value = profile_config()
        value["access_token_env"] = "HEREDITY_TEST_PROVIDER_TOKEN"
        original = deepcopy(value)
        with patch.dict(os.environ, {"HEREDITY_TEST_PROVIDER_TOKEN": "fixture-secret-token"}):
            _, registration = profile_registration(value)
            self.assertTrue(registration.descriptor.selectable)
            rendered = json.dumps(registration.descriptor.to_dict())
            self.assertNotIn("fixture-secret-token", rendered)
            self.assertNotIn("access_token_env", rendered)
        self.assertEqual(value, original)

    def test_profile_cannot_point_back_to_gateway_listening_port(self):
        for origin in ("http://127.0.0.1:5700", "http://localhost:5700", "http://[::1]:5700"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                profile_registration(
                    {**profile_config(), "base_url": origin}, forbidden_port=5700
                )
        _, registration = profile_registration(profile_config(), forbidden_port=5700)
        self.assertTrue(registration.descriptor.selectable)


if __name__ == "__main__":
    unittest.main()
