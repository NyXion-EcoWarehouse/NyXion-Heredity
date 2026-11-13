"""Stable OneBot listener over an explicitly selected plugin account."""

import threading

from nyxion_heredity_sdk import BackendError


class AccountRoutingGate:
    """Keep allocation ownership separate from account and listener settings."""

    def __init__(self, *, controlled=False):
        self._lock = threading.RLock()
        self._controlled = controlled
        self._owners = set()
        self._epoch = 1 if controlled else 0
        self._runtime = None
        self._floor = 0

    def snapshot(self):
        with self._lock:
            return {"controlled": self._controlled, "owners": len(self._owners),
                    "allowed": not self._controlled or bool(self._owners),
                    "epoch": self._epoch}

    def add(self, owner, runtime):
        with self._lock:
            reset = not self._controlled or not self._owners
            self._controlled = True
            self._owners.add(owner)
            if reset:
                self._epoch += 1
                self._runtime = runtime
                self._floor = runtime.event_hub.latest_sequence if runtime is not None else 0
            return reset

    def remove(self, owner):
        with self._lock:
            if owner not in self._owners:
                return False
            self._owners.remove(owner)
            if self._owners:
                return False
            self._epoch += 1
            self._runtime, self._floor = None, 0
            return True

    def clear(self):
        with self._lock:
            if self._controlled:
                self._owners.clear()
                self._epoch += 1
                self._runtime, self._floor = None, 0

    def start_cursor(self, runtime):
        with self._lock:
            return self._floor if runtime is self._runtime else 0


class PluginGatewayBackend:
    def __init__(self, host, *, account=None, routing_gate=None):
        if account is not None and (not isinstance(account, str) or account.count(":") != 1):
            raise ValueError("account selector must be platform:self_id")
        self.host = host
        self.account = account
        self._lock = threading.RLock()
        self.routing_gate = routing_gate

    def routing_allowed(self):
        return self.routing_gate is None or self.routing_gate.snapshot()["allowed"]

    def event_route_stamp(self):
        return self.routing_gate.snapshot()["epoch"] if self.routing_gate is not None else 0

    def event_start_cursor(self, runtime):
        return self.routing_gate.start_cursor(runtime) if self.routing_gate is not None else 0

    def event_route_current(self, runtime, stamp):
        return (self.event_route_stamp() == stamp and self.event_runtime() is runtime
                and self.event_route_stamp() == stamp)

    def selection(self):
        with self._lock:
            return {"account": self.account, "accounts": self.host.account_descriptors(),
                    "state": self.status()["state"]}

    def select_account(self, account):
        if account is not None:
            if (not isinstance(account, str) or account.count(":") != 1
                    or any(ord(char) < 33 for char in account)):
                raise ValueError("Account selector must be platform:self_id.")
            available = {f"{item['platform']}:{item['self_id']}"
                         for item in self.host.account_descriptors()}
            if account not in available:
                raise ValueError("Select an online plugin account.")
        with self._lock:
            self.account = account
            return self.selection()

    def selected_runtime(self):
        sessions = self.host.account_runtimes()
        with self._lock:
            account = self.account
        if account is not None:
            sessions = [(identity, runtime) for identity, runtime in sessions
                        if f"{identity.platform}:{identity.self_id}" == account]
        return sessions[0][1] if len(sessions) == 1 else None

    def event_runtime(self):
        return self.selected_runtime() if self.routing_allowed() else None

    @property
    def self_id(self):
        runtime = self.event_runtime()
        return runtime.self_id if runtime is not None else 0

    @property
    def online(self):
        runtime = self.event_runtime()
        return runtime.online if runtime is not None else False

    @property
    def event_hub(self):
        runtime = self.event_runtime()
        return runtime.event_hub if runtime is not None else None

    def status(self):
        runtime = self.event_runtime()
        if runtime is not None:
            return runtime.status()
        if not self.routing_allowed():
            return {"online": False, "good": False, "self_id": 0,
                    "state": "network_interface_unassigned", "mode": "plugin",
                    "identity_verified": False, "receive_healthy": None}
        sessions = self.host.account_runtimes()
        return {"online": False, "good": False, "self_id": 0,
                "state": "account_selection_required" if len(sessions) > 1 else "waiting_for_plugin",
                "mode": "plugin", "identity_verified": False, "receive_healthy": None}

    def execution_info(self):
        runtime = self.event_runtime()
        if runtime is not None:
            return runtime.execution_info()
        return {"levels": [], "switch_supported": False,
                "state": {"state": self.status()["state"], "active_provider_id": None,
                          "session_epoch": None, "descriptor": None, "providers": []}}

    def call(self, action, params):
        if action == "get_plugin_info":
            return self.host.catalog()
        if action == "get_execution_info":
            return self.execution_info()
        runtime = self.event_runtime()
        if runtime is None:
            if not self.routing_allowed():
                raise BackendError("Network interface is not assigned to an active module.", 1503,
                                   details={"state": "network_interface_unassigned"})
            raise BackendError("No unique online plugin account is selected.", 1503)
        return runtime.call(action, params)

    def emit_meta(self, fields):
        runtime = self.event_runtime()
        if runtime is not None:
            runtime.emit_meta(fields)

    def close(self):
        pass
