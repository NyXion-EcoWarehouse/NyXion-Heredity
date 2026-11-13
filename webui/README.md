# NyXion Heredity WebUI

NyXion Heredity's React management console. Runtime and build inputs are
self-contained in this directory.
The current host release is `Alpha Build 26.11.13 (Core Revision 1)` with machine
version `26.11.13.post1`; the frontend package retains version `26.11.13`.
Display text prefers the host's `version_label`
while older hosts retain their semantic version label.

## Run

Use Node `^24.22.1 || >=26.12.1` and pnpm `11.25.0`.

```powershell
pnpm dev
```

The UI listens on `http://127.0.0.1:5178` (or the next free port), proxying `/api`
to `http://127.0.0.1:5099`. Set `HEREDITY_WEBUI_BACKEND` or `HEREDITY_WEBUI_PORT`
to change these endpoints. Existing installed dependencies can be reused.

On this machine:

```powershell
$env:PATH = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin;$env:PATH"
pnpm dev
```

Authentication uses `POST /api/login` and `X-Heredity-Token`. The overview reads
generic host resources, registered plugins and accounts. Platform authentication
and communication are supplied by plugins.

The `/debug` page selects an identity from `GET /api/accounts`, whose entries
contain `account`, `platform`, `self_id` and `nickname`. Requests to
`POST /api/debug/invoke` contain `{"account": "example:12345678", "action": "...",
"params": {...}}`. Debug actions run only on explicit submission; write actions
require an additional confirmation checkbox and a named write/submit button.
Plugin account identities retain the platform supplied by the plugin. A generic
external OneBot service without platform metadata uses the `onebot` namespace.

Gateway credentials and event callback signing secrets are fetched only on the
corresponding operator command and displayed as masked fields. Enabling a
callback listener does not imply receipt of upstream events.

## Plugins

The `/plugins` page uses the host's real plugin catalog. Standalone `webui`
without an injected plugin host reports that the host is not running and does
not invent a registered plugin or online account. The application owner passes
`plugin_host` into `create_webui_server`.

The authenticated management API is:

```text
GET  /api/heredity/plugins
POST /api/heredity/plugins/{id}/start       {"config": {...}}
POST /api/heredity/plugins/{id}/stop        {}
GET  /api/heredity/plugins/{id}/challenge
GET  /api/heredity/plugins/{id}/instances
POST /api/heredity/plugins/{id}/instances   {"instance_key": "...", "display_name": "..."}
POST /api/heredity/plugins/{id}/instances/{key}/start  {"config": {...}}
POST /api/heredity/plugins/{id}/instances/{key}/stop   {}
POST /api/heredity/plugins/{id}/instances/{key}/restart {}
GET  /api/heredity/plugins/{id}/instances/{key}/challenge
DELETE /api/heredity/plugins/{id}/instances/{key}
GET  /api/heredity/account-selection
POST /api/heredity/account-selection       {"account": "example:12345678"}
```

Catalog and challenge reads never start a plugin. Plugin start/stop requires
the existing management token and rejects foreign browser origins. The
standard Vite origin on port 5178 is allowed; other development ports must
serve the built UI from the management server for mutations.

The console presents host services and registered plugin pages through the same
navigation. It has no built-in platform client process discovery.

Configuration fields come from the plugin's schema. Secret fields are not
prefilled or stored in browser storage and are cleared after a successful start.
Host heartbeat health and platform account state are separate UI values.
QR challenges are fetched using the authenticated API and accept bounded PNG
data only; plugin-local paths, login URLs and tokens are not exposed in that
challenge response. Login material disappears when the platform becomes online.
All SDK challenge kinds (`qr`, `input`, `notice`) and the prelogin `starting`
state are accepted. A platform notice displays its actual message instead of
being reported as an invalid interaction.

Plugins register controlled child pages through manifest `ui_pages`. The host
supplies account-page rendering; plugins cannot load arbitrary JavaScript,
HTML or external routes into the sidebar. A plugin can register
`/plugins/example.im/accounts`. This page creates separate logical account
instances, each with its own configuration, login challenge and start/stop
controls. Adding an account creates metadata only and does not initiate login.
Removing a stopped account does not remove the plugin or another account.
The visible Restart control restarts one instance with its saved configuration.
The plugin schema supplies any automatic-login toggle. Integer identity inputs
use a numeric keyboard without increment/decrement spinners.
Saved non-secret values refill the account's controls after a page reload.
Polling updates the saved baseline without replacing fields edited locally.
Plugin-registered navigation entries appear immediately above the Plugins item
with the same left alignment. The first registration order remains stable
across subsequent catalog polls. The header and footer stay within the viewport;
the central content area scrolls independently and restores its position when
returning to a route.

