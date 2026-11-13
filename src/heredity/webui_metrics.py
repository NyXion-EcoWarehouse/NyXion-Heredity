"""Own-system metrics for the WebUI's legacy SystemInfo JSON shape.

CPU percentages use differences between requests. A first or non-advancing
sample is explicitly unavailable. Node/V8 memory counters have no equivalent
in this Python service and remain zero with an explanatory availability list.
"""

from __future__ import annotations

import ctypes
import math
import os
from pathlib import Path
import platform
import socket
import sys
import threading


DWORD = ctypes.c_uint32
QWORD = ctypes.c_uint64
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
SIZE_T = ctypes.c_size_t


class _FileTime(ctypes.Structure):
    _fields_ = [("low", DWORD), ("high", DWORD)]

    def ticks(self):
        return (int(self.high) << 32) | int(self.low)


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("length", DWORD),
        ("load", DWORD),
        ("total_phys", QWORD),
        ("available_phys", QWORD),
        ("total_pagefile", QWORD),
        ("available_pagefile", QWORD),
        ("total_virtual", QWORD),
        ("available_virtual", QWORD),
        ("available_extended_virtual", QWORD),
    ]


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("size", DWORD),
        ("page_fault_count", DWORD),
        ("peak_working_set", SIZE_T),
        ("working_set", SIZE_T),
        ("quota_peak_paged_pool", SIZE_T),
        ("quota_paged_pool", SIZE_T),
        ("quota_peak_nonpaged_pool", SIZE_T),
        ("quota_nonpaged_pool", SIZE_T),
        ("pagefile_usage", SIZE_T),
        ("peak_pagefile_usage", SIZE_T),
    ]


