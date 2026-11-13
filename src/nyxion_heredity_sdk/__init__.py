"""Public, standard-library-only NyXion Heredity plugin SDK."""

from .contracts import (
    API_MAJOR, API_MINOR, CHALLENGE_KINDS, SESSION_STATES,
    ActionRequest, ActionResult, Challenge, EventEnvelope,
    Heartbeat, HostHello, Plugin, PluginContext, PluginHello, PluginManifest,
    PluginUIPage, UIPage, Pong, PublishAck, SessionIdentity, SessionUpdate, freeze_json, require_session,
    thaw_json, validate_action_result, validate_handshake, validate_pong,
)
from .errors import (
    BackendError, CompatibilityError, ContractError, DeadlineExceeded, PluginError,
    SessionMismatch,
)
from .config import validate_config, validate_config_schema
from .networks import (
    NETWORK_DIRECTIONS, NETWORK_MESSAGE_KINDS, NETWORK_TRANSPORT_TYPES,
    ModuleBinding, NetworkAllocationDenied, NetworkBindingClosed,
    NetworkInterfaceAllocator, NetworkInterfaceDeclarations, NetworkInterfaceGrant,
    NetworkInterfaceMultiAllocator,
    NetworkInterfaceLifecycle,
    NetworkInterfaceRequirement,
    NetworkInterfaces, NetworkMessage, NetworkMessageGap,
)
from .sandbox import SandboxModuleManifest

__version__ = "1.5.0"

__all__ = [
    "API_MAJOR", "API_MINOR", "CHALLENGE_KINDS", "SESSION_STATES",
    "ActionRequest", "ActionResult", "BackendError",
    "Challenge", "CompatibilityError", "ContractError", "DeadlineExceeded",
    "EventEnvelope", "Heartbeat", "HostHello", "Plugin", "PluginContext",
    "PluginError", "PluginHello", "PluginManifest", "PluginUIPage", "UIPage", "Pong", "PublishAck",
    "SessionIdentity", "SessionMismatch", "SessionUpdate", "freeze_json",
    "NETWORK_DIRECTIONS", "NETWORK_MESSAGE_KINDS", "NETWORK_TRANSPORT_TYPES",
    "ModuleBinding", "NetworkAllocationDenied", "NetworkBindingClosed",
    "NetworkInterfaceAllocator", "NetworkInterfaceDeclarations", "NetworkInterfaceGrant",
    "NetworkInterfaceMultiAllocator",
    "NetworkInterfaceLifecycle",
    "NetworkInterfaceRequirement", "NetworkInterfaces", "NetworkMessage",
    "NetworkMessageGap",
    "SandboxModuleManifest",
    "require_session", "thaw_json", "validate_action_result", "validate_config",
    "validate_config_schema", "validate_handshake", "validate_pong",
]