Several online accounts require an explicit OneBot account selection. The
account page can select one online identity or restore automatic selection
with `{"account": null}`; automatic selection only resolves a unique account.
The UI never broadcasts an outgoing action to all accounts.

## Execution Providers

The `/execution` page renders the generic provider catalog reported by the host.
Provider declarations, implementation evidence and availability remain distinct;
the console does not infer support from a level name. A demonstration provider
remains outside L0-L3. Switch requests include the current session epoch and are
never automatically retried.

## External Connections

The `/external-connections` page connects an explicitly configured local OneBot
HTTP service. The operator enters an origin and optional access token, verifies
the identity through read-only login/status requests, then confirms the detected
account. A successful probe does not connect automatically. It uses the generic
`/api/heredity/connection` resource and its `/probe`, `/connect`, `/disconnect`,
`/gateway` and `/events/config` operations. Credentials are fetched only by the
corresponding operator command and are displayed in masked fields.

## Host Connections

The `/connections` page now manages the Heredity host's independent network
interfaces. Each interface receives a unique random 16-character lowercase
hexadecimal ID from the host. The ID is stable across editing and restarts,
and modules use it to bind to the corresponding interface. It is an addressable
resource identifier, not an authentication token or a platform-plugin feature.

```text
GET    /api/heredity/network-interfaces
POST   /api/heredity/network-interfaces
GET    /api/heredity/network-interfaces/{interface_id}
PUT    /api/heredity/network-interfaces/{interface_id}
POST   /api/heredity/network-interfaces/{interface_id}/start
POST   /api/heredity/network-interfaces/{interface_id}/stop
POST   /api/heredity/network-interfaces/{interface_id}/retry
GET    /api/heredity/network-interfaces/{interface_id}/credentials
POST   /api/heredity/network-interfaces/{interface_id}/regenerate-token
DELETE /api/heredity/network-interfaces/{interface_id}
GET    /api/heredity/network-requests
GET    /api/heredity/network-requests?include_history=true
POST   /api/heredity/network-requests/{request_id}/assign
POST   /api/heredity/network-requests/{request_id}/unassign
POST   /api/heredity/network-requests/{request_id}/auto-start
POST   /api/heredity/network-requests/{request_id}/reapply
```

The interface list supports creating, configuring, starting, stopping, deleting
and copying interface IDs. A new interface is stopped until explicitly started.
Transport choices come from the host catalog: HTTP server/client and forward/
reverse WebSocket. The configuration dialog renders the corresponding listener
address, port, path or destination URL, plus account binding and heartbeat.
An account can be selected from online identities or entered explicitly while
offline. Editing a running interface rebuilds its connection while preserving
the ID. Interface retry reconnects that resource once.

Tokens are generated by the host. Configuration payloads cannot supply or
clear an access token or separate event-signing key. The explicit credential
command reads one interface's token through the authenticated API and displays
it masked; the operator can reveal, copy or regenerate it. Event signing uses
the same token. Public catalog snapshots contain status flags only.

The module-request section displays declarations independently of platform
plugins. The operator selects compatible existing interfaces with checkboxes
and saves the complete desired set through `{"interface_ids": [...]}`.
Unchanged grants retain their identities. Each grant can be revoked separately
with `{"interface_id": "..."}`; an empty unassignment body revokes all grants.
Saving an empty selection also revokes all grants and leaves the same request
pending. The operator can edit existing allocations without restarting a
platform account. Polling preserves unsaved selections.

Denied, revoked and expired requests may be assigned again. A released request
can request a new declaration while its module remains active; the old grant
is never revived. The UI disables reapplication for a stopped module. A current
failed automatic restore can be retried explicitly. The UI displays every active
grant, honors `max_interfaces` separately from subscription limits, and filters
by transport and account. An unscoped interface may be bound to the module's
requested account by the host. Declarations do not create new listeners or choose
unconfigured ports. With no matching interfaces, the UI asks the operator to
create one first.

Automatic startup is controlled per module/account through an authenticated
`{"enabled": true|false}` mutation and defaults to enabled. Only interface IDs
chosen and saved explicitly are restored when that module starts again.
Missing or incompatible saved IDs leave the request pending with a retry
control; the host does not select replacements. Unsaved checkbox changes must
be saved before changing the policy. Current requests occupy the primary view;
superseded requests and released duplicates move into a collapsed history.
Full history is fetched only when its section opens.
Interface declarations have no rejection control in the console. Explicit
grant removal remains available through single-interface and all-interface
unassignment.

