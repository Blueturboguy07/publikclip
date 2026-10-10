"""Windows memory probe for the speech-model ladder (asr/stage.py).

For each model and commit cap, spawns a child that puts itself in a Job
Object with that process-memory limit, loads the model int8 on CPU the way
AsrStage does, transcribes 20 s of audio, and prints one JSON row with its
peak commit charge and working set. A cap of 0 means no cap. The run's
table lands in GITHUB_STEP_SUMMARY when present.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes

MODELS = ["large-v3-turbo", "small", "base"]
CAPS_MB = [0, 3072, 2048]
MB = 1024 * 1024


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        (n, ctypes.c_ulonglong)
        for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
JobObjectExtendedLimitInformation = 9


def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.GetCurrentProcess.restype = wintypes.HANDLE
    return k


def cap_self(cap_mb: int) -> None:
    k = _k32()
    job = k.CreateJobObjectW(None, None)
    if not job:
        raise OSError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_PROCESS_MEMORY
    info.ProcessMemoryLimit = cap_mb * MB
    if not k.SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError(f"SetInformationJobObject failed: {ctypes.get_last_error()}")
    if not k.AssignProcessToJobObject(job, k.GetCurrentProcess()):
        raise OSError(f"AssignProcessToJobObject failed: {ctypes.get_last_error()}")


def peak() -> tuple[int, int]:
    """(peak commit charge MB, peak working set MB) of this process."""
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    pmc = PROCESS_MEMORY_COUNTERS_EX()
    pmc.cb = ctypes.sizeof(pmc)
    psapi.GetProcessMemoryInfo(_k32().GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    return pmc.PeakPagefileUsage // MB, pmc.PeakWorkingSetSize // MB


def child(model: str, cap_mb: int) -> None:
    row: dict = {"model": model, "cap_mb": cap_mb}
    t0 = time.time()
    try:
        if cap_mb:
            cap_self(cap_mb)
        import numpy as np
        import torch  # noqa: F401
        import whisperx

        row["after_import_commit_mb"] = peak()[0]
        m = whisperx.load_model(model, "cpu", compute_type="int8", vad_method="silero")
        row["after_load_commit_mb"] = peak()[0]
        sr = 16000
        t = np.arange(sr * 20) / sr
        audio = (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        m.transcribe(audio, batch_size=8)
        row["status"] = "ok"
    except BaseException as err:  # noqa: BLE001 — the point is to record it
        row["status"] = f"{type(err).__name__}: {err}"[:160]
    row["peak_commit_mb"], row["peak_wset_mb"] = peak()
    row["secs"] = round(time.time() - t0, 1)
    print("ROW " + json.dumps(row), flush=True)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(sys.argv[2], int(sys.argv[3]))
        return
    rows = []
    for model in MODELS:
        for cap in CAPS_MB:
            proc = subprocess.run(
                [sys.executable, __file__, "--child", model, str(cap)],
                capture_output=True, text=True, timeout=2400, encoding="utf-8", errors="replace",
            )
            lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("ROW ")]
            if lines:
                row = json.loads(lines[-1][4:])
            else:
                row = {"model": model, "cap_mb": cap, "status": f"exit {proc.returncode}: {proc.stderr[-300:]!r}"}
            rows.append(row)
            print(json.dumps(row), flush=True)
    md = [
        "| model | cap MB | status | after load commit MB | peak commit MB | peak working set MB | secs |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        md.append(
            f"| {r['model']} | {r['cap_mb'] or 'none'} | {r.get('status', '?')} | "
            f"{r.get('after_load_commit_mb', '?')} | {r.get('peak_commit_mb', '?')} | "
            f"{r.get('peak_wset_mb', '?')} | {r.get('secs', '?')} |"
        )
    text = "\n".join(md)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## ASR memory probe (Windows)\n\n" + text + "\n")


if __name__ == "__main__":
    main()
