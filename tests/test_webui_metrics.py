import ctypes
import json
import math
import os
import threading
import unittest
from unittest.mock import patch

from heredity import webui_metrics as metrics


class CPUUsageTests(unittest.TestCase):
    def test_first_sample_is_explicitly_unmeasured(self):
        usage = metrics._CPUUsage()
        self.assertEqual(usage.sample({"cpu": (100, 40), "cpu0": (100, 40)}), (0.0, [], False))

    def test_cpu_usage_uses_busy_delta_and_preserves_core_order(self):
        usage = metrics._CPUUsage()
        usage.sample({"cpu": (100, 40), "cpu1": (40, 20), "cpu0": (60, 20)})
        average, per_core, sampled = usage.sample(
            {"cpu": (200, 65), "cpu1": (80, 30), "cpu0": (120, 35)}
        )
        self.assertTrue(sampled)
        self.assertEqual(average, 75.0)
        self.assertEqual(per_core, [75.0, 75.0])

    def test_stalled_or_reset_counters_never_produce_invalid_percentages(self):
        usage = metrics._CPUUsage()
        for counters in (
            {"cpu": (100, 40)},
            {"cpu": (100, 40)},
            {"cpu": (50, 20)},
            {"cpu": (60, 40)},
        ):
            average, per_core, measured = usage.sample(counters)
            self.assertEqual((average, per_core, measured), (0.0, [], False))

    def test_sampling_is_safe_across_threads(self):
        usage = metrics._CPUUsage()
        results = []
        workers = [
            threading.Thread(
                target=lambda counter=index: results.append(
                    usage.sample({"cpu": (counter * 100, counter * 40)})
                )
            )
            for index in range(1, 17)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), len(workers))
        for average, per_core, _ in results:
            self.assertTrue(math.isfinite(average))
            self.assertLessEqual(average, 100)
            self.assertGreaterEqual(average, 0)
            self.assertEqual(per_core, [])


class LinuxCounterTests(unittest.TestCase):
    def test_cpu_stat_excludes_overlapping_guest_counters(self):
        raw = (
            "cpu 10 5 20 40 3 2 4 1 8 2\n"
            "cpu0 5 2 10 20 1 1 2 1 4 1\n"
            "intr 999\n"
        )
        counters = metrics._parse_linux_cpu_stat(raw)
        self.assertEqual(counters, {"cpu": (85, 43), "cpu0": (42, 21)})

    def test_memory_prefers_reported_available_bytes(self):
        value = metrics._parse_linux_meminfo(
            "MemTotal: 1000 kB\nMemFree: 100 kB\nMemAvailable: 400 kB\n"
        )
        self.assertEqual(value["total"], 1000 * 1024)
        self.assertEqual(value["free"], 400 * 1024)
        self.assertEqual(value["used"], 600 * 1024)
        self.assertEqual(value["usagePercent"], 60)

    def test_memory_falls_back_to_actual_free_when_available_is_missing(self):
        value = metrics._parse_linux_meminfo("MemTotal: 1000 kB\nMemFree: 250 kB\n")
        self.assertEqual(value["free"], 250 * 1024)
        self.assertEqual(value["usagePercent"], 75)

    def test_invalid_memory_and_cpu_data_are_not_silently_presented_as_measurements(self):
        for text in ("cpu 1 2\n", "cpu 1 2 3 -4\n", "intr 5\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                metrics._parse_linux_cpu_stat(text)
        for text in ("MemTotal: 0 kB\nMemFree: 0 kB\n",
                     "MemTotal: 100 kB\nMemFree: 101 kB\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                metrics._parse_linux_meminfo(text)

    def test_process_uptime_handles_spaces_and_parentheses_in_own_comm(self):
        raw = "123 (name with ) parentheses) S " + " ".join(["0"] * 18 + ["250"])
        self.assertEqual(metrics._linux_process_uptime(raw, 10, 100), 7.5)


class SystemInfoTests(unittest.TestCase):
    def test_system_info_shape_is_finite_and_identifies_python(self):
        result = metrics.system_info()
        json.dumps(result, allow_nan=False)
        for key in ("hostname", "platform", "arch", "archLabel", "release", "distro"):
            self.assertIsInstance(result[key], str)
        self.assertTrue(result["nodeVersion"].startswith("Python "))
        self.assertEqual(result["runtime"]["pid"], os.getpid())
        self.assertIsInstance(result["runtime"]["lowIntegrity"], bool)
        self.assertIsInstance(result["metricsUnavailable"], list)
        for key in ("uptime", "processUptime"):
            self.assertTrue(math.isfinite(result[key]))
            self.assertGreaterEqual(result[key], 0)
        cpu = result["cpu"]
        self.assertGreaterEqual(cpu["cores"], 0)
        self.assertTrue(0 <= cpu["average"] <= 100)
        for value in cpu["perCore"]:
            self.assertTrue(math.isfinite(value) and 0 <= value <= 100)
        for value in cpu["loadAvg"]:
            self.assertTrue(math.isfinite(value) and value >= 0)
        memory = result["memory"]
        self.assertEqual(memory["used"] + memory["free"], memory["total"])
        self.assertTrue(0 <= memory["usagePercent"] <= 100)
        for key in ("rss", "heapTotal", "heapUsed", "external", "arrayBuffers"):
            self.assertGreaterEqual(result["runtime"][key], 0)
        self.assertEqual(
            [result["runtime"][key] for key in ("heapTotal", "heapUsed", "external", "arrayBuffers")],
            [0, 0, 0, 0],
        )
        self.assertTrue(any("Node/V8" in item for item in result["metricsUnavailable"]))

    def test_native_counter_layouts_use_fixed_windows_widths(self):
        self.assertEqual(ctypes.sizeof(metrics._MemoryStatusEx), 64)
        self.assertEqual(
            ctypes.sizeof(metrics._ProcessMemoryCounters),
            8 + 8 * ctypes.sizeof(ctypes.c_size_t),
        )

    def test_architecture_labels_follow_current_machine(self):
        with patch.object(metrics.platform, "machine", return_value="AMD64"):
            self.assertEqual(metrics._architecture(), ("x64", "x86-64"))
        with patch.object(metrics.platform, "machine", return_value="aarch64"):
            self.assertEqual(metrics._architecture(), ("arm64", "ARM64"))

    @unittest.skipUnless(os.name == "nt", "Windows own-process metrics")
    def test_windows_reports_actual_system_and_current_process_memory(self):
        windows = metrics._WindowsMetrics()
        memory = windows.memory()
        self.assertGreater(memory["total"], 0)
        self.assertGreaterEqual(memory["free"], 0)
        self.assertLessEqual(memory["free"], memory["total"])
        self.assertGreater(windows.rss(), 0)
        self.assertGreater(windows.uptime(), 0)
        self.assertGreaterEqual(windows.process_uptime(), 0)
        counters = windows.cpu_counters()
        self.assertGreater(counters["cpu"][0], 0)
        self.assertGreaterEqual(counters["cpu"][1], 0)
        self.assertLessEqual(counters["cpu"][1], counters["cpu"][0])
        result = metrics.system_info()
        self.assertEqual(result["platform"], "win32")
        self.assertEqual(result["cpu"]["perCore"], [])
        self.assertGreater(result["runtime"]["rss"], 0)


if __name__ == "__main__":
    unittest.main()
