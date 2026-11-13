"""Execution provider descriptions and explicit unavailable integration points."""

from .contracts import ExecutionLevel, ProviderDescriptor, level_catalog
from .placeholders import (
    AttachedProcessProvider,
    CoreProtocolProvider,
    FullClientProvider,
    HostedKernelProvider,
    IndependentProtocolProvider,
    UiDriverProvider,
    placeholder_descriptors,
)

__all__ = [
    "ExecutionLevel",
    "ProviderDescriptor",
    "level_catalog",
    "placeholder_descriptors",
    "UiDriverProvider",
    "AttachedProcessProvider",
    "FullClientProvider",
    "CoreProtocolProvider",
    "HostedKernelProvider",
    "IndependentProtocolProvider",
]
