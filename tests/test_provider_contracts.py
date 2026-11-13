from dataclasses import FrozenInstanceError, replace
import json
import unittest

from heredity.providers.contracts import (
    ExecutionLevel,
    ProviderDescriptor,
    level_catalog,
)


def descriptor(**changes):
    fields = {
        "provider_id": "fixture-ui",
        "label": "测试界面",
        "execution_level": ExecutionLevel.L0,
        "source_kind": "native",
        "host_kind": "official_gui",
        "session_owner": "official_client",
        "target_memory_access": "read",
        "in_process_code": False,
        "business_request_builder": "official_services",
        "primary_transport_owner": "official_client",
        "event_sources": ("ui", "memory_observer"),
        "supported_actions": ("get_login_info",),
        "receipt_evidence": ("ui_input_submitted",),
        "availability": "ready",
        "level_provenance": "implementation",
    }
    fields.update(changes)
    return ProviderDescriptor(**fields)


class ProviderDescriptorTests(unittest.TestCase):
    def test_descriptor_is_frozen_and_capabilities_are_tuples(self):
        item = descriptor()
        with self.assertRaises(FrozenInstanceError):
            item.label = "changed"
        for name in ("auxiliary_transports", "event_sources", "supported_actions", "receipt_evidence"):
            self.assertIsInstance(getattr(item, name), tuple)
            with self.subTest(name=name), self.assertRaises(ValueError):
                descriptor(**{name: ["mutable"]})

    def test_to_dict_is_independent_and_json_safe(self):
        item = descriptor()
        rendered = item.to_dict()
        self.assertEqual(rendered["execution_level"], "L0")
        self.assertTrue(rendered["selectable"])
        self.assertEqual(rendered["event_sources"], ["ui", "memory_observer"])
        self.assertEqual(json.loads(json.dumps(rendered, allow_nan=False)), rendered)
        rendered["supported_actions"].append("delete_msg")
        self.assertEqual(item.supported_actions, ("get_login_info",))

    def test_availability_alone_controls_selectability(self):
        for value in ("ready", "partial", "unimplemented", "configuration_required"):
            with self.subTest(availability=value):
                self.assertEqual(descriptor(availability=value).selectable, value == "ready")

    def test_invalid_identifiers_actions_and_duplicate_capabilities_are_rejected(self):
        cases = [
            {"provider_id": ""},
            {"provider_id": "../provider"},
            {"provider_id": "has space"},
            {"provider_id": "a" * 65},
            {"supported_actions": ("get/status",)},
            {"supported_actions": ("send_msg\n",)},
            {"supported_actions": ("send_msg", "send_msg")},
            {"auxiliary_transports": ("http", "http")},
            {"receipt_evidence": ("not a token",)},
            {"source_kind": "unknown_source"},
            {"availability": "pretend_ready"},
            {"in_process_code": 0},
            {"target_memory_access": "all"},
            {"label": "bad\nlabel"},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                descriptor(**changes)

    def test_enum_is_explicit_and_unknown_levels_are_rejected(self):
        self.assertEqual(ExecutionLevel("L2"), ExecutionLevel.L2)
        for level in ("L0", "L4", 0, []):
            with self.subTest(level=level), self.assertRaises(ValueError):
                descriptor(execution_level=level)

    def test_demo_profile_and_unconfigured_sources_can_have_no_level(self):
        for source in ("demo", "profile", "unconfigured"):
            item = descriptor(
                source_kind=source,
                execution_level=None,
                host_kind="unknown",
                session_owner="unknown",
                primary_transport_owner="unknown",
                business_request_builder="unknown",
                event_sources=(),
                level_provenance="none",
            )
            self.assertIsNone(item.execution_level)
            self.assertIsNone(item.to_dict()["execution_level"])
        for source in ("demo", "unconfigured"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                descriptor(source_kind=source)
        with self.assertRaises(ValueError):
            descriptor(execution_level=None, level_provenance="none")

    def test_level_provenance_cannot_be_fabricated_or_omitted(self):
        with self.assertRaises(ValueError):
            descriptor(level_provenance="none")
        with self.assertRaises(ValueError):
            descriptor(source_kind="profile", execution_level=None, level_provenance="declared")
        self.assertEqual(descriptor(source_kind="profile", level_provenance="declared").level_provenance, "declared")

    def test_l0_cannot_write_inject_or_receive_raw_packet_events(self):
        for changes in (
            {"target_memory_access": "write"},
            {"in_process_code": True},
            {"event_sources": ("packet_decoder",)},
            {"business_request_builder": "heredity"},
            {"host_kind": "standalone"},
            {"session_owner": "heredity"},
            {"primary_transport_owner": "heredity"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                descriptor(**changes)

    def test_l1_attaches_to_the_existing_official_client(self):
        item = descriptor(
            execution_level=ExecutionLevel.L1,
            host_kind="attached_client",
            in_process_code=True,
            target_memory_access="write",
            business_request_builder="mixed",
            event_sources=("kernel_callback", "packet_decoder"),
        )
        self.assertEqual(item.execution_level, ExecutionLevel.L1)
        for changes in ({"session_owner": "heredity"}, {"in_process_code": False}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(item, **changes)

    def test_l2_is_a_complete_first_party_client(self):
        item = descriptor(
            execution_level=ExecutionLevel.L2,
            host_kind="full_client",
            session_owner="heredity",
            primary_transport_owner="heredity",
            business_request_builder="heredity",
            target_memory_access="none",
            event_sources=("protocol_decoder",),
        )
        self.assertEqual(item.execution_level, ExecutionLevel.L2)
        with self.assertRaises(ValueError):
            replace(item, session_owner="official_client")
        with self.assertRaises(ValueError):
            replace(item, business_request_builder="official_services")

    def test_l3_is_a_minimized_first_party_protocol_core(self):
        item = descriptor(
            execution_level=ExecutionLevel.L3,
            host_kind="protocol_core",
            session_owner="heredity",
            primary_transport_owner="heredity",
            business_request_builder="heredity",
            target_memory_access="none",
            event_sources=("packet_decoder",),
        )
        self.assertEqual(item.execution_level, ExecutionLevel.L3)
        for changes in (
            {"host_kind": "full_client"},
            {"session_owner": "official_client"},
            {"primary_transport_owner": "official_client"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(item, **changes)

    def test_catalog_describes_four_technical_levels_without_risk_claims(self):
        catalog = level_catalog()
        self.assertEqual([item["level"] for item in catalog], ["L0", "L1", "L2", "L3"])
        for item in catalog:
            self.assertTrue(item["label"])
            self.assertTrue(item["description"])
            self.assertFalse(any("risk" in key.lower() for key in item))
        catalog[0]["label"] = "changed"
        self.assertEqual(level_catalog()[0]["label"], "界面驱动")


if __name__ == "__main__":
    unittest.main()
