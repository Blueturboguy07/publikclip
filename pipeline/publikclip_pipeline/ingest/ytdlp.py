"""Managed yt-dlp standalone binary + wrapper.

Ported from JeremySNR/clip-forge src/main/pipeline/ytdlp.ts (MIT — see
VENDORED-LICENSES.md): the standalone binary (not the pip package) is
downloaded to PUBLIKCLIP_HOME/bin on first use so the built-in self-updater
keeps working — sites change their players constantly and a stale yt-dlp is
the most common cause of extractor failures. On any yt-dlp failure we run
`-U` once per process and retry once (withSelfUpdateRetry pattern).

Every invocation gets an inactivity watchdog: if yt-dlp prints nothing for
SUBPROCESS_INACTIVITY_TIMEOUT seconds it is killed — a blackholed connection
must never freeze the pipeline (PLAN.md §3).
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from .. import config
from ..render import ffmpeg_bin

ProgressFn = Callable[[float, str], None]  # (fraction 0..1 or -1, message)


class YtDlpError(Exception):
    """yt-dlp process failure with a cleaned, user-facing message."""


def _binary_name() -> str:
    system = platform.system()
    if system == "Windows":
        return "yt-dlp.exe"
    if system == "Darwin":
        return "yt-dlp_macos"
    return "yt-dlp_linux"


def binary_path() -> Path:
    return config.bin_dir() / _binary_name()


def ensure_ytdlp(progress: ProgressFn) -> Path:
    """Download the official standalone binary on first use (~30 MB)."""
    path = binary_path()
    if path.exists():
        return path
    progress(-1, "Downloading yt-dlp (one-time setup)…")
    config.ensure_home()
    url = f"https://github.com/yt-dlp/yt-dlp/releases/latest/download/{_binary_name()}"
    tmp = path.with_suffix(".download")
    with httpx.stream("GET", url, follow_redirects=True, timeout=config.HTTP_TIMEOUT) as res:
        if res.status_code != 200:
            raise YtDlpError(
                f"Could not download yt-dlp (HTTP {res.status_code}). Check your connection."
            )
        total = int(res.headers.get("content-length", 0))
        seen = 0
        with open(tmp, "wb") as fh:
            for chunk in res.iter_bytes():
                fh.write(chunk)
                seen += len(chunk)
                if total:
                    progress(min(1.0, seen / total) * 0.15, "Downloading yt-dlp (one-time setup)…")
    tmp.chmod(0o755)
    tmp.replace(path)
    return path


def _clean_error(stderr: str) -> str:
    """Last ERROR: line, with extractor prefixes stripped (clip-forge)."""
    for line in reversed(stderr.splitlines()):
        if line.startswith("ERROR:"):
            return re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*[^\s:]*:?\s*)?", "", line).strip()
    return ""


def is_auth_error(message: str) -> bool:
    """Site refused the anonymous request — surface a clear next step."""
    return bool(
        re.search(
            r"log ?in|sign ?in|password|private|members only|purchase|cookies|401|403|authoriz",
            message,
            re.IGNORECASE,
        )
    )


def _run(
    bin_path: Path,
    args: list[str],
    on_line: Callable[[str], None] | None = None,
    inactivity_timeout: float = config.SUBPROCESS_INACTIVITY_TIMEOUT,
) -> str:
    """Run yt-dlp, streaming stdout lines, killing on output inactivity."""
    proc = subprocess.Popen(
        [str(bin_path), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    activity = threading.Event()
    done = threading.Event()

    def _pump(stream, sink: list[str], line_cb: Callable[[str], None] | None) -> None:
        for line in stream:
            sink.append(line)
            activity.set()
            if line_cb:
                line_cb(line.rstrip("\n"))

    threads = [
        threading.Thread(target=_pump, args=(proc.stdout, stdout_parts, on_line), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, stderr_parts, None), daemon=True),
    ]
    for t in threads:
        t.start()

    def _watchdog() -> None:
        while not done.is_set():
            activity.clear()
            if done.wait(inactivity_timeout):
                return
            if not activity.is_set():
                proc.kill()
                return

    watchdog = threading.Thread(target=_watchdog, daemon=True)
    watchdog.start()
    code = proc.wait()
    done.set()
    for t in threads:
        t.join(timeout=5)
    if code != 0:
        stderr = "".join(stderr_parts)[-65536:]
        msg = _clean_error(stderr) or f"yt-dlp exited with code {code}"
        if code in (-9, -15) and not _clean_error(stderr):
            msg = "yt-dlp stalled (no output for a while) and was stopped. Check your connection and retry."
        raise YtDlpError(msg)
    return "".join(stdout_parts)


_self_updated_this_run = False


def _with_self_update_retry(bin_path: Path, progress: ProgressFn, fn: Callable[[], str]) -> str:
    global _self_updated_this_run
    try:
        return fn()
    except YtDlpError as original:
        if _self_updated_this_run:
            raise
        _self_updated_this_run = True
        progress(-1, "Updating yt-dlp…")
        try:
            _run(bin_path, ["-U"])
        except YtDlpError:
            raise original from None  # updater failed; the original error is the story
        return fn()


@dataclass
class UrlMeta:
    id: str
    title: str
    duration_sec: float
    webpage_url: str
    # YouTube "most replayed" heatmap: [{start_time, end_time, value}] with
    # value normalized 0..1. Real human engagement data — the free ground
    # truth for the interest curve (PLAN.md stage 5). None if unavailable.
    heatmap: list[dict] | None = None
    raw: dict = field(default_factory=dict, repr=False)


def _pick_playlist_entry(data: dict) -> dict:
    """Some sites resolve to a playlist (archive.org items). Pick the longest
    entry — almost always the main video (clip-forge pattern)."""
    entries = [e for e in data.get("entries") or [] if (e.get("duration") or 0) > 0]
    if not entries:
        raise YtDlpError("This URL does not contain a downloadable video.")
    main = max(entries, key=lambda e: e.get("duration") or 0)
    merged = dict(main)
    merged.setdefault("title", data.get("title"))
    if main.get("url"):
        merged["webpage_url"] = main["url"]
    return merged


def fetch_meta(url: str, progress: ProgressFn) -> UrlMeta:
    bin_path = ensure_ytdlp(progress)

    def _go() -> str:
        return _run(bin_path, ["-J", "--no-playlist", "--no-warnings", url])

    out = _with_self_update_retry(bin_path, progress, _go)
    data = json.loads(out)
    if data.get("_type") == "playlist":
        data = _pick_playlist_entry(data)
    if not data.get("duration") or data["duration"] <= 0:
        raise YtDlpError("This URL does not point to a downloadable video.")
    heatmap = data.get("heatmap")
    if isinstance(heatmap, list) and heatmap:
        heatmap = [
            {
                "start_time": float(seg.get("start_time", 0.0)),
                "end_time": float(seg.get("end_time", 0.0)),
                "value": float(seg.get("value", 0.0)),
            }
            for seg in heatmap
            if isinstance(seg, dict)
        ]
    else:
        heatmap = None
    return UrlMeta(
        id=str(data.get("id", "video")),
        title=str(data.get("title", "Imported video")),
        duration_sec=float(data["duration"]),
        webpage_url=str(data.get("webpage_url") or data.get("url") or url),
        heatmap=heatmap,
        raw=data,
    )


DOWNLOAD_FORMAT = (
    f"bv*[height<={config.MAX_HEIGHT}][ext=mp4]+ba[ext=m4a]"
    f"/b[height<={config.MAX_HEIGHT}][ext=mp4]/b"
)

_PCT_RE = re.compile(r"\[download\]\s+([\d.]+)%")


def download(url: str, out_path: Path, progress: ProgressFn) -> None:
    bin_path = ensure_ytdlp(progress)
    # Provision the bundled static FFmpeg before selecting formats.  YouTube
    # commonly exposes only split video/audio streams; a PATH-only check makes
    # a clean Windows install download orphaned files and fail later.
    ffmpeg_bin.ensure_capable(progress)
    resolved_ffmpeg = ffmpeg_bin.ffmpeg()
    ffmpeg = resolved_ffmpeg if Path(resolved_ffmpeg).exists() else None
    # The Windows bundle does not require a system FFmpeg.  Without it,
    # requesting separate video+audio streams leaves orphaned files that look
    # like a successful yt-dlp run but cannot be consumed by the pipeline.
    # Prefer a progressive MP4 in that case; retain the higher-quality merged
    # selection when FFmpeg is available.
    format_spec = DOWNLOAD_FORMAT if ffmpeg else f"b[height<={config.MAX_HEIGHT}][ext=mp4]/b"
    args = [
        "-f", format_spec,
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--no-warnings",
        "--newline",
        "--socket-timeout", "30",
    ]
    if ffmpeg:
        args += ["--ffmpeg-location", ffmpeg]
    args += ["-o", str(out_path), url]

    def _on_line(line: str) -> None:
        m = _PCT_RE.search(line)
        if m:
            # Map download to 0.15..0.95 of ingest (after binary setup).
            progress(0.15 + (float(m.group(1)) / 100) * 0.8, "Downloading video…")
        elif "[Merger]" in line:
            progress(0.96, "Merging streams…")

    def _go() -> str:
        return _run(bin_path, args, on_line=_on_line)

    _with_self_update_retry(bin_path, progress, _go)
    if not out_path.exists():
        # yt-dlp may add an extension when the template lacks one
        candidates = list(out_path.parent.glob(out_path.name + ".*"))
        if candidates:
            candidates[0].replace(out_path)
        else:
            raise YtDlpError("Download finished but no output file was produced.")
