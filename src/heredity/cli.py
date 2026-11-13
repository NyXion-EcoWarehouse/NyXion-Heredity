import argparse
import json
import os
from pathlib import Path
import sys
import time

from . import __version__, __version_label__
from .backends.base import BackendError
from .runtime import AccountRuntime
from .storage import MessageStore
from .providers.registry import build_manager, execution_payload, read_profile, read_registry


def build_parser():
    parser = argparse.ArgumentParser(prog="nyxion-heredity",
                                     description="NyXion Heredity OneBot and plugin host.")
    parser.add_argument("--version", action="version", version=__version_label__)
    commands = parser.add_subparsers(dest="command", required=True)
    app = commands.add_parser("app", help="Run the UI, OneBot gateway and plugin handshake host.")
    app.add_argument("--host", default="127.0.0.1")
    app.add_argument("--port", type=int, default=5700)
    app.add_argument("--ui-port", type=int,
                     default=int(os.environ.get("HEREDITY_UI_PORT", "5099")))
    app.add_argument("--env-file", type=Path, default=Path(".env"))
    app.add_argument("--password-env", default="HEREDITY_WEBUI_PASSWORD")
    app.add_argument("--token-env", default="HEREDITY_ACCESS_TOKEN")
    app.add_argument("--event-secret-env", default="HEREDITY_EVENT_SECRET")
    app.add_argument("--event-url", action="append", default=[])
    app.add_argument("--sse", action="store_true")
    app.add_argument("--state-dir", type=Path, default=Path(".heredity/host"))
    app.add_argument("--plugin-registry", type=Path)
    app.add_argument("--account", help="Select one OneBot v11 account as platform:self_id.")
    app.add_argument("--static-dir", type=Path)
    app.add_argument("--market-offline", dest="market_refresh_on_start", action="store_false",
                     help="Use the cached module catalog until a manual refresh.")
    plugins = commands.add_parser("plugins", help="Inspect SDK plugin metadata without loading code.")
    plugins.add_argument("operation", choices=("list", "conformance"), default="list", nargs="?")
    plugins.add_argument("--registry", type=Path)
    analyze = commands.add_parser("deobfuscate", help="Statically decode encoded endpoint clues in a local file.")
    analyze.add_argument("file", type=Path, help="Regular local file, at most 8 MiB; never executed.")
    catalog = commands.add_parser("providers", help="Inspect execution levels and configured providers.")
    catalog.add_argument("operation", nargs="?", choices=("list",), default="list")
    catalog.add_argument("--registry", type=Path)
    serve = commands.add_parser("serve", help="Start the OneBot v11 forward HTTP gateway.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5700)
    backend = serve.add_mutually_exclusive_group()
    backend.add_argument("--demo", action="store_true", help="Use an isolated simulated provider; never a live IM platform.")
    backend.add_argument("--profile", type=Path, help="Explicit confirmed HTTP JSON backend profile.")
    backend.add_argument("--providers", type=Path, help="Explicit account-bound provider registry.")
    serve.add_argument("--provider", help="Initial provider ID; defaults to the registry selection.")
    serve.add_argument("--token-env", default="HEREDITY_ACCESS_TOKEN",
                       help="Read gateway token from this environment variable.")
    serve.add_argument("--event-url", action="append", default=[],
                       help="Explicit forward HTTP event receiver; repeatable.")
    serve.add_argument("--heartbeat", type=float, default=15)
    serve.add_argument("--state-dir", type=Path, default=Path(".heredity"),
                       help="Durable state root; demo and profile accounts use separate namespaces.")
    serve.add_argument("--memory-state", action="store_true", help="Use temporary in-memory state.")
    serve.add_argument("--provider-token-env", default="HEREDITY_PROVIDER_TOKEN",
                       help="Enable authenticated provider event input using this separate token.")
    serve.add_argument("--event-secret-env", default="HEREDITY_EVENT_SECRET",
                       help="Optional HMAC-SHA1 signing secret for forward HTTP events.")
    serve.add_argument("--sse", action="store_true", help="Enable the Heredity SSE event extension.")
    serve.add_argument("--execution-token-env", default="HEREDITY_EXECUTION_ADMIN_TOKEN",
                       help="Separate administrative token enabling explicit provider transitions.")
    panel = commands.add_parser("webui", help="Run Heredity's local management API and WebUI.")
    panel.add_argument("--host", default="127.0.0.1")
    panel.add_argument("--port", type=int, default=5099)
    panel.add_argument("--gateway", default="http://127.0.0.1:5700")
    panel.add_argument("--password-env", default="HEREDITY_WEBUI_PASSWORD")
    panel.add_argument("--gateway-token-env", default="HEREDITY_WEBUI_GATEWAY_TOKEN")
    panel.add_argument("--execution-token-env", default="HEREDITY_WEBUI_EXECUTION_TOKEN")
    panel.add_argument("--state-dir", type=Path, default=Path(".heredity/webui"))
    panel.add_argument("--static-dir", type=Path, help="Optional local frontend build directory.")
    return parser


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8")
    from .settings import load_environment
    environment_parser = argparse.ArgumentParser(add_help=False)
    environment_parser.add_argument("--env-file", type=Path, default=Path(".env"))
    environment_args, _ = environment_parser.parse_known_args(argv)
    if "app" in (sys.argv[1:] if argv is None else argv):
        try:
            load_environment(environment_args.env_file)
        except ValueError as failure:
            print(f"Heredity: {failure}", file=sys.stderr)
            return 1
    args = build_parser().parse_args(argv)
    try:
        if args.command == "app":
            from .application import run_application
            return run_application(args, report=_print)
        if args.command == "plugins":
            if args.operation == "conformance":
                from nyxion_heredity_sdk.conformance import main as check_sdk
                return check_sdk([])
            import tempfile
            from .application import discover_plugins
            from .plugins import PluginHost
            with tempfile.TemporaryDirectory(prefix="nyxion-plugin-catalog-") as directory:
                host = PluginHost(directory)
                try:
                    discover_plugins(host, args.registry)
                    _print(host.catalog())
                finally:
                    host.close()
            return 0
        if args.command == "deobfuscate":
            from .analysis.deobfuscate import analyze_file
            _print(analyze_file(args.file))
            return 0
        if args.command == "providers":
            registry = read_registry(args.registry) if args.registry else None
            store = MessageStore()
            manager = None
            try:
                manager = build_manager(
                    store, registry["self_id"] if registry else 10000,
                    demo=registry is None, registry=registry,
                    registry_directory=args.registry.parent if args.registry else None,
                    activate=False)
                _print({**execution_payload(manager), "inspection_only": True})
            finally:
                if manager is not None:
                    manager.close()
                store.close()
            return 0
        if args.command == "webui":
            from .webui import create_webui_server
            if not 0 <= args.port <= 65535:
                raise ValueError("WebUI port must be in 0..65535.")
            panel = create_webui_server(
                args.host, args.port, os.environ.get(args.password_env, ""),
                gateway=args.gateway, gateway_token=os.environ.get(args.gateway_token_env, ""),
                state_dir=args.state_dir, static_dir=args.static_dir,
                execution_token=os.environ.get(args.execution_token_env, ""))
            try:
                panel.state.start()
                print(f"Heredity {__version__}: local WebUI API on {panel.server_address}; "
                      "Heredity plugin and connection management", flush=True)
                panel.serve_forever()
            finally:
                panel.server_close()
            return 0
        from .onebot import create_server
        if not 0 <= args.port <= 65535 or not 0 < args.heartbeat <= 86400:
            raise ValueError("Require port 0..65535 and heartbeat in (0,86400].")
        registry = read_registry(args.providers) if args.providers else None
        profile = read_profile(args.profile) if args.profile else None
        mode = (registry["providers"][0]["kind"] if registry else
                "demo" if args.demo else "profile" if profile else "unconfigured")
        mode = "profile" if mode == "http_json" else mode
        self_id = (registry["self_id"] if registry else 10000 if args.demo else
                   profile.get("self_id", 0) if profile else 0)
        if type(self_id) is not int or not 0 <= self_id <= 2**63 - 1:
            raise ValueError("A configured account must be a positive signed-64-bit integer.")
        state = (":memory:" if args.memory_state or not self_id else
                 args.state_dir / mode / f"{self_id}.sqlite3")
        store = MessageStore(state)
        provider = None
        try:
            provider = build_manager(
                store, self_id, demo=args.demo, profile=profile, registry=registry,
                registry_directory=args.providers.parent if args.providers else None,
                initial_provider_id=args.provider, forbidden_port=args.port)
            backend = AccountRuntime(provider, store, mode=mode)
        except BaseException:
            store.close()
            if provider is not None:
                provider.close()
            raise
        server = None
        try:
            server = create_server(args.host, args.port, os.environ.get(args.token_env, ""), backend,
                                   event_urls=tuple(args.event_url), heartbeat_interval=args.heartbeat,
                                   self_id=backend.self_id,
                                   provider_token=os.environ.get(args.provider_token_env, ""),
                                   event_secret=os.environ.get(args.event_secret_env, ""),
                                   enable_sse=args.sse,
                                   execution_token=os.environ.get(args.execution_token_env, ""))
            backend.initialize()
            label = "DEMO (no live IM connection)" if mode == "demo" else mode
            print(f"Heredity {__version__}: OneBot v11 HTTP on {server.server_address}; backend={label}",
                  flush=True)
            server.start_events()
            server.serve_forever()
        finally:
            if server is not None:
                server.stop_events()
                server.server_close()
                with server.backend_lock:
                    backend.close()
            else:
                backend.close()
        return 0
    except KeyboardInterrupt:
        return 0
    except (BackendError, ValueError, OSError) as exc:
        print(f"Heredity: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
