# Heredity Core Handover

Updated: 2026-11-13, Asia/Taipei.

## Scope

This repository owns the platform-independent core, SDK, management UI, generic
network transports, module marketplace and capability runtime. Platform adapters
are independent projects. The core must not contain platform-specific protocol,
login, signing, process discovery, fallback import paths or implementation examples.

Historical platform material has been preserved outside this repository.
Do not restore compatibility wrappers or adapter-specific fixtures into the core.
Do not modify private `.env`, `.heredity`, device/session caches, account directories
or user services as a way to make a test pass.

## Current interface

- Core package version: `26.11.13.post1`.
- Product label: `Alpha Build 26.11.13 (Core Revision 1)`.
- SDK package version: `1.5.0`; handshake API: `1.0`.
- Main command: `python run.py app`.
- Adapters are discovered through installed entry-point metadata or an explicitly
  supplied external registry. The repository does not ship a platform registry.
- Management accounts: `GET /api/accounts`.
- Account identity: `platform:self_id`; debug invocation uses `{account, action, params}`.
- HTTP gateways without platform metadata use the transport namespace `onebot`.
- SDK message extension data is opaque; adapter implementations own platform
  validation. Generic JSON, size, shape and message checks remain enforced.

The former process inspection and built-in protocol commands are intentionally
not part of the core interface. Generic external OneBot connection and execution
metadata remain available.

## Verification

Use isolated synthetic adapters and temporary state directories. Core tests must
run without installing a platform adapter and must not perform real account login
or send messages.

```powershell
$env:PYTHONPATH='src;tests'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests
pnpm --dir webui test
pnpm --dir webui typecheck
pnpm --dir webui build
```

The final verification record is kept outside the core source tree. It must
distinguish unit/fixture tests, package installation checks and real platform
validation; success in one category is not evidence of another.

## Packaging

Build into a new directory with `scripts/Build-Release.py`, then audit it with
`scripts/Check-NeutralRelease.py`. Host and SDK artifacts must be independent;
release files must not contain runtime state, native platform components or bundled
adapters. Keep metadata, source, UI assets and integrity manifests consistent.

The marketplace accepts stable three-part module versions. Host post revisions
are compared separately, so a packaging revision does not disable compatible
modules.