The host persists interface configuration in local state. Enabled interfaces
are restored on startup; a new host starts with no network interfaces. Platform
automatic login is controlled separately by each account's saved setting.

`tests/network-interfaces.test.mjs` covers strict metadata and mutation payloads.
`tests/network-interfaces.browser.mjs` starts a bounded real application with an
offline SDK account and a bound module, exercises interface CRUD and an actual
local HTTP action, and checks desktop/mobile layouts. It uses offline platform
fixtures and sends no real message. The older host-connection settings component and tests
remain for compatibility; they are no longer the `/connections` primary view.
The supplied Heredity SVG is used by the favicon and application logo without
recoloring it. Theme tokens use cyan and blue for primary actions, neutral
surfaces, and restrained magenta selection markers. Colors follow the system's
light or dark appearance, including live changes.
`tests/network-multi-assignment.browser.mjs` verifies real multi-interface
assignment and revocation, unchanged grants, live account continuity, unsaved
selection retention, light/dark text contrast, and desktop/mobile screenshots.
`tests/runtime-shell-policy.browser.mjs` checks fixed chrome across five
viewports, stable aligned plugin navigation, container scroll restoration,
saved-interface automatic startup, explicit reapplication, inactive-module
blocking, failed restore retry, and lazily loaded collapsed history.

## Module Market

The `/market` page reads the host-owned module registry, refreshes the fixed
catalog through a background job, and shows rejected catalog entries without
inventing installable metadata. Browser code never downloads module packages.

```text
GET  /api/heredity/market
POST /api/heredity/market/refresh {}
POST /api/heredity/market/install
GET  /api/heredity/market/jobs/{job_id}
GET  /api/heredity/market/modules
POST /api/heredity/market/modules/{id}/start {}
POST /api/heredity/market/modules/{id}/stop {}
POST /api/heredity/market/modules/{id}/restart {}
```

The installation dialog confirms each permission and submits the exact
`plugin_id`, `expected_version`, `expected_sha256`, `confirmed_permissions`,
and `force_warning` values. Warning entries require a separate acknowledgement
before forced installation. Banned entries have no installation control.
Incompatible host versions and stale catalogs disable installation.
Package source labels exclude signed URL query parameters.

The catalog reports actual runtime support only for market WASM modules.
Installed modules show their own process/isolation state, namespace and
start/stop/restart controls. Existing embedded platform plugins are outside
that runtime display. Background refresh, install and module lifecycle jobs
show their queued/running/terminal state and sanitized errors. Installation
success with startup failure remains visible as installed but not running.

`tests/market.browser.mjs` uses an offline byte source with real host HTTP,
background jobs, hash checking and WASM workers. It exercises all catalog
safety states, exact permission confirmation, version blocking, integrity
failure, installed-module controls, failed-refresh cache behavior and
desktop/mobile light/dark layouts using offline fixtures.

## Host Settings

The `/settings` page reads and saves the management listener's next startup
port through `GET` and `PUT /api/heredity/settings`. The mutation contains only
`{"ui_port": 1..65535}`. The host persists it to `.env`; the current listener and
platform sessions keep running. The page reports the active management port
separately from the configured next startup port and lists every registered
network interface's configured port, actual port, endpoint and failure state.
Service port changes link back to the independent network-interface editor.

The app polls `GET /api/heredity/attention` every three seconds after
authentication. Only host-provided error/warning counts and public messages
become sidebar indicators and current-page notices. Valid plugin page paths
are displayed only when present in the registered navigation metadata.
Unrecognized routes are ignored; additional private fields are not projected.
`tests/settings-attention.browser.mjs` checks isolated `.env` persistence,
an unchanged running management listener, independent service ports, actual
port-conflict errors, plugin page findings, and desktop/mobile fixed chrome.

## Checks

```powershell
pnpm typecheck
pnpm test
pnpm build
```

Builds emit to this directory's `dist/`.
`tests/plugin-accounts.browser.mjs` starts a bounded real application with
offline SDK fixtures, exercises independent account actions and captures
desktop/mobile layouts. It uses only the registered offline fixture plugin. Set
`PLAYWRIGHT_PACKAGE` and `NYXION_TEST_BROWSER` when using an explicit local
Playwright package and browser executable.
