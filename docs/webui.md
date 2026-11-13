# Heredity WebUI

The React / Vite management interface provides account selection, plugin
instances, execution providers, network interfaces, OneBot debugging, logs,
settings, and marketplace modules. Plugins contribute structured configuration
and navigation metadata rather than executable frontend scripts.

## Startup

```powershell
$env:HEREDITY_WEBUI_PASSWORD = "replace-with-your-local-password"
.\.venv\Scripts\python.exe run.py app
```

The password must have at least 12 characters. The management API listens on
`127.0.0.1:5099` by default. A new install has no account or network listener.
Install a plugin and create its instance before selecting an account.

For a standalone gateway and management service:

```powershell
.\.venv\Scripts\python.exe run.py serve --sse
.\.venv\Scripts\python.exe run.py webui --gateway http://127.0.0.1:5700
```

`serve --demo` explicitly uses synthetic data. Demo does not establish a real
service connection or an execution-level implementation.

The management session, consumer access token, provider token, and execution
administration token have separate purposes. Browser requests use
`X-Heredity-Token`; a valid Bearer management session is also accepted.

## Accounts And Debugging

`GET /api/accounts` returns `{list:[...]}`. Each entry contains `account`,
`platform`, `self_id`, and `nickname`. Its account key is `platform:self_id`.
Generic external HTTP connections use `onebot:<self_id>` because their upstream
contract does not declare a platform identity.

Debug requests use:

```json
{"account":"example:123","action":"get_login_info","params":{}}
```

The server verifies the selected account and action parameters before dispatch.
Debug records retain the canonical `account` key. Write actions require an
explicit submission and are not executed by page loading.

## External OneBot Connection

The user supplies a known local HTTP(S) origin with an explicit port and an
optional upstream access token. Paths, queries, URL credentials, redirects,
proxies, and recursive connections to the host are rejected.

Probe calls only `get_login_info` and `get_status`. An explicit connect consumes
the short-lived probe and verifies the same account again. Only then does the
host create its provider, runtime, local history, and authenticated gateway.
A failed switch preserves the old connection and never replays business actions.

Upstream credentials stay in management-process memory and are not returned by
status endpoints or saved in configuration. A management restart requires a
new explicit probe and connection. Disconnect closes host-owned connections.

## Events And Health

Each external connection has its own reverse HTTP callback and signature secret.
The receiver requires
`X-Signature: sha1=<HMAC-SHA1(secret, original request bytes)>` and fixes events
to the current account and session epoch.

An open listener initially waits for upstream evidence. Only authenticated,
valid, persisted events establish ingress health. Duplicate events are not
stored twice. Host lifecycle and heartbeat events do not prove upstream
message delivery.

## Management API

All routes below require a management session.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/accounts` | Current canonical account projections |
| `POST /api/debug/invoke` | Explicit account-scoped action |
| `GET /api/heredity/plugins` | Plugin metadata and instance state |
| `GET /api/heredity/execution` | Generic provider descriptions and switch state |
| `POST /api/heredity/connection/probe` | Check the supplied local upstream |
| `POST /api/heredity/connection/connect` | Connect using a verified probe |
| `POST /api/heredity/connection/disconnect` | Restore the default gateway |
| `GET /api/heredity/connection` | Connection and ingress state |
| `GET /api/heredity/connection/gateway` | Explicitly reveal local consumer configuration |
| `GET /api/heredity/connection/events/config` | Explicitly reveal callback configuration |

## Frontend Development

From `webui/`, use the workspace package manager to run `pnpm dev`,
`pnpm typecheck`, `pnpm test`, and `pnpm build`. The development server normally
listens on `127.0.0.1:5178` and proxies `/api` to the management API.
Production assets are served by the management service.

Backend integration tests use isolated local HTTP fixtures. They do not contact
real accounts or send messages to external services.