class _WindowsMetrics:
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows system metrics are unavailable on this host.")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self.kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatusEx)]
        self.kernel.GlobalMemoryStatusEx.restype = BOOL
        self.kernel.GetSystemTimes.argtypes = [ctypes.POINTER(_FileTime)] * 3
        self.kernel.GetSystemTimes.restype = BOOL
        self.kernel.GetTickCount64.argtypes = []
        self.kernel.GetTickCount64.restype = QWORD
        self.kernel.GetCurrentProcess.argtypes = []
        self.kernel.GetCurrentProcess.restype = HANDLE
        self.kernel.GetProcessTimes.argtypes = [HANDLE] + [
            ctypes.POINTER(_FileTime)
        ] * 4
        self.kernel.GetProcessTimes.restype = BOOL
        self.kernel.GetSystemTimeAsFileTime.argtypes = [ctypes.POINTER(_FileTime)]
        self.kernel.GetSystemTimeAsFileTime.restype = None
        self.psapi.GetProcessMemoryInfo.argtypes = [
            HANDLE, ctypes.POINTER(_ProcessMemoryCounters), DWORD
        ]
        self.psapi.GetProcessMemoryInfo.restype = BOOL

    @staticmethod
    def _failed(operation):
        raise OSError(ctypes.get_last_error(), f"{operation} failed")

    def memory(self):
        info = _MemoryStatusEx()
        info.length = ctypes.sizeof(info)
        if not self.kernel.GlobalMemoryStatusEx(ctypes.byref(info)):
            self._failed("GlobalMemoryStatusEx")
        return _memory_values(int(info.total_phys), int(info.available_phys))

    def cpu_counters(self):
        idle, kernel, user = (_FileTime() for _ in range(3))
        if not self.kernel.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            self._failed("GetSystemTimes")
        # Windows kernel time includes idle time.
        return {"cpu": (kernel.ticks() + user.ticks(), idle.ticks())}

    def uptime(self):
        return int(self.kernel.GetTickCount64()) / 1000.0

    def process_uptime(self):
        created, exited, kernel, user, now = (_FileTime() for _ in range(5))
        own_process = self.kernel.GetCurrentProcess()
        if not self.kernel.GetProcessTimes(
            own_process,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            self._failed("GetProcessTimes")
        self.kernel.GetSystemTimeAsFileTime(ctypes.byref(now))
        return max(0.0, (now.ticks() - created.ticks()) / 10_000_000.0)

    def rss(self):
        counters = _ProcessMemoryCounters()
        counters.size = ctypes.sizeof(counters)
        if not self.psapi.GetProcessMemoryInfo(
            self.kernel.GetCurrentProcess(), ctypes.byref(counters), counters.size
        ):
            self._failed("GetProcessMemoryInfo")
        return int(counters.working_set)


def _memory_values(total, free):
    if type(total) is not int or type(free) is not int or total <= 0 or not 0 <= free <= total:
        raise ValueError("Invalid physical memory counters.")
    used = total - free
    return {"total": total, "free": free, "used": used, "usagePercent": 100.0 * used / total}


def _parse_linux_cpu_stat(raw):
    counters = {}
    for line in raw.splitlines():
        fields = line.split()
        if not fields:
            continue
        label = fields[0]
        if label != "cpu" and not (label.startswith("cpu") and label[3:].isdigit()):
            continue
        values = [int(value) for value in fields[1:9]]
        if len(values) < 4 or any(value < 0 for value in values):
            raise ValueError("Invalid Linux CPU counters.")
        # guest and guest_nice, if present later in the row, overlap user/nice.
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        counters[label] = (sum(values), idle)
    if "cpu" not in counters:
        raise ValueError("Linux aggregate CPU counters are unavailable.")
    return counters


def _parse_linux_meminfo(raw):
    values = {}
    for line in raw.splitlines():
        key, separator, text = line.partition(":")
        if not separator or key not in {"MemTotal", "MemAvailable", "MemFree"}:
            continue
        fields = text.split()
        if len(fields) != 2 or fields[1] != "kB":
            raise ValueError("Invalid Linux memory counters.")
        values[key] = int(fields[0]) * 1024
    free_key = "MemAvailable" if "MemAvailable" in values else "MemFree"
    return _memory_values(values["MemTotal"], values[free_key])


def _linux_process_uptime(raw_stat, uptime, ticks_per_second):
    closing = raw_stat.rfind(")")
    if closing < 0 or ticks_per_second <= 0:
        raise ValueError("Invalid Linux own-process start time.")
    fields = raw_stat[closing + 1 :].split()
    start_ticks = int(fields[19])
    if start_ticks < 0:
        raise ValueError("Invalid Linux own-process start time.")
    return max(0.0, uptime - start_ticks / ticks_per_second)


class _CPUUsage:
    def __init__(self):
        self._lock = threading.Lock()
        self._previous = None

    @staticmethod
    def _percentage(previous, current):
        total = current[0] - previous[0]
        idle = current[1] - previous[1]
        if total <= 0 or idle < 0 or idle > total:
            return None
        return min(100.0, max(0.0, 100.0 * (total - idle) / total))

    def sample(self, counters):
        with self._lock:
            previous, self._previous = self._previous, dict(counters)
            if previous is None or "cpu" not in previous:
                return 0.0, [], False
            average = self._percentage(previous["cpu"], counters["cpu"])
            if average is None:
                return 0.0, [], False
            per_core = []
            for label in sorted(
                (key for key in counters if key != "cpu"), key=lambda key: int(key[3:])
            ):
                if label not in previous:
                    continue
                value = self._percentage(previous[label], counters[label])
                if value is not None:
                    per_core.append(value)
            return average, per_core, True


_SNAPSHOT_LOCK = threading.Lock()
_CPU_USAGE = _CPUUsage()
_WINDOWS_METRICS = None


def _finite_nonnegative(value):
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return 0.0
    return value if math.isfinite(value) and value >= 0 else 0.0


def _platform_name():
    if os.name == "nt":
        return "win32"
    if hasattr(sys, "getandroidapilevel") or os.environ.get("ANDROID_ROOT"):
        return "android"
    return sys.platform


def _architecture():
    machine = platform.machine()
    key = machine.lower()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64",
            "i386": "ia32", "i686": "ia32", "x86": "ia32",
            "armv7l": "arm", "armv8l": "arm"}.get(key, key)
    label = {"x64": "x86-64", "arm64": "ARM64", "ia32": "x86", "arm": "ARM"}.get(arch, machine)
    return arch, label


def _distro(platform_name):
    if platform_name == "win32":
        return "Windows " + platform.release()
    if platform_name == "android":
        return "Android"
    try:
        return platform.freedesktop_os_release().get("PRETTY_NAME", platform.system())
    except (AttributeError, OSError):
        return platform.system()


