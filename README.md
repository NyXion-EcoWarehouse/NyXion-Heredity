# NyXion Heredity

NyXion Heredity is a platform-independent plugin host, local management console,
OneBot gateway and capability broker.

The core provides account routing, network interfaces, plugin lifecycle management,
durable events, the module marketplace and the Aegis process/WebAssembly runtime.
Client discovery, platform authentication, native signing and platform protocols
belong to separately installed adapters. No platform adapter is bundled or registered
by default.

## Install and run

Python 3.11 or newer is required. From an extracted release directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --find-links wheels nyxion-heredity==26.11.13.post1
.\.venv\Scripts\heredity.exe app
```

On Linux use `.venv/bin/python` and `.venv/bin/heredity`. WebSockets and Wasmtime
dependencies are obtained through pip for the local operating system.

For development:

```powershell
.\.venv\Scripts\python.exe run.py app --market-offline
```

The default management address is `http://127.0.0.1:5099`. Copy `.env.example`
to `.env` to configure `HEREDITY_UI_PORT`. The generated password is stored in
`.heredity/host/ui-password.txt`. Existing configuration and account data are
private runtime state and must never be included in a release.

## Adapter integration

Install an adapter as a separate Python distribution. Adapters expose the
`nyxion_heredity.plugins` entry-point group and a packaged SDK manifest. The host
reads metadata without executing adapter code; starting an adapter is an explicit
operation. An optional `app --plugin-registry /path/to/registry.json` can select an
external source registry for development.

The SDK defines manifests, configuration, login interactions, session identities,
actions and events. Accounts are identified by `platform:self_id`; the host never
substitutes one platform namespace for another. Each adapter validates its own
platform-specific message data. The SDK validates bounded JSON, message structure
and generic transport fields while preserving extension data.

The management account endpoint is `/api/accounts`. Debug requests use an explicit
canonical `account` selector, preventing accounts with the same numeric ID on
different platforms from being confused.

## Network and module capabilities

New installations have no account instance and no default OneBot listener.
Create network interfaces and assign accounts through the management console.
The host supports HTTP, forward/reverse WebSocket, optional SSE and independent
per-interface credentials.

Marketplace modules declare requested capabilities and run through Aegis. Embedded
Python adapters are trusted code; they are not made into a security sandbox by
being loaded through the SDK.

See:

- [Architecture](docs/architecture.md)
- [Adapter contract](docs/plugin-architecture.md)
- [Network interfaces](docs/network-interfaces.md)
- [SDK](sdk/README.md)
- [Aegis](sdk/AEGIS.md)
- [Development handover](HANDOVER.md)

## Tests and release

```powershell
$env:PYTHONPATH='src;tests'
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests
pnpm --dir webui test
pnpm --dir webui typecheck
pnpm --dir webui build
.\.venv\Scripts\python.exe scripts/Build-Release.py --output dist/core-revision-1
```

The release builder creates only host, SDK and generic example artifacts.
Adapters maintain independent build and release pipelines. Always use a new output
directory; historical build output is not used as a source of truth.
