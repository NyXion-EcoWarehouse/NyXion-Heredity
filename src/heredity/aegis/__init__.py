"""Aegis: separate-process Wasmtime modules with explicit host capabilities."""

from .broker import CapabilityBroker, PermissionViolation
from .runtime import AegisError, SandboxLimits, SandboxedModule


__all__ = ["AegisError", "CapabilityBroker", "PermissionViolation",
           "SandboxLimits", "SandboxedModule"]
