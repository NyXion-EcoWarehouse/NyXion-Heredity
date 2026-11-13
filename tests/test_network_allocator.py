"""Host allocations use configured interfaces and explicit, scoped grants only."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from heredity.network_allocator import NetworkAllocationError, NetworkInterfaceAllocator
from heredity.network_interfaces import NetworkInterfaceManager
from nyxion_heredity_sdk import (
    BackendError, ContractError, DeadlineExceeded, NetworkAllocationDenied,
    NetworkBindingClosed, NetworkInterfaceDeclarations, NetworkInterfaceGrant,
    NetworkInterfaceMultiAllocator, NetworkInterfaceRequirement, NetworkInterfaces,
    NetworkInterfaceLifecycle,
)
from nyxion_heredity_sdk.networks import NetworkInterfaceAllocator as AllocatorContract


INTERFACE_ID = "1234567890abcdef"


class EmptyAccounts:
    def account_descriptors(self):
        return []

    def account_runtimes(self):
        return []


class NetworkAllocationContractTests(unittest.TestCase):
    def requirement(self, **changes):
        return NetworkInterfaceRequirement(**{
            "module_id": "example.module",
            "transport_types": ("http_server", "websocket_server"),
            "directions": ("call", "receive"),
            "account": "example:12345678",
            "purpose": "Local module account connection",
            "max_instances": 2,
        } | changes)

    def grant(self, **changes):
        return NetworkInterfaceGrant(**{
            "request_id": "request-1",
            "module_id": "example.module",
            "interface_id": INTERFACE_ID,
            "generation": 2,
            "transport_type": "http_server",
            "account": "example:12345678",
            "expires_at": 200,
            "grant_id": "grant-1",
        } | changes)

    def test_requirement_is_immutable_and_owns_its_capability_sequences(self):
        transport, directions = ["http_server"], ["receive"]
        value = self.requirement(transport_types=transport, directions=directions)
        transport.append("http_client")
        directions.append("call")
        self.assertEqual(value.transport_types, ("http_server",))
        self.assertEqual(value.directions, ("receive",))
        with self.assertRaises(FrozenInstanceError):
            value.module_id = "other.module"
        serialized = value.to_dict()
        serialized["transport_types"].append("websocket_client")
        self.assertEqual(value.transport_types, ("http_server",))

    def test_requirement_rejects_ambiguous_or_unbounded_capabilities(self):
        changes = [
            {"module_id": ""}, {"module_id": "Other.Module"},
            {"module_id": "bad/module"}, {"module_id": "m" * 129},
            {"transport_types": []}, {"transport_types": "http_server"},
            {"transport_types": ["http_server", "http_server"]},
            {"transport_types": ["raw_socket"]}, {"transport_types": [None]},
            {"directions": []}, {"directions": ["receive", "receive"]},
            {"directions": ["configure"]}, {"directions": "call"},
            {"account": ""}, {"account": "12345678"}, {"account": "example:with space"},
            {"account": {}}, {"account": "example:" + "a" * 129},
            {"purpose": "line\nbreak"}, {"purpose": "x" * 257},
            {"purpose": "\ud800"}, {"purpose": []},
            {"max_instances": True}, {"max_instances": 0}, {"max_instances": 65},
            {"max_interfaces": True}, {"max_interfaces": 0}, {"max_interfaces": 65},
        ]
        for fields in changes:
            with self.subTest(fields=fields), self.assertRaises(ContractError):
                self.requirement(**fields)

    def test_grant_is_not_a_network_credential_and_expiry_has_an_exact_boundary(self):
        grant = self.grant()
        self.assertFalse(grant.expired(199.999))
        self.assertTrue(grant.expired(200))
        self.assertTrue(grant.expired(201))
        self.assertFalse(self.grant(expires_at=None).expired(10**12))
        with self.assertRaises(FrozenInstanceError):
            grant.generation = 3
        fields = grant.to_dict()
        self.assertEqual(fields["grant_id"], "grant-1")
        self.assertFalse(set(fields) & {
            "access_token", "event_secret", "settings", "password", "session_key"})
        self.assertIsInstance(NetworkAllocationDenied(), ContractError)

    def test_grant_rejects_invalid_identity_generation_and_deadline(self):
        changes = [
            {"request_id": ""}, {"request_id": "invalid id"},
            {"module_id": "Uppercase"}, {"interface_id": "ABCDEF1234567890"},
            {"interface_id": "a" * 15}, {"interface_id": "g" * 16},
            {"generation": 0}, {"generation": True}, {"generation": 2**63},
            {"transport_type": "unknown"}, {"transport_type": []},
            {"account": "unscoped"}, {"expires_at": 0}, {"expires_at": True},
            {"expires_at": float("nan")}, {"expires_at": float("inf")},
            {"grant_id": ""}, {"grant_id": "invalid id"}, {"grant_id": []},
        ]
        for fields in changes:
            with self.subTest(fields=fields), self.assertRaises(ContractError):
                self.grant(**fields)

    def test_legacy_interface_service_does_not_require_allocator_methods(self):
        class ExistingProvider:
            def bind_module(self, module_id, interface_id):
                raise NotImplementedError
        provider = ExistingProvider()
        self.assertIsInstance(provider, NetworkInterfaces)
        self.assertNotIsInstance(provider, AllocatorContract)
        self.assertNotIsInstance(provider, NetworkInterfaceDeclarations)
        class SingleAllocationProvider:
            def declare_interface(self, requirement):
                raise NotImplementedError

            def request_interface(self, requirement, *, timeout=30.0):
                raise NotImplementedError

            def bind_grant(self, module_id, grant):
                raise NotImplementedError

            def release_interface(self, request_id):
                raise NotImplementedError
        single = SingleAllocationProvider()
        self.assertIsInstance(single, AllocatorContract)
        self.assertIsInstance(single, NetworkInterfaceDeclarations)
        self.assertNotIsInstance(single, NetworkInterfaceMultiAllocator)

    def test_public_allocation_contracts_import_without_host_or_dependencies(self):
        source = Path(__file__).resolve().parents[1] / "src"
        script = (
            "import sys;sys.path.insert(0,sys.argv[1]);"
            "from nyxion_heredity_sdk import NetworkInterfaceRequirement,"
            "NetworkInterfaceGrant,NetworkInterfaceDeclarations,NetworkInterfaceAllocator,"
            "NetworkInterfaceMultiAllocator;"
            "assert not any(n=='heredity' or n.startswith(('heredity.',"
            "'cryptography')) for n in sys.modules);"
            "assert not any(n.startswith('nyxion_heredity_') and "
            "not n.startswith('nyxion_heredity_sdk') for n in sys.modules);"
            "print('allocation-sdk-only')"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", script, str(source)],
            capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("allocation-sdk-only", result.stdout)


class NetworkAllocatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="NyXion allocator ")
        self.addCleanup(self.temporary.cleanup)
        self.manager = NetworkInterfaceManager(Path(self.temporary.name), EmptyAccounts())
        self.addCleanup(self.manager.close)
        self.allocator = NetworkInterfaceAllocator(self.manager)
        self.addCleanup(self.allocator.close)

    def interface(self, **changes):
        return self.manager.create({
            "name": "Configured local interface", "type": "http_server",
            "enabled": False, "account": None,
            "settings": {"host": "127.0.0.1", "port": 0},
        } | changes)

    def requirement(self, **changes):
        return NetworkInterfaceRequirement(**{
            "module_id": "example.module", "transport_types": ("http_server",),
        } | changes)

    def allocated(self, requirement=None, interface=None, *, allocator=None):
        allocator = allocator or self.allocator
        requirement = requirement or self.requirement()
        interface = interface or self.interface()
        declared = allocator.declare_interface(requirement)
        assigned = allocator.assign(declared["request_id"], interface["interface_id"])
        grant = NetworkInterfaceGrant(**assigned["grant"])
        return grant, allocator.bind_grant(requirement.module_id, grant)

    def test_declaration_is_fast_idempotent_and_does_not_select_or_create_an_interface(self):
        self.interface()
        with patch("socket.socket", side_effect=AssertionError("declaration opened a socket")):
            first = self.allocator.declare_interface(self.requirement())
            repeated = self.allocator.declare_interface(self.requirement())
        self.assertEqual(first, repeated)
        self.assertEqual(first["state"], "pending")
        self.assertIsNone(first["grant"])
        self.assertEqual(first["grants"], [])
        self.assertEqual(first["requirement"]["max_interfaces"], 16)
        self.assertEqual(len(self.manager.catalog()["interfaces"]), 1)
        first["requirement"]["transport_types"].clear()
        self.assertEqual(self.allocator.catalog()["requests"][0]["requirement"]["transport_types"],
                         ["http_server"])
        self.assertIsInstance(self.allocator, AllocatorContract)
        self.assertIsInstance(self.allocator.for_module("example.module"), NetworkInterfaceDeclarations)
        self.assertIsInstance(self.allocator.for_module("example.module"), NetworkInterfaceMultiAllocator)
        self.assertIsInstance(self.allocator.for_module("example.module"), NetworkInterfaceLifecycle)

    def test_concurrent_duplicate_declarations_keep_one_pending_request(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _: self.allocator.declare_interface(self.requirement()), range(32)))
        self.assertEqual(len({value["request_id"] for value in results}), 1)
        self.assertEqual(len(self.allocator.catalog()["requests"]), 1)

    def test_request_waits_for_explicit_assignment_and_timeout_preserves_pending_record(self):
        requirement = self.requirement()
        declared = self.allocator.declare_interface(requirement)
        with self.assertRaises(DeadlineExceeded):
            self.allocator.request_interface(requirement, timeout=0.005)
        self.assertEqual(self.allocator.catalog()["requests"][0]["state"], "pending")
        interface = self.interface()
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(self.allocator.request_interface, requirement, timeout=2)
            self.allocator.assign(declared["request_id"], interface["interface_id"])
            grant = waiting.result(timeout=2)
        self.assertEqual(grant.interface_id, interface["interface_id"])
        self.assertIs(self.allocator.request_interface(requirement, timeout=1), grant)
        for timeout in (0, -1, True, 121, float("nan"), float("inf"), "30"):
            with self.subTest(timeout=timeout), self.assertRaises(ContractError):
                self.allocator.request_interface(requirement, timeout=timeout)

    def test_deny_wakes_request_and_repeated_declarations_do_not_spam_requests(self):
        requirement = self.requirement()
        declared = self.allocator.declare_interface(requirement)
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(self.allocator.request_interface, requirement, timeout=2)
            denied = self.allocator.deny(declared["request_id"])
            with self.assertRaises(NetworkAllocationDenied):
                waiting.result(timeout=2)
        self.assertEqual(denied["state"], "denied")
        self.assertEqual(self.allocator.declare_interface(requirement)["request_id"],
                         declared["request_id"])

    def test_assignment_checks_transport_and_account_before_changing_resources(self):
        declared = self.allocator.declare_interface(self.requirement(account="example:12345678"))
        wrong_type = self.interface(type="http_client", settings={"url": "http://127.0.0.1:9"})
        wrong_account = self.interface(account="example:87654321")
        for interface in (wrong_type, wrong_account):
            with self.subTest(interface=interface["interface_id"]):
                with self.assertRaises(NetworkAllocationError):
                    self.allocator.assign(declared["request_id"], interface["interface_id"])
        self.assertEqual(self.allocator.catalog()["requests"][0]["state"], "pending")
        self.assertEqual(self.manager.get(wrong_account["interface_id"])["account"], "example:87654321")

    def test_assignment_can_bind_an_unclaimed_account_without_starting_the_interface(self):
        interface = self.interface()
        secret = self.manager.credentials(interface["interface_id"])["access_token"]
        declared = self.allocator.declare_interface(self.requirement(account="example:12345678"))
        assigned = self.allocator.assign(declared["request_id"], interface["interface_id"])
        current = self.manager.get(interface["interface_id"])
        self.assertEqual(current["account"], "example:12345678")
        self.assertEqual(current["state"], "stopped")
        self.assertEqual(assigned["grant"]["account"], "example:12345678")
        self.assertNotIn(secret, json.dumps(self.allocator.catalog()))
        self.assertFalse(hasattr(self.allocator.for_module("example.module"), "create"))

    def test_assignment_does_not_overwrite_a_concurrent_account_selection(self):
        interface = self.interface()
        declared = self.allocator.declare_interface(self.requirement(account="example:12345678"))
        original = self.manager.update

        def changed_before_claim(identifier, payload, **expected):
            original(identifier, {"account": "example:87654321"})
            return original(identifier, payload, **expected)

        with patch.object(self.manager, "update", side_effect=changed_before_claim):
            with self.assertRaises(NetworkBindingClosed):
                self.allocator.assign(declared["request_id"], interface["interface_id"])
        self.assertEqual(self.manager.get(interface["interface_id"])["account"], "example:87654321")
        self.assertEqual(self.allocator.catalog()["requests"][0]["state"], "pending")

    def test_stopped_assignment_survives_normal_start_and_stop(self):
        grant, binding = self.allocated()
        with self.assertRaises(BackendError):
            binding.call("get_status", {})
        self.manager.start(grant.interface_id)
        self.assertEqual(binding.call("get_status", {})["status"], "ok")
        self.manager.stop(grant.interface_id)
        self.assertFalse(binding.closed)
        self.assertEqual(self.allocator.catalog()["requests"][0]["state"], "granted")
        self.assertEqual(self.manager.get(grant.interface_id)["generation"], grant.generation)
        with self.assertRaises(BackendError):
            binding.call("get_status", {})

    def test_grant_rejects_other_modules_forged_fields_and_unknown_requests(self):
        grant, _ = self.allocated()
        with self.assertRaises(NetworkAllocationDenied):
            self.allocator.bind_grant("different.module", grant)
        for changes in (
                {"grant_id": "forged"}, {"generation": grant.generation + 1},
                {"account": "example:999"}, {"transport_type": "http_client"}):
            with self.subTest(changes=changes), self.assertRaises(NetworkBindingClosed):
                self.allocator.bind_grant(grant.module_id, replace(grant, **changes))
        with self.assertRaises(NetworkAllocationError) as failure:
            self.allocator.bind_grant(grant.module_id, replace(grant, request_id="unknown"))
        self.assertEqual(failure.exception.status, 404)

    def test_scoped_service_cannot_declare_bind_or_release_for_another_module(self):
        scoped = self.allocator.for_module("example.module")
        self.assertEqual(scoped.module_id, "example.module")
        other, _ = self.allocated(self.requirement(module_id="other.module"))
        with self.assertRaises(NetworkAllocationDenied):
            scoped.declare_interface(self.requirement(module_id="other.module"))
        with self.assertRaises(NetworkAllocationDenied):
            scoped.bind_grant("other.module", other)
        with self.assertRaises(NetworkAllocationDenied):
            scoped.bind_grant("example.module", other)
        with self.assertRaises(NetworkAllocationDenied):
            scoped.release_interface(other.request_id)
        declared = scoped.declare_interface(self.requirement())
        scoped.release_interface(declared["request_id"])
        self.assertEqual(self.allocator.catalog()["requests"][-1]["state"], "released")

    def test_direction_constraints_are_enforced_before_calling_the_interface(self):
        interface = self.interface(enabled=True)
        for allowed in ("call", "publish", "receive"):
            grant, binding = self.allocated(
                self.requirement(module_id="example." + allowed, directions=(allowed,)),
                interface)
            for operation, invoke in (
                    ("call", lambda: binding.call("get_status", {})),
                    ("publish", lambda: binding.publish_event({
                        "self_id": 123, "post_type": "meta_event", "time": 1,
                        "meta_event_type": "lifecycle", "sub_type": "enable"})),
                    ("receive", binding.receive)):
                with self.subTest(allowed=allowed, operation=operation):
                    if allowed == operation:
                        invoke()
                    else:
                        with self.assertRaises(NetworkAllocationDenied):
                            invoke()
            self.allocator.release_interface(grant.request_id)

    def test_subscription_limits_release_only_the_owning_request(self):
        interface = self.interface()
        grant, binding = self.allocated(interface=interface)
        other, observer = self.allocated(
            self.requirement(module_id="another.module"), interface)
        with self.assertRaises(NetworkAllocationDenied):
            self.allocator.bind_grant(grant.module_id, grant)
        binding.close()
        replacement = self.allocator.bind_grant(grant.module_id, grant)
        self.allocator.release_interface(grant.request_id)
        self.assertTrue(replacement.closed)
        self.assertFalse(observer.closed)
        message = self.manager.module_bus.emit(other.interface_id, "event", {"value": 1})
        self.assertIs(observer.receive(), message)
        self.assertTrue(self.manager.exists(other.interface_id))

    def test_config_update_retry_token_rotation_and_delete_revoke_grants(self):
        for operation in ("update", "retry", "regenerate_token", "delete"):
            with self.subTest(operation=operation):
                grant, binding = self.allocated(
                    self.requirement(module_id="example." + operation))
                if operation == "update":
                    self.manager.update(grant.interface_id, {"name": "Updated local interface"})
                else:
                    getattr(self.manager, operation)(grant.interface_id)
                with self.assertRaises(NetworkBindingClosed):
                    binding.receive()
                row = next(item for item in self.allocator.catalog()["requests"]
                           if item["request_id"] == grant.request_id)
                self.assertEqual(row["state"], "revoked")
                if self.manager.exists(grant.interface_id):
                    self.manager.stop(grant.interface_id)

    def test_reassignment_has_a_new_grant_identity_and_never_revives_an_old_handle(self):
        grant, binding = self.allocated()
        self.allocator.deny(grant.request_id)
        assigned = self.allocator.assign(grant.request_id, grant.interface_id)
        new = NetworkInterfaceGrant(**assigned["grant"])
        self.assertNotEqual(grant.grant_id, new.grant_id)
        self.assertEqual(grant.generation, new.generation)
        with self.assertRaises(NetworkBindingClosed):
            self.allocator.bind_grant(grant.module_id, grant)
        self.assertTrue(binding.closed)
        self.assertFalse(self.allocator.bind_grant(new.module_id, new).closed)

    def test_expiry_revokes_subscriptions_and_redeclaration_is_explicit(self):
        with patch("heredity.network_allocator.time.time", return_value=100) as clock:
            expiring = NetworkInterfaceAllocator(self.manager, grant_ttl=1)
            self.addCleanup(expiring.close)
            requirement = self.requirement()
            grant, binding = self.allocated(requirement, allocator=expiring)
            clock.return_value = 101
            with self.assertRaises(NetworkBindingClosed):
                binding.receive()
            self.assertTrue(binding.closed)
            self.assertEqual(expiring.catalog()["requests"][0]["state"], "expired")
            declared = expiring.declare_interface(requirement)
            expiring.close()
        self.assertEqual(declared["state"], "pending")
        self.assertNotEqual(declared["request_id"], grant.request_id)

    def test_release_module_and_close_wake_receivers_without_stopping_shared_resources(self):
        interface = self.interface()
        grant, binding = self.allocated(interface=interface)
        _, other = self.allocated(self.requirement(module_id="another.module"), interface)
        with ThreadPoolExecutor(max_workers=1) as pool:
            receiving = pool.submit(binding.receive, 10)
            self.allocator.release_module(grant.module_id)
            with self.assertRaises(NetworkBindingClosed):
                receiving.result(timeout=1)
        self.assertFalse(other.closed)
        self.allocator.close()
        self.allocator.close()
        self.assertTrue(other.closed)
        self.assertTrue(self.manager.exists(grant.interface_id))
        with self.assertRaises(NetworkBindingClosed):
            self.allocator.declare_interface(self.requirement())

    def test_bounded_request_storage_reclaims_only_terminal_records(self):
        bounded = NetworkInterfaceAllocator(self.manager, max_requests=1)
        self.addCleanup(bounded.close)
        first = bounded.declare_interface(self.requirement())
        with self.assertRaises(NetworkAllocationDenied):
            bounded.declare_interface(self.requirement(module_id="other.module"))
        bounded.release_interface(first["request_id"])
        next_request = bounded.declare_interface(self.requirement(module_id="other.module"))
        self.assertEqual(bounded.catalog()["requests"], [next_request])

    def test_released_module_scope_cannot_reopen_or_revoke_a_new_run(self):
        requirement = self.requirement()
        previous = self.allocator.for_module(requirement.module_id)
        previous.declare_interface(requirement)
        self.allocator.release_module(requirement.module_id)
        current = self.allocator.for_module(requirement.module_id)
        self.assertIsNot(current, previous)
        next_request = current.declare_interface(requirement)
        for operation in (
                lambda: previous.declare_interface(requirement),
                lambda: previous.request_interface(requirement, timeout=1),
                lambda: previous.release_interface(next_request["request_id"])):
            with self.assertRaises(NetworkBindingClosed):
                operation()
        self.assertEqual(self.allocator.catalog()["requests"][-1]["state"], "pending")

    def test_multi_assignment_preserves_unchanged_grants_bindings_and_explicit_order(self):
        requirement = self.requirement()
        first, second, third = self.interface(), self.interface(), self.interface()
        declared = self.allocator.declare_interface(requirement)
        assigned = self.allocator.assign_many(
            declared["request_id"], [first["interface_id"], second["interface_id"]])
        self.assertEqual(assigned["grant"], assigned["grants"][0])
        grants = self.allocator.request_interfaces(requirement, timeout=1)
        self.assertIsInstance(grants, tuple)
        self.assertEqual([grant.interface_id for grant in grants],
                         [first["interface_id"], second["interface_id"]])
        bindings = [self.allocator.bind_grant(requirement.module_id, grant) for grant in grants]
        with patch.object(self.manager, "start", side_effect=AssertionError("hot assignment started transport")), \
                patch.object(self.manager, "stop", side_effect=AssertionError("hot assignment stopped transport")):
            reconciled = self.allocator.assign_many(
                declared["request_id"], [second["interface_id"], third["interface_id"]])
        self.assertTrue(bindings[0].closed)
        self.assertFalse(bindings[1].closed)
        self.assertEqual(reconciled["grants"][0], grants[1].to_dict())
        self.assertEqual(reconciled["grant"], grants[1].to_dict())
        self.assertIs(self.allocator.request_interface(requirement, timeout=1), grants[1])
        self.assertEqual(self.manager.module_bus.bindings(second["interface_id"]),
                         [{"module_id": requirement.module_id, "subscriptions": 1}])
        event = self.manager.module_bus.emit(second["interface_id"], "event", {"still": "subscribed"})
        self.assertIs(bindings[1].receive(), event)

    def test_repeated_multi_selection_is_idempotent_and_does_not_rotate_authorizations(self):
        requirement = self.requirement()
        first, second = self.interface(), self.interface()
        declared = self.allocator.declare_interface(requirement)
        selected = [first["interface_id"], second["interface_id"]]
        assigned = self.allocator.assign_many(declared["request_id"], selected)
        granted = self.allocator.request_interfaces(requirement)
        bindings = [self.allocator.bind_grant(requirement.module_id, grant) for grant in granted]
        repeated = self.allocator.assign_many(declared["request_id"], tuple(selected))
        self.assertEqual(repeated["grants"], assigned["grants"])
        self.assertTrue(all(not binding.closed for binding in bindings))

    def test_legacy_assignment_replaces_all_previous_interfaces_with_exactly_one(self):
        requirement = self.requirement()
        first, second = self.interface(), self.interface()
        declared = self.allocator.declare_interface(requirement)
        self.allocator.assign_many(
            declared["request_id"], [first["interface_id"], second["interface_id"]])
        grants = self.allocator.request_interfaces(requirement)
        first_binding = self.allocator.bind_grant(requirement.module_id, grants[0])
        second_binding = self.allocator.bind_grant(requirement.module_id, grants[1])
        replaced = self.allocator.assign(declared["request_id"], second["interface_id"])
        self.assertEqual(replaced["grants"], [grants[1].to_dict()])
        self.assertTrue(first_binding.closed)
        self.assertFalse(second_binding.closed)

    def test_unassign_one_and_all_are_hot_and_reassignment_keeps_the_same_request(self):
        requirement = self.requirement()
        first, second = self.interface(enabled=True), self.interface(enabled=True)
        declared = self.allocator.declare_interface(requirement)
        self.allocator.assign_many(
            declared["request_id"], [first["interface_id"], second["interface_id"]])
        grants = self.allocator.request_interfaces(requirement)
        a, b = [self.allocator.bind_grant(requirement.module_id, grant) for grant in grants]
        observer = self.manager.bind_module("other.module", first["interface_id"])
        self.addCleanup(observer.close)
        with ThreadPoolExecutor(max_workers=1) as pool:
            receiving = pool.submit(a.receive, 10)
            removed = self.allocator.unassign(declared["request_id"], first["interface_id"])
            with self.assertRaises(NetworkBindingClosed):
                receiving.result(timeout=1)
        self.assertEqual(removed["state"], "granted")
        self.assertEqual(removed["grants"], [grants[1].to_dict()])
        self.assertFalse(b.closed)
        self.assertFalse(observer.closed)
        for item in (first, second):
            self.assertEqual(self.manager.get(item["interface_id"])["state"], "running")
        pending = self.allocator.unassign(declared["request_id"])
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(pending["grants"], [])
        self.assertIsNone(pending["grant"])
        self.assertTrue(b.closed)
        self.assertEqual(self.allocator.declare_interface(requirement)["request_id"],
                         declared["request_id"])
        repeated = self.allocator.unassign(declared["request_id"])
        self.assertEqual(repeated["state"], "pending")
        reassigned = self.allocator.assign(declared["request_id"], first["interface_id"])
        new = NetworkInterfaceGrant(**reassigned["grant"])
        self.assertNotEqual(new.grant_id, grants[0].grant_id)
        with self.assertRaises(NetworkBindingClosed):
            self.allocator.bind_grant(requirement.module_id, grants[0])
        self.assertFalse(self.allocator.bind_grant(requirement.module_id, new).closed)

    def test_scoped_multi_grants_release_only_own_interface_and_enforce_per_grant_limits(self):
        requirement = self.requirement(max_interfaces=2, max_instances=1)
        scope = self.allocator.for_module(requirement.module_id)
        declared = scope.declare_interface(requirement)
        first, second = self.interface(), self.interface()
        self.allocator.assign_many(
            declared["request_id"], [first["interface_id"], second["interface_id"]])
        grants = scope.request_interfaces(requirement)
        first_binding, second_binding = [scope.bind_grant(requirement.module_id, grant)
                                        for grant in grants]
        for grant in grants:
            with self.assertRaises(NetworkAllocationDenied):
                scope.bind_grant(requirement.module_id, grant)
        other, observer = self.allocated(
            self.requirement(module_id="other.module"), first)
        with self.assertRaises(NetworkAllocationDenied):
            scope.release_binding(other.request_id, first["interface_id"])
        scope.release_binding(declared["request_id"], first["interface_id"])
        scope.release_binding(declared["request_id"], first["interface_id"])
        self.assertTrue(first_binding.closed)
        self.assertFalse(second_binding.closed)
        self.assertFalse(observer.closed)
        self.assertEqual(scope.request_interfaces(requirement), (grants[1],))
        scope.release_interface(declared["request_id"])
        self.assertTrue(second_binding.closed)
        self.assertFalse(observer.closed)

    def test_partial_configuration_revocation_keeps_all_other_grants_active(self):
        for operation in ("update", "retry", "regenerate_token", "delete"):
            with self.subTest(operation=operation):
                requirement = self.requirement(module_id="multi." + operation)
                first, second = self.interface(), self.interface()
                declared = self.allocator.declare_interface(requirement)
                self.allocator.assign_many(
                    declared["request_id"], [first["interface_id"], second["interface_id"]])
                grants = self.allocator.request_interfaces(requirement)
                a, b = [self.allocator.bind_grant(requirement.module_id, grant) for grant in grants]
                if operation == "update":
                    self.manager.update(first["interface_id"], {"name": "Changed first"})
                else:
                    getattr(self.manager, operation)(first["interface_id"])
                row = next(item for item in self.allocator.catalog()["requests"]
                           if item["request_id"] == declared["request_id"])
                self.assertEqual(row["state"], "granted")
                self.assertEqual(row["grants"], [grants[1].to_dict()])
                self.assertIsNotNone(row["error"])
                with self.assertRaises(NetworkBindingClosed):
                    a.receive()
                self.assertFalse(b.closed)
                message = self.manager.module_bus.emit(second["interface_id"], "event", {"intact": True})
                self.assertIs(b.receive(), message)
                if self.manager.exists(first["interface_id"]):
                    self.manager.stop(first["interface_id"])

    def test_partial_expiry_does_not_expire_later_independent_grants(self):
        with patch("heredity.network_allocator.time.time", return_value=100) as clock:
            allocator = NetworkInterfaceAllocator(self.manager, grant_ttl=5)
            self.addCleanup(allocator.close)
            requirement = self.requirement()
            first, second = self.interface(), self.interface()
            declared = allocator.declare_interface(requirement)
            assigned = allocator.assign(declared["request_id"], first["interface_id"])
            first_grant = NetworkInterfaceGrant(**assigned["grant"])
            a = allocator.bind_grant(requirement.module_id, first_grant)
            clock.return_value = 102
            assigned = allocator.assign_many(
                declared["request_id"], [first["interface_id"], second["interface_id"]])
            self.assertEqual(assigned["grants"][0], first_grant.to_dict())
            second_grant = NetworkInterfaceGrant(**assigned["grants"][1])
            b = allocator.bind_grant(requirement.module_id, second_grant)
            clock.return_value = 105
            row = allocator.catalog()["requests"][0]
            self.assertEqual(row["state"], "granted")
            self.assertEqual(row["grants"], [second_grant.to_dict()])
            self.assertTrue(a.closed)
            self.assertFalse(b.closed)
            self.assertEqual(allocator.request_interfaces(requirement), (second_grant,))
            allocator.close()

    def test_expiration_revokes_account_routing_without_module_or_catalog_polling(self):
        self.assertIsNone(self.allocator._expiry_worker)
        allocator = NetworkInterfaceAllocator(self.manager, grant_ttl=0.05)
        self.addCleanup(allocator.close)
        grant, binding = self.allocated(allocator=allocator)
        self.assertEqual(self.manager.get(grant.interface_id)["allocation_owner_count"], 1)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.manager.get(grant.interface_id)["allocation_owner_count"] == 0:
                break
            threading.Event().wait(0.005)
        self.assertEqual(self.manager.get(grant.interface_id)["allocation_owner_count"], 0)
        self.assertEqual(self.manager._resources[grant.interface_id].backend.status()["state"],
                         "network_interface_unassigned")
        self.assertTrue(binding.closed)
        allocator.close()
        self.assertFalse(allocator._expiry_worker.is_alive())

    def test_shared_interface_route_remains_open_until_its_last_independent_grant_is_removed(self):
        interface = self.interface(enabled=True)
        first, first_binding = self.allocated(interface=interface)
        second, second_binding = self.allocated(
            self.requirement(module_id="other.module"), interface)
        current = self.manager.get(interface["interface_id"])
        self.assertTrue(current["allocation_controlled"])
        self.assertEqual(current["allocation_owner_count"], 2)
        self.allocator.unassign(first.request_id, interface["interface_id"])
        self.assertTrue(first_binding.closed)
        self.assertFalse(second_binding.closed)
        self.assertEqual(self.manager.get(interface["interface_id"])["allocation_owner_count"], 1)
        self.assertEqual(second_binding.call("get_status", {})["status"], "ok")
        self.allocator.unassign(second.request_id, interface["interface_id"])
        current = self.manager.get(interface["interface_id"])
        self.assertEqual(current["state"], "running")
        self.assertTrue(current["allocation_controlled"])
        self.assertEqual(current["allocation_owner_count"], 0)
        self.assertEqual(self.manager._resources[interface["interface_id"]].backend.status()["state"],
                         "network_interface_unassigned")

    def test_owner_registration_failure_rolls_back_only_new_owners_and_preserves_existing_grants(self):
        requirement = self.requirement()
        grant, binding = self.allocated(requirement)
        first, second = self.interface(), self.interface()
        original = self.manager.add_allocation_owner

        def unavailable(identifier, owner_id, revision, generation):
            if identifier == second["interface_id"]:
                raise NetworkBindingClosed("concurrent lifecycle change")
            return original(identifier, owner_id, revision, generation)

        with patch.object(self.manager, "add_allocation_owner", side_effect=unavailable):
            with self.assertRaises(NetworkBindingClosed):
                self.allocator.assign_many(grant.request_id, [
                    grant.interface_id, first["interface_id"], second["interface_id"]])
        self.assertEqual(self.allocator.request_interfaces(requirement), (grant,))
        self.assertFalse(binding.closed)
        self.assertEqual(self.manager.get(grant.interface_id)["allocation_owner_count"], 1)
        self.assertEqual(self.manager.get(first["interface_id"])["allocation_owner_count"], 0)
        self.assertEqual(self.manager.get(second["interface_id"])["allocation_owner_count"], 0)

    def test_late_invalid_selection_does_not_claim_accounts_or_revoke_existing_bindings(self):
        requirement = self.requirement(account="example:12345678")
        existing = self.interface(account=requirement.account)
        grant, binding = self.allocated(requirement, existing)
        unclaimed = self.interface()
        wrong_type = self.interface(type="http_client", settings={"url": "http://127.0.0.1:9"})
        before = self.manager.allocation_state(unclaimed["interface_id"])
        with patch.object(self.manager, "update", side_effect=AssertionError("invalid batch claimed account")):
            for invalid in (wrong_type["interface_id"], "a" * 16):
                if self.manager.exists(invalid):
                    expected = NetworkAllocationError
                else:
                    expected = ValueError
                with self.subTest(invalid=invalid), self.assertRaises(expected):
                    self.allocator.assign_many(grant.request_id, [
                        existing["interface_id"], unclaimed["interface_id"], invalid])
        self.assertEqual(self.manager.allocation_state(unclaimed["interface_id"]), before)
        self.assertEqual(self.allocator.request_interfaces(requirement), (grant,))
        self.assertFalse(binding.closed)

    def test_multi_selection_rejects_duplicates_bad_ids_and_count_limit(self):
        requirement = self.requirement(max_interfaces=1)
        declared = self.allocator.declare_interface(requirement)
        first, second = self.interface(), self.interface()
        for invalid in (
                first["interface_id"], None, [first["interface_id"], first["interface_id"]],
                ["bad-id"], [first["interface_id"], second["interface_id"]]):
            with self.subTest(invalid=invalid), self.assertRaises((ContractError, NetworkAllocationError)):
                self.allocator.assign_many(declared["request_id"], invalid)
        self.assertEqual(self.allocator.catalog()["requests"][0]["state"], "pending")

    def test_partial_account_claim_failure_keeps_existing_grants_without_overwriting_concurrent_edit(self):
        requirement = self.requirement(account="example:12345678")
        existing = self.interface(account=requirement.account)
        grant, binding = self.allocated(requirement, existing)
        first, second = self.interface(), self.interface()
        original = self.manager.update

        def concurrent_selection(identifier, payload, **expected):
            if identifier == second["interface_id"]:
                original(identifier, {"account": "example:87654321"})
            return original(identifier, payload, **expected)

        with patch.object(self.manager, "update", side_effect=concurrent_selection):
            with self.assertRaises(NetworkAllocationError) as failure:
                self.allocator.assign_many(grant.request_id, [
                    existing["interface_id"], first["interface_id"], second["interface_id"]])
        self.assertEqual(failure.exception.code, "NETWORK_ALLOCATION_CLAIM_INCOMPLETE")
        self.assertEqual(self.manager.get(first["interface_id"])["account"], requirement.account)
        self.assertEqual(self.manager.get(second["interface_id"])["account"], "example:87654321")
        self.assertEqual(self.allocator.request_interfaces(requirement), (grant,))
        self.assertFalse(binding.closed)

    def test_released_request_cannot_be_revived_through_assign_deny_or_unassign(self):
        requirement = self.requirement()
        grant, _ = self.allocated(requirement)
        self.allocator.release_interface(grant.request_id)
        for operation in (
                lambda: self.allocator.assign(grant.request_id, grant.interface_id),
                lambda: self.allocator.assign_many(grant.request_id, []),
                lambda: self.allocator.deny(grant.request_id),
                lambda: self.allocator.unassign(grant.request_id)):
            with self.assertRaises(NetworkAllocationError) as failure:
                operation()
            self.assertEqual(failure.exception.status, 409)
        self.assertEqual(self.allocator.catalog()["requests"][0]["state"], "released")
        self.assertNotEqual(self.allocator.declare_interface(requirement)["request_id"], grant.request_id)

    def test_inflight_call_keeps_original_interface_when_hot_assignment_replaces_its_grant(self):
        requirement = self.requirement(transport_types=("http_client",))
        first = self.interface(type="http_client", enabled=True, settings={"url": "http://127.0.0.1:9"})
        second = self.interface(type="http_client", enabled=True, settings={"url": "http://127.0.0.1:9"})
        grant, binding = self.allocated(requirement, first)
        entered, release, calls = threading.Event(), threading.Event(), []

        def delayed(action, params, *, timeout):
            calls.append((first["interface_id"], action))
            entered.set()
            if not release.wait(2):
                raise AssertionError("inflight fixture was not released")
            return {"status": "ok", "data": {"interface_id": first["interface_id"]}}

        def replacement(action, params, *, timeout):
            calls.append((second["interface_id"], action))
            return {"status": "ok", "data": {"interface_id": second["interface_id"]}}

        with patch.object(self.manager._resources[first["interface_id"]].transport,
                          "call_remote", side_effect=delayed), \
                patch.object(self.manager._resources[second["interface_id"]].transport,
                             "call_remote", side_effect=replacement):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(binding.call, "get_status", {})
                self.assertTrue(entered.wait(1))
                try:
                    assigned = self.allocator.assign(grant.request_id, second["interface_id"])
                    new_grant = NetworkInterfaceGrant(**assigned["grant"])
                    new_binding = self.allocator.bind_grant(requirement.module_id, new_grant)
                    current = new_binding.call("get_status", {})
                finally:
                    release.set()
                result = pending.result(timeout=2)
        self.assertEqual(result["data"]["interface_id"], first["interface_id"])
        self.assertEqual(current["data"]["interface_id"], second["interface_id"])
        self.assertEqual(calls, [(first["interface_id"], "get_status"),
                                 (second["interface_id"], "get_status")])
        self.assertTrue(binding.closed)


if __name__ == "__main__":
    unittest.main()