def system_info():
    """Return read-only system data; byte counts are bytes, uptime is seconds.

    On Linux, memory.free uses MemAvailable when reported, otherwise MemFree.
    metricsUnavailable explains neutral values required by the legacy schema.
    """
    global _WINDOWS_METRICS
    with _SNAPSHOT_LOCK:
        unavailable = [
            "cpu.speedMHz: clock frequency is not measured.",
            "runtime.lowIntegrity: process integrity level is not measured.",
            "runtime.heapTotal/heapUsed/external/arrayBuffers: Node/V8 metrics do not apply to Python.",
        ]
        memory = {"total": 0, "free": 0, "used": 0, "usagePercent": 0.0}
        uptime = process_uptime = 0.0
        rss = 0
        counters = None
        name = _platform_name()
        if os.name == "nt":
            try:
                if _WINDOWS_METRICS is None:
                    _WINDOWS_METRICS = _WindowsMetrics()
                windows = _WINDOWS_METRICS
            except (OSError, AttributeError):
                windows = None
                unavailable.append("windows.system: system metric APIs are unavailable.")
            if windows is not None:
                for metric, operation in (
                    ("memory", windows.memory),
                    ("cpu", windows.cpu_counters),
                    ("uptime", windows.uptime),
                    ("processUptime", windows.process_uptime),
                    ("runtime.rss", windows.rss),
                ):
                    try:
                        value = operation()
                        if metric == "memory":
                            memory = value
                        elif metric == "cpu":
                            counters = value
                        elif metric == "uptime":
                            uptime = value
                        elif metric == "processUptime":
                            process_uptime = value
                        else:
                            rss = value
                    except (OSError, ValueError):
                        unavailable.append(f"{metric}: Windows system query failed.")
            unavailable.append("cpu.perCore: per-core Windows CPU usage is not measured.")
            if (os.cpu_count() or 0) > 64:
                unavailable.append("cpu.average: Windows counters cover the calling processor group.")
        elif sys.platform.startswith("linux") or name == "android":
            try:
                counters = _parse_linux_cpu_stat(Path("/proc/stat").read_text())
            except (OSError, ValueError):
                unavailable.append("cpu: /proc/stat counters are unavailable.")
            try:
                memory = _parse_linux_meminfo(Path("/proc/meminfo").read_text())
            except (OSError, ValueError, KeyError):
                unavailable.append("memory: /proc/meminfo counters are unavailable.")
            try:
                uptime = float(Path("/proc/uptime").read_text().split()[0])
            except (OSError, ValueError, IndexError):
                unavailable.append("uptime: /proc/uptime is unavailable.")
            try:
                if uptime <= 0:
                    raise ValueError
                process_uptime = _linux_process_uptime(
                    Path("/proc/self/stat").read_text(), uptime, os.sysconf("SC_CLK_TCK")
                )
            except (OSError, ValueError, IndexError, AttributeError):
                unavailable.append("processUptime: own-process start time is unavailable.")
            try:
                resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
                if resident_pages < 0:
                    raise ValueError
                rss = resident_pages * os.sysconf("SC_PAGE_SIZE")
            except (OSError, ValueError, IndexError, AttributeError):
                unavailable.append("runtime.rss: own-process resident memory is unavailable.")
        else:
            unavailable.append("system: memory, CPU usage and uptime are unavailable on this platform.")
        average, per_core, sampled = (
            _CPU_USAGE.sample(counters) if counters is not None else (0.0, [], False)
        )
        if not sampled:
            unavailable.append("cpu.usage: waiting for two available, advancing CPU samples.")
        try:
            load_average = [_finite_nonnegative(value) for value in os.getloadavg()]
        except (AttributeError, OSError):
            load_average = []
            unavailable.append("cpu.loadAvg: operating-system load averages are unavailable.")
        try:
            hostname = socket.gethostname()
        except OSError:
            hostname = ""
            unavailable.append("hostname: operating-system hostname is unavailable.")
        arch, arch_label = _architecture()
        cores = os.cpu_count() or 0
        if not cores:
            unavailable.append("cpu.cores: logical processor count is unavailable.")
        return {
            "hostname": hostname,
            "platform": name,
            "arch": arch,
            "archLabel": arch_label,
            "release": platform.release(),
            "distro": _distro(name),
            "uptime": _finite_nonnegative(uptime),
            "processUptime": _finite_nonnegative(process_uptime),
            "nodeVersion": "Python " + platform.python_version(),
            "cpu": {
                "model": platform.processor(),
                "cores": cores,
                "speedMHz": 0,
                "loadAvg": load_average,
                "perCore": per_core,
                "average": average,
            },
            "memory": memory,
            "runtime": {
                "pid": os.getpid(),
                "lowIntegrity": False,
                "rss": max(0, rss),
                "heapTotal": 0,
                "heapUsed": 0,
                "external": 0,
                "arrayBuffers": 0,
            },
            "metricsUnavailable": unavailable,
        }
