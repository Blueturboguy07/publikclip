"""Local audio library: home_dir()/audio/{music,sfx}/ + an index at
home_dir()/audio/library.json.

Local import and online fetches (sources/) both land here through the same
Item shape, so the suggester and the render mixer never need to know where
a file came from — only its tags, duration, bpm and licence.

Licence policy lives in sources/__init__.py; this module doesn't enforce
it (a locally-imported file has no licence to police), it only stores
whatever licence/attribution the caller hands it.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..render import ffmpeg_bin

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
AUTO_SFX_MAX_SEC = 8.0  # import kind="auto": shorter than this -> sfx, else music
BPM_ANALYSIS_WINDOW_SEC = 60.0

_TAG_SPLIT = re.compile(r"[^a-z0-9]+")


@dataclass
class Item:
    id: str
    path: str
    kind: str                       # "music" | "sfx"
    name: str
    duration: float
    bpm: float | None
    tags: list[str] = field(default_factory=list)
    source: str = "local"            # "local" | "freesound" | "jamendo"
    source_id: str | None = None
    source_url: str | None = None
    licence: str | None = None       # SPDX-like, e.g. "CC0-1.0"; None for local imports
    attribution: str | None = None   # credit text, or None (local imports, or no credit required)
    added_at: str = ""

    def to_json(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_json(cls, data: dict) -> "Item":
        return cls(
            id=data["id"],
            path=data["path"],
            kind=data["kind"],
            name=data["name"],
            duration=float(data["duration"]),
            bpm=float(data["bpm"]) if data.get("bpm") is not None else None,
            tags=list(data.get("tags", [])),
            source=data.get("source", "local"),
            source_id=data.get("source_id"),
            source_url=data.get("source_url"),
            licence=data.get("licence"),
            attribution=data.get("attribution"),
            added_at=data.get("added_at", ""),
        )


def root_dir() -> Path:
    return config.home_dir() / "audio"


def _index_path() -> Path:
    return root_dir() / "library.json"


def kind_dir(kind: str) -> Path:
    d = root_dir() / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_root() -> Path:
    root = root_dir()
    for d in (root, root / "music", root / "sfx"):
        d.mkdir(parents=True, exist_ok=True)
    return root


def load_index() -> dict[str, Item]:
    p = _index_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {k: Item.from_json(v) for k, v in raw.items()}


def save_index(items: dict[str, Item]) -> None:
    ensure_root()
    tmp = _index_path().with_suffix(".tmp")
    tmp.write_text(json.dumps({k: v.to_json() for k, v in items.items()}, indent=1))
    tmp.replace(_index_path())


def list_items(kind: str | None = None, query: str | None = None) -> list[Item]:
    items = list(load_index().values())
    if kind:
        items = [i for i in items if i.kind == kind]
    if query:
        q = query.lower()
        items = [
            i for i in items
            if q in i.name.lower() or any(q in t for t in i.tags)
        ]
    return items


def remove_item(item_id: str) -> bool:
    items = load_index()
    item = items.pop(item_id, None)
    if item is None:
        return False
    save_index(items)
    Path(item.path).unlink(missing_ok=True)
    return True


def update_tags(item_id: str, tags: list[str]) -> Item | None:
    items = load_index()
    item = items.get(item_id)
    if item is None:
        return None
    item.tags = list(tags)
    save_index(items)
    return item


def add_item(item: Item) -> Item:
    items = load_index()
    items[item.id] = item
    save_index(items)
    return item


def _probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [ffmpeg_bin.ffprobe(), "-v", "error", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, timeout=config.PROBE_TIMEOUT,
    )
    info = json.loads(proc.stdout or "{}")
    return float(info.get("format", {}).get("duration", 0.0))


def detect_bpm(path: Path) -> float | None:
    """librosa.beat.beat_track on the first BPM_ANALYSIS_WINDOW_SEC. None on
    any failure, or on a non-positive/non-finite tempo (librosa returns
    tempo 0.0 for near-silent audio — not a real beat, so treat it as a
    detection failure rather than storing a nonsense bpm)."""
    try:
        import math

        import librosa
        import numpy as np

        y, sr = librosa.load(str(path), sr=None, mono=True, duration=BPM_ANALYSIS_WINDOW_SEC)
        if y.size == 0:
            return None
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        tempo = float(np.asarray(tempo).reshape(-1)[0])
        if not math.isfinite(tempo) or tempo <= 0:
            return None
        return round(tempo, 1)
    except Exception:  # noqa: BLE001 — a library item without a bpm still ships
        return None


def _derive_tags(path: Path) -> list[str]:
    """Tags from the filename + parent folder, lowercased, punctuation and
    pure-numeric tokens dropped, order-preserving de-dupe."""
    tags: list[str] = []
    seen: set[str] = set()
    for part in (path.stem, path.parent.name):
        for tok in _TAG_SPLIT.split(part.lower()):
            if tok and not tok.isdigit() and tok not in seen:
                seen.add(tok)
                tags.append(tok)
    return tags


def unique_dest(target_dir: Path, name: str, item_id: str) -> Path:
    dest = target_dir / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    return target_dir / f"{stem}_{item_id[:8]}{suffix}"


def _iter_audio_files(paths: list[str | Path]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob("*") if f.suffix.lower() in AUDIO_EXTS))
        elif p.is_file() and p.suffix.lower() in AUDIO_EXTS:
            files.append(p)
    return files


def import_paths(paths: list[str | Path], kind: str = "auto") -> list[Item]:
    """Copy files/folders into the library. kind="auto" classifies each
    file by duration (< AUTO_SFX_MAX_SEC -> sfx, else music); "music" or
    "sfx" forces every file into that bucket."""
    ensure_root()
    items = load_index()
    added: list[Item] = []
    now = datetime.now(timezone.utc).isoformat()

    for src in _iter_audio_files(paths):
        item_id = str(uuid.uuid4())
        tags = _derive_tags(src)
        duration = _probe_duration(src)
        resolved_kind = kind
        if kind == "auto":
            resolved_kind = "sfx" if duration < AUTO_SFX_MAX_SEC else "music"

        dest = unique_dest(kind_dir(resolved_kind), src.name, item_id)
        dest.write_bytes(src.read_bytes())

        bpm = detect_bpm(dest) if resolved_kind == "music" else None

        item = Item(
            id=item_id,
            path=str(dest),
            kind=resolved_kind,
            name=src.stem,
            duration=duration,
            bpm=bpm,
            tags=tags,
            source="local",
            added_at=now,
        )
        items[item_id] = item
        added.append(item)

    save_index(items)
    return added
