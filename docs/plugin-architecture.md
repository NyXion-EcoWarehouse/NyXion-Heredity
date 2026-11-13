# Plugin Architecture

The host, public SDK, and independently installed adapters have separate package
boundaries. The SDK is the public contract between a plugin and the host.

```text
Management UI and network interfaces
                  |
              Core host
                  |
              Public SDK
                  |
       Independently installed plugins
```

## Discovery And Startup

The host reads plugin metadata without executing the plugin factory. A registry
or an installed package can describe a plugin. The core ships no registered
platform plugin and creates no default account.

An explicit start creates an instance, performs the local SDK handshake, and
passes the instance configuration and state directory to the plugin. Saved
instances with automatic startup enabled may be restored on host startup.
The plugin owns authentication and must report its actual account state.

Plugins import `nyxion_heredity_sdk`, not host internals. They do not create the
host's UI or OneBot listeners, import host database classes, or share private
host locks. Adapter-specific native components and configuration stay within
the adapter's own project and local state.

## Session And Account Routing

SDK identity consists of `platform`, `self_id`, and `epoch`. The public account
key is `platform:self_id`, for example `example:123`. This example is synthetic;
an installed adapter must report its own real platform identity.

The current host projects online sessions into OneBot v11 immediately. Online
`self_id` values therefore must be canonical positive integer strings within the
signed 64-bit range. The SDK DTO itself can represent other strings; an adapter
whose native identity is nonnumeric must own an explicit, stable transport mapping.
The core never guesses or rewrites a platform's identity.

Multiple instances of one plugin have separate instance IDs, saved
configuration, state, session epochs, and errors. Selecting an account in the
UI does not merge their state or redirect an action already in progress.

```powershell
python run.py app --account example:123
```

This selects an existing account. It neither installs a plugin nor creates an
authenticated session.

The SDK distinguishes local handshake health from account state, and supports
states such as `starting`, `login_required`, `online`, `offline`, and `failed`.
The host displays reported state without inferring platform authentication
from a responding process.

## UI And Permissions

A plugin declares structured metadata, configuration fields, navigation, and
account views. It cannot inject arbitrary executable frontend scripts.
The host validates plugin routes, instance IDs, input schemas, and authenticated
management actions.

Network listeners belong to the host. Modules declare required interfaces
through the SDK; the host grants, revokes, and persists assignments. Account
selection and interface grants are separate operations.

Trusted Python plugins use `embedded` execution. Marketplace WebAssembly modules
use Aegis capabilities and workers. Native workers owned by an embedded plugin
do not give the plugin Aegis security guarantees.

## Message Boundary

The SDK checks message structure, JSON size, text fields, and reply identifiers.
Extension segments and their platform-specific payloads remain opaque.
Each adapter validates its own identifiers and supported message features.
Neither the SDK nor the host guesses protocol-specific targets from a generic
message segment.

## Independent Distribution

The core depends on `nyxion-heredity-sdk>=1.5,<2`. The SDK runtime uses only the
standard library. An adapter declares the SDK and any private implementation
dependencies in its own package.

The core release contains the core wheel, SDK artifacts, production UI, generic
configuration, and the SDK example. It contains no platform engine, native
platform components, private session state, or bundled adapter registration.

See [SDK](../sdk/README.md), [Network Contract](../sdk/NETWORKS.md),
[Aegis](../sdk/AEGIS.md), and [WebUI](webui.md).

## Dispatch Measurement

```powershell
.\.venv\Scripts\python.exe scripts/Benchmark-PluginBus.py --iterations 5000
```

This measures local dispatch with the SDK's offline example plugin. It excludes
upstream networks, native workers, persistent transactions, and HTTP transport,
and does not establish end-to-end throughput or delivery guarantees.
