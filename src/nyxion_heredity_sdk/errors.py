"""Public errors shared by the host and independently packaged plugins."""


class ContractError(ValueError):
    """A message or declaration violates the SDK contract."""


class CompatibilityError(ContractError):
    """Host and plugin cannot negotiate a compatible SDK version."""


class SessionMismatch(ContractError):
    """An operation belongs to another account or a revoked session."""


class DeadlineExceeded(ContractError):
    """A local monotonic deadline expired; no replay is implied."""


class BackendError(Exception):
    """OneBot-compatible business failure, with optional completion evidence."""

    def __init__(self, message: str, retcode: int = 1400, *, details=None):
        super().__init__(message)
        self.retcode = retcode
        self.details = details


class PluginError(Exception):
    """Lifecycle failure carrying no native traceback or secret payload."""

    def __init__(self, message: str, code: str = "plugin_error"):
        super().__init__(message)
        self.code = code
