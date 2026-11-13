"""Executable SDK checks using explicit, reviewed offline plugin fixtures."""

from __future__ import annotations

import argparse
from dataclasses import replace
import importlib
import json
from pathlib import Path
import secrets
import tempfile
import time

from . import (
    API_MAJOR, API_MINOR, ActionRequest, ActionResult, ContractError,
    EventEnvelope, Heartbeat, HostHello, Plugin, PluginContext, PluginManifest,
    PublishAck, SessionIdentity, SessionMismatch, require_session, validate_config,
    validate_action_result, validate_handshake, validate_pong,
)


def run_conformance(factory, *, config=None):
    """Exercise one synthetic session; factory must not contact real accounts.

    This validates observable public contract behavior, not plugin trust,
    completeness, OS sandboxing, wire compatibility or production performance.
    There is no automatic retry of a business action.
    """
    checks = []

    def checked(name):
        checks.append(name)

    plugin = factory()
    if not isinstance(plugin, Plugin):
        raise ContractError("fixture does not implement the SDK plugin protocol")
    manifest = plugin.manifest
    if not isinstance(manifest, PluginManifest):
        raise ContractError("fixture manifest must be a PluginManifest")
    if PluginManifest.from_dict(manifest.to_dict()) != manifest:
        raise ContractError("manifest round trip did not preserve the contract")
    checked("manifest")
    config = validate_config(manifest, {"self_id": "10000"} if config is None else config)
    checked("configuration")
    hello = HostHello("conformance-" + secrets.token_hex(8), secrets.token_hex(24))
    updates, events, challenges = [], [], []

    def publish(envelope):
        if not isinstance(envelope, EventEnvelope):
            raise ContractError("event callback did not receive a typed envelope")
        events.append(envelope)
        return PublishAck(envelope.event_id, True)

    with tempfile.TemporaryDirectory(prefix="nyxion-sdk-") as directory:
        context = PluginContext(
            hello.instance_id, Path(directory).resolve(),
            publish_event=publish, update_session=updates.append,
            publish_challenge=challenges.append)
        try:
            reply = plugin.handshake(hello)
            validate_handshake(manifest, hello, reply)
            checked("handshake_instance_nonce_api")
            try:
                validate_handshake(manifest, hello, replace(reply, nonce="wrong-nonce"))
            except ContractError:
                checked("wrong_handshake_rejected")
            else:
                raise ContractError("wrong handshake unexpectedly passed")
            plugin.start(context, config)
            checked("start")
            online = [item for item in updates if item.state == "online"]
            if not online or online[-1].session is None:
                raise ContractError("offline fixture did not publish its test account")
            session = online[-1].session
            if session.platform != manifest.platform:
                raise ContractError("fixture session platform differs from manifest")
            checked("account_session_binding")
            for sequence in (1, 2):
                heartbeat = Heartbeat(
                    hello.instance_id, hello.nonce, sequence, time.monotonic() + 2)
                validate_pong(heartbeat, plugin.ping(heartbeat))
            checked("continuous_handshake")
            request = ActionRequest(
                "conformance-action", session, "get_login_info", {}, time.monotonic() + 2)
            if request.action not in manifest.actions:
                raise ContractError("offline fixture must implement get_login_info")
            result = validate_action_result(request, plugin.call(request))
            if not result.succeeded or result.outcome != "completed":
                raise ContractError("offline fixture identity action did not succeed")
            if str(result.data.get("user_id", "")) != session.self_id:
                raise ContractError("identity action account differs from its session")
            checked("action_result_account")
            try:
                validate_action_result(request, replace(result, request_id="wrong-request"))
            except ContractError:
                checked("wrong_result_id_rejected")
            else:
                raise ContractError("wrong action result unexpectedly passed")
            try:
                require_session(replace(session, session_epoch="revoked-epoch"), session)
            except SessionMismatch:
                checked("revoked_session_rejected")
            else:
                raise ContractError("revoked session unexpectedly passed")
            for outcome in ("not_started", "completed", "unknown"):
                ActionResult("failure-shape", outcome, error_code=1502)
            checked("explicit_failure_outcomes")
            acknowledgement = context.publish_event(EventEnvelope(
                session, "conformance-event", {"post_type": "meta_event",
                                               "meta_event_type": "lifecycle",
                                               "sub_type": "enable",
                                               "time": 0, "self_id": int(session.self_id)}))
            if not acknowledgement.persisted:
                raise ContractError("event persistence acknowledgement was not accepted")
            checked("event_persistence_ack")
        finally:
            plugin.stop("conformance_finished")
        stopped = [item for item in updates if item.state == "stopped"]
        if not stopped:
            raise ContractError("offline fixture did not report stopped")
        checked("stop")
    return {"sdk_api": f"{API_MAJOR}.{API_MINOR}", "plugin_id": manifest.plugin_id,
            "passed": True, "checks": checks, "network_required": False,
            "scope": "offline_contract_fixture"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", default="nyxion_heredity_sdk.example:create_plugin",
                        help="Explicit reviewed offline module:factory; importing code is not sandboxed.")
    parser.add_argument("--config", type=Path, help="JSON configuration for that offline fixture.")
    args = parser.parse_args(argv)
    try:
        if args.plugin.count(":") != 1:
            raise ContractError("plugin selector must be module:factory")
        module, name = args.plugin.split(":")
        if not module or not name or "." in name:
            raise ContractError("plugin selector is invalid")
        factory = getattr(importlib.import_module(module), name)
        if not callable(factory):
            raise ContractError("plugin factory must be callable")
        config = None
        if args.config is not None:
            with args.config.open("rb") as stream:
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ContractError("conformance configuration exceeds its limit")
            config = json.loads(raw)
        result = run_conformance(factory, config=config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        # Neither a plugin traceback nor its arbitrary exception message is
        # public test output; those can contain credentials or native details.
        print(json.dumps({"passed": False, "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
