"""Host memory, without a dependency.

The ASR stage picks its speech model by how much memory is actually free
right now (see asr/stage.py). psutil would do this in one line, but the
pipeline group is already a multi-GB install that is synced once and then
trusted; a new dependency would not reach existing installs until their
marker is cleared. These three code paths are the OS calls psutil makes.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys

GIB = 1024**3


def available_memory_bytes() -> int | None:
    """Bytes a new allocation could get without swapping, or None when the
    host cannot say. Honors PUBLIKCLIP_MEM_AVAILABLE_MB for tests/support."""
    override = os.environ.get("PUBLIKCLIP_MEM_AVAILABLE_MB")
    if override:
        try:
            return int(float(override) * 1024 * 1024)
        except ValueError:
            pass
    try:
        if os.name == "nt":
            return _windows_available()
        if sys.platform == "darwin":
            return _macos_available()
        return _linux_available()
    except Exception:  # noqa: BLE001 — a probe failure must never stop a job
        return None


def _windows_available() -> int | None:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    # Physical memory that is free now. The page file could back more, but
    # a 1.6 GB model read through the page file is what "it won't get past
    # transcribe" looks like on an 8 GB laptop.
    return int(status.ullAvailPhys)


def _macos_available() -> int | None:
    out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    page = re.search(r"page size of (\d+) bytes", out)
    page_size = int(page.group(1)) if page else 16384
    counts = {}
    for line in out.splitlines():
        m = re.match(r"(Pages [a-z ]+?):\s+(\d+)\.", line)
        if m:
            counts[m.group(1)] = int(m.group(2))
    if "Pages free" not in counts:
        return None
    # free + inactive + speculative is what Activity Monitor calls available
    # once the file cache is counted as reclaimable.
    pages = counts.get("Pages free", 0) + counts.get("Pages inactive", 0) + counts.get("Pages speculative", 0)
    return pages * page_size


def _linux_available() -> int | None:
    with open("/proc/meminfo", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    return None
