"""Measure local embedded SDK dispatch without network or business sends."""

import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from heredity.plugins.host import PluginHost
from nyxion_heredity_sdk import ActionRequest
from nyxion_heredity_sdk.example import OfflineExamplePlugin


def measure(call, count):
    samples = []
    for _ in range(100):
        call()
    for _ in range(count):
        started = time.perf_counter_ns()
        call()
        samples.append(time.perf_counter_ns() - started)
    samples.sort()
    return {"p50_us": round(statistics.median(samples) / 1000, 2),
            "p95_us": round(samples[int((count - 1) * 0.95)] / 1000, 2),
            "p99_us": round(samples[int((count - 1) * 0.99)] / 1000, 2)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=5000)
    args = parser.parse_args(argv)
    if not 100 <= args.iterations <= 100000:
        parser.error("iterations must be in 100..100000")
    instances = []
    def factory():
        instance = OfflineExamplePlugin()
        instances.append(instance)
        return instance
    with tempfile.TemporaryDirectory(prefix="nyxion-embedded-benchmark-") as directory:
        host = PluginHost(directory)
        try:
            host.register(OfflineExamplePlugin.manifest, factory=factory)
            host.start("example.offline", {"self_id": "12345678"})
            deadline = time.monotonic() + 3
            while not host.account_runtimes() and time.monotonic() < deadline:
                time.sleep(0.001)
            session, runtime = host.account_runtimes()[0]
            payload = {"message": "local synthetic payload", "value": 7}
            direct = measure(
                lambda: instances[0].call(ActionRequest(
                    "local-benchmark", session, "echo", payload, time.monotonic() + 10)),
                args.iterations)
            bus = measure(lambda: runtime.backend.call("echo", payload), args.iterations)
            print(json.dumps({
                "scope": "offline embedded SDK dispatch, no network or per-call persistence",
                "iterations": args.iterations, "direct_sdk": direct, "host_bus": bus,
                "note": "Local synthetic measurement; not upstream throughput or an end-to-end SLA."
            }, indent=2))
        finally:
            host.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
