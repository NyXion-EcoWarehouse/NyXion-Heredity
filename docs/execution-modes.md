# Execution Modes

The core exposes generic plugin and provider slots. A slot's metadata describes
its execution boundary; it does not install an engine or establish account
authentication.

## Plugin Execution

| Mode | Boundary |
| --- | --- |
| `embedded` | A trusted Python adapter runs in the host process through the SDK |
| Aegis WebAssembly | A marketplace module runs in a worker with granted capabilities |

An embedded adapter has the host's process permissions. It must not be described
as Aegis-isolated. Aegis module permissions for files, networks, and the bus are
independent of the account and interface selected by the user.

## Provider Descriptors

The generic provider contract retains optional mechanism descriptors:

| Level | Mechanism | Session And Transport Ownership |
| --- | --- | --- |
| L0 | UI driver | An external client owns the session and primary transport |
| L1 | Attached extension | An external client owns the session and primary transport |
| L2 | Full client | The implementation owns the session and primary transport |
| L3 | Minimal protocol core | The implementation owns the session and primary transport |

The core supplies metadata and unavailable placeholders for these levels.
Platform engines must be supplied separately. A configured external HTTP
provider's level is a declaration and remains marked `declared`; successful
account verification does not verify its implementation mechanism.

Demo providers have no execution level. Supported actions, event sources,
memory access, and receipt evidence are independent fields and remain subject
to consistency validation.

## Configuration And Sessions

Platform configuration is adapter-owned schema data. The core renders that
schema and stores instance configuration without interpreting platform-specific
protocol or client-build selections.

Each account instance has an independent state directory. The SDK session uses
`platform`, `self_id`, and an opaque epoch. Account selection is expressed as
`platform:self_id`; it is separate from interface allocation and module grants.

See [Plugin Architecture](plugin-architecture.md),
[Providers](providers.md), and [Network Interfaces](network-interfaces.md).
