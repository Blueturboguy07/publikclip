"""ffmpeg binary resolution.

Caption burning needs an ffmpeg built with libass, and (as this very
machine demonstrates) Homebrew's slimmed `ffmpeg` formula ships without it.
Resolution order: PUBLIKCLIP_FFMPEG env → bundled sidecar binary (packaged
app) → Homebrew ffmpeg-full keg → PATH. The first candidate that actually
has the `subtitles` filter wins; if none do, the plain PATH binary is
returned with `has_subtitles=False` so the caller can degrade (render
without burned captions) with an honest message instead of a crash.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import zipfile
from functools import lru_cache
from pathlib import Path

from .. import config

_KEG_CANDIDATES = [
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
    "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
]

# Static macOS builds with libass (ffmpeg.martin-riedl.de). Used only when
# no capable ffmpeg exists on the machine — downloaded once into
# PUBLIKCLIP_HOME/bin so end users never touch Homebrew.
_STATIC_BASE = "https://ffmpeg.martin-riedl.de/redirect/latest/macos/{arch}/release/{tool}.zip"

# Static Windows build with libass: BtbN's GPL build ships ffmpeg.exe and
# ffprobe.exe (with the subtitles filter) in one zip under a stable
# latest-release URL. ~80 MB once, into PUBLIKCLIP_HOME/bin.
_STATIC_WINDOWS = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/"
    "ffmpeg-master-latest-win64-gpl.zip"
)

_EXE = ".exe" if platform.system() == "Windows" else ""


def _has_subtitles_filter(binary: str) -> bool:
    try:
        proc = subprocess.run(
            [binary, "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return " subtitles " in proc.stdout


@lru_cache(maxsize=1)
def resolve() -> tuple[str, bool]:
    """(ffmpeg_path, has_subtitles)."""
    candidates: list[str] = []
    env = os.environ.get("PUBLIKCLIP_FFMPEG")
    if env:
        candidates.append(env)
    candidates.append(str(config.bin_dir() / f"ffmpeg{_EXE}"))  # our downloaded static
    bundled = os.environ.get("PUBLIKCLIP_BUNDLED_FFMPEG")  # set by the app shell
    if bundled:
        candidates.append(bundled)
    if platform.system() == "Darwin":
        candidates.extend(_KEG_CANDIDATES)
    path_ffmpeg = shutil.which("ffmpeg")
    if path_ffmpeg:
        candidates.append(path_ffmpeg)

    fallback: str | None = None
    for cand in candidates:
        if not os.path.exists(cand):
            continue
        fallback = fallback or cand
        if _has_subtitles_filter(cand):
            return cand, True
    return (fallback or "ffmpeg"), False


def ffmpeg() -> str:
    return resolve()[0]


def ffprobe() -> str:
    """ffprobe next to the resolved ffmpeg when present, else PATH."""
    sibling = Path(ffmpeg()).parent / f"ffprobe{_EXE}"
    if sibling.exists():
        return str(sibling)
    return shutil.which("ffprobe") or "ffprobe"


def supports_captions() -> bool:
    return resolve()[1]


def available() -> bool:
    """Whether any ffmpeg (captions or not) can be run. resolve() falls back
    to the bare name "ffmpeg" when nothing was found, so check that it is a
    real file or on PATH rather than trusting the string."""
    path = ffmpeg()
    return os.path.exists(path) or shutil.which(path) is not None


def _download(url: str, dest: Path) -> bool:
    import httpx

    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=300.0) as res:
            if res.status_code != 200:
                return False
            with open(dest, "wb") as fh:
                for chunk in res.iter_bytes():
                    fh.write(chunk)
        return True
    except (httpx.HTTPError, OSError):
        return False


def _ensure_capable_macos(progress) -> bool:
    arch = "arm64" if platform.machine() == "arm64" else "amd64"
    for tool in ("ffmpeg", "ffprobe"):
        dest = config.bin_dir() / tool
        if dest.exists() and (_has_subtitles_filter(str(dest)) if tool == "ffmpeg" else True):
            continue
        if progress:
            progress(-1, f"Downloading {tool} (one-time, caption support)…")
        zpath = dest.with_suffix(".zip")
        try:
            if not _download(_STATIC_BASE.format(arch=arch, tool=tool), zpath):
                return False
            with zipfile.ZipFile(zpath) as zf:
                for name in zf.namelist():
                    if name.rstrip("/").endswith(tool):
                        dest.write_bytes(zf.read(name))
                        break
            dest.chmod(0o755)
        except (OSError, zipfile.BadZipFile):
            return False
        finally:
            zpath.unlink(missing_ok=True)
    return True


def _ensure_capable_windows(progress) -> bool:
    """One zip carries both tools (BtbN GPL build, libass included)."""
    wanted = {
        "ffmpeg.exe": config.bin_dir() / "ffmpeg.exe",
        "ffprobe.exe": config.bin_dir() / "ffprobe.exe",
    }
    if all(dest.exists() for dest in wanted.values()) and _has_subtitles_filter(
        str(wanted["ffmpeg.exe"])
    ):
        return True
    if progress:
        progress(-1, "Downloading ffmpeg (one-time, caption support)…")
    zpath = config.bin_dir() / "ffmpeg-static.zip"
    try:
        if not _download(_STATIC_WINDOWS, zpath):
            return False
        with zipfile.ZipFile(zpath) as zf:
            for name in zf.namelist():
                base = name.rsplit("/", 1)[-1]
                if base in wanted and "/bin/" in name:
                    wanted[base].write_bytes(zf.read(name))
    except (OSError, zipfile.BadZipFile):
        return False
    finally:
        zpath.unlink(missing_ok=True)
    return all(dest.exists() for dest in wanted.values())


def ensure_capable(progress=None) -> bool:
    """If no libass ffmpeg exists anywhere, download a static build once
    into PUBLIKCLIP_HOME/bin (macOS arm64/x86_64, Windows x64), then
    re-resolve. Returns whether caption burning is available afterwards."""
    if supports_captions():
        return True
    system = platform.system()
    config.ensure_home()
    if system == "Darwin":
        ok = _ensure_capable_macos(progress)
    elif system == "Windows":
        ok = _ensure_capable_windows(progress)
    else:
        return False
    if not ok:
        return False
    resolve.cache_clear()
    return supports_captions()
