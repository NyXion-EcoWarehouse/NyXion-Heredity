# Core Architecture

NyXion Heredity is a platform-neutral host. It manages plugin lifecycle,
account routing, network interfaces, message history, and the management UI.
Platform authentication, client protocols, native components, device state, and
platform-specific message validation belong to separately installed adapters.

## Request And Event Flow

```text
Application
  <-> Host network interface
  <-> Authorization and account routing
  <-> Public SDK
  <-> Installed adapter
  <-> External service
```

The core discovers plugin metadata before it runs a plugin factory. An explicit
start, or a saved instance with automatic startup enabled, begins the plugin
lifecycle. Discovery of metadata does not establish an authenticated account.

Each SDK account uses `platform:self_id` as its routing identity. Multiple
instances of one plugin have separate configuration, state directories, and
session epochs. An action keeps the account and session selected when it began.
Changing the selected UI account does not redirect an action already in flight.

## Core Components

| Component | Responsibility |
| --- | --- |
| `application.py` | Compose the host and restore configured instances |
| `plugins/host.py` | Metadata discovery, handshake, lifecycle, and instance state |
| `plugins/gateway.py` | Project SDK sessions and route account actions |
| `network_interfaces.py` | Persistent interface identity and configuration |
| `network_allocator.py` | Module requirements, grants, and revocation |
| `network_bus.py` | Authorized action and event delivery |
| `runtime.py`, `storage.py` | Runtime checks, message history, and event persistence |
| `providers/` | Generic execution descriptors and controlled provider switching |
| `webui.py` | Authenticated management API and static UI |
| `analysis/` | Bounded offline analysis of explicitly supplied inputs |

The host has no built-in platform adapter or default account. A fresh install
also has no default network listener. Users install plugins, create account
instances, and allocate network interfaces explicitly.

## Execution Boundaries

Trusted Python adapters use the SDK's `embedded` execution model. They run in the
host process and have its operating-system permissions. A plugin's private worker
may isolate crashes or lifecycle handling, but does not give the plugin a
general-purpose security sandbox.

Marketplace WebAssembly modules use Aegis workers with bounded SDK capabilities.
Filesystem, network, and bus permissions are granted separately. See
[`plugin-architecture.md`](plugin-architecture.md),
[`execution-modes.md`](execution-modes.md), and
[`../sdk/AEGIS.md`](../sdk/AEGIS.md).

## Failure And Evidence

Host handshake health, adapter account state, upstream receipt evidence, and
event-ingress health are distinct observations. An online flag alone does not
prove a message was delivered or rendered.

The host does not replay business actions after timeouts, reconnects, or
persistence failures. Confirmed sends with local persistence failures retain
their completion details. Unsupported actions and unavailable providers fail
explicitly rather than falling back to demonstration data.

HTTP event delivery is best effort. The online SSE window is bounded and does
not provide a durable delivery queue. Persistent history and session epochs
protect ownership but do not guarantee upstream delivery.

## Further Reading

- [Account Runtime](runtime.md)
- [Execution Providers](providers.md)
- [Network Interfaces](network-interfaces.md)
- [Marketplace](marketplace.md)
- [Settings](settings.md)
