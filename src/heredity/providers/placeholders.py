"""Unavailable entry points for separately implemented execution providers."""

from __future__ import annotations

from ..backends.base import BackendError
from .contracts import ExecutionLevel, ProviderDescriptor


_DESCRIPTORS = (
    ProviderDescriptor(
        provider_id="l0-ui",
        label="L0 界面驱动",
        execution_level=ExecutionLevel.L0,
        source_kind="native",
        host_kind="official_gui",
        session_owner="official_client",
        target_memory_access="read",
        in_process_code=False,
        business_request_builder="official_services",
        primary_transport_owner="official_client",
        availability="unimplemented",
        reason="界面驱动由外部适配器实现；本体不提供客户端观察、输入驱动或动作执行。",
        level_provenance="declared",
    ),
    ProviderDescriptor(
        provider_id="l1-attached",
        label="L1 进程附着",
        execution_level=ExecutionLevel.L1,
        source_kind="native",
        host_kind="attached_client",
        session_owner="official_client",
        target_memory_access="write",
        in_process_code=True,
        business_request_builder="mixed",
        primary_transport_owner="official_client",
        reason="进程附着由外部适配器实现；本体不加载、附着或注入客户端进程。",
        level_provenance="declared",
    ),
    ProviderDescriptor(
        provider_id="l2-client",
        label="L2 完整客户端",
        execution_level=ExecutionLevel.L2,
        source_kind="native",
        host_kind="full_client",
        session_owner="heredity",
        target_memory_access="none",
        in_process_code=False,
        business_request_builder="heredity",
        primary_transport_owner="heredity",
        reason="完整客户端由外部适配器实现；本体仅接收已验证的会话、动作与事件。",
        level_provenance="declared",
    ),
    ProviderDescriptor(
        provider_id="l3-core",
        label="L3 协议核心",
        execution_level=ExecutionLevel.L3,
        source_kind="native",
        host_kind="protocol_core",
        session_owner="heredity",
        target_memory_access="none",
        in_process_code=False,
        business_request_builder="heredity",
        primary_transport_owner="heredity",
        reason="协议核心由外部适配器实现；本体不包含平台协议或登录实现。",
        level_provenance="declared",
    ),
)


def placeholder_descriptors() -> tuple[ProviderDescriptor, ...]:
    return _DESCRIPTORS


class _UnavailableExecutionProvider:
    _descriptor: ProviderDescriptor

    def __init__(self, self_id: int = 0) -> None:
        if type(self_id) is not int or self_id < 0:
            raise ValueError("self_id must be a nonnegative integer.")
        self._self_id = self_id
        self._closed = False

    @property
    def self_id(self) -> int:
        return self._self_id

    @property
    def online(self) -> bool:
        return False

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def call(self, action, params):
        descriptor = self.descriptor
        message = (
            f"Provider {descriptor.provider_id} is closed."
            if self._closed else descriptor.reason
        )
        raise BackendError(
            message,
            1503,
            details={
                "provider_id": descriptor.provider_id,
                "execution_level": descriptor.execution_level.value,
                "availability": descriptor.availability,
                "reason": descriptor.reason,
                "selectable": descriptor.selectable,
                "implemented": False,
                "action_completed": False,
                "closed": self._closed,
            },
        )

    def close(self) -> None:
        self._closed = True


class UiDriverProvider(_UnavailableExecutionProvider):
    """Unavailable slot for an adapter-owned UI execution provider."""

    _descriptor = _DESCRIPTORS[0]


class AttachedProcessProvider(_UnavailableExecutionProvider):
    """Unavailable slot for an adapter-owned process execution provider."""

    _descriptor = _DESCRIPTORS[1]


class FullClientProvider(_UnavailableExecutionProvider):
    """Unavailable slot for an adapter-owned complete client."""

    _descriptor = _DESCRIPTORS[2]


class CoreProtocolProvider(_UnavailableExecutionProvider):
    """Unavailable slot for an adapter-owned protocol implementation."""

    _descriptor = _DESCRIPTORS[3]


HostedKernelProvider = FullClientProvider
IndependentProtocolProvider = CoreProtocolProvider
