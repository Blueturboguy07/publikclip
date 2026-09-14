"""Jamendo API v3 — track search + mp3 download.

Jamendo is music-only (full tracks, not sound effects): search(kind="sfx")
returns [] without an HTTP call rather than pretending to look. Needs a
free client id, sent as `client_id` on every request — sign up at
https://devportal.jamendo.com/.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from ... import config
from .. import library
from . import MissingKeyError, Result, is_allowed, key_error, parse_cc_licence

API_BASE = "https://api.jamendo.com/v3.0"
SIGNUP_URL = "https://devportal.jamendo.com/"
PAGE_SIZE = 20


def _client_id() -> str:
    secrets_path = config.home_dir() / "secrets.json"
    client_id = None
    if secrets_path.exists():
        try:
            client_id = json.loads(secrets_path.read_text()).get("jamendo_client_id")
        except (json.JSONDecodeError, OSError):
            client_id = None
    if not client_id:
        raise key_error("Jamendo", "jamendo_client_id", SIGNUP_URL)
    return client_id


def _attribution(name: str, artist: str, licence: str) -> str:
    return f"{name} by {artist} on Jamendo ({licence})"


def _tags(track: dict) -> list[str]:
    info = track.get("musicinfo") or {}
    tags = info.get("tags") or {}
    seen: set[str] = set()
    out: list[str] = []
    for group in ("genres", "instruments", "vartags"):
        for t in tags.get(group) or []:
            t = str(t).lower()
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _to_result(track: dict) -> Result | None:
    if not track.get("audiodownload_allowed", True) or not track.get("audiodownload"):
        return None
    licence = parse_cc_licence(track.get("license_ccurl", ""))
    if licence is None:
        return None
    name = track.get("name", f"jamendo-{track.get('id')}")
    artist = track.get("artist_name", "")
    return Result(
        source="jamendo",
        source_id=str(track["id"]),
        name=name,
        duration=float(track.get("duration", 0.0)),
        tags=_tags(track),
        kind="music",
        licence=licence,
        attribution=_attribution(name, artist, licence),
        download_url=track["audiodownload"],
        page_url=track.get("shareurl") or f"https://www.jamendo.com/track/{track['id']}",
    )


def _licence_param(allow_attribution: bool) -> str:
    return "cc0,by" if allow_attribution else "cc0"


def search(
    query: str,
    kind: str = "music",
    max_duration: float | None = None,
    allow_attribution: bool = False,
) -> list[Result]:
    if kind == "sfx":
        return []  # Jamendo has no sound-effect content
    res = httpx.get(
        f"{API_BASE}/tracks/",
        params={
            "client_id": _client_id(),
            "format": "json",
            "namesearch": query,
            "license_cc": _licence_param(allow_attribution),
            "include": "musicinfo",
            "limit": PAGE_SIZE,
        },
        timeout=config.HTTP_TIMEOUT,
    )
    res.raise_for_status()
    results = []
    for track in res.json().get("results", []):
        if max_duration and float(track.get("duration", 0.0)) > max_duration:
            continue
        r = _to_result(track)
        if r is not None and is_allowed(r.licence, allow_attribution):
            results.append(r)
    return results


def get(source_id: str) -> Result | None:
    """Fetch one track by id (`audio fetch`) — always music (Jamendo has no
    sfx content), matching search()'s kind="sfx" -> [] behavior."""
    res = httpx.get(
        f"{API_BASE}/tracks/",
        params={
            "client_id": _client_id(),
            "format": "json",
            "id": source_id,
            "include": "musicinfo",
        },
        timeout=config.HTTP_TIMEOUT,
    )
    res.raise_for_status()
    tracks = res.json().get("results", [])
    if not tracks:
        return None
    r = _to_result(tracks[0])
    # fetch-by-id: caller already picked this specific item, same as freesound.get
    if r is not None and is_allowed(r.licence, allow_attribution=True):
        return r
    return None


def download(result: Result) -> "library.Item":
    library.ensure_root()
    res = httpx.get(result.download_url, timeout=config.HTTP_TIMEOUT, follow_redirects=True)
    res.raise_for_status()

    item_id = str(uuid4())
    dest_dir = library.kind_dir("music")
    suffix = Path(result.download_url.split("?")[0]).suffix or ".mp3"
    dest = library.unique_dest(dest_dir, f"{result.source_id}{suffix}", item_id)
    dest.write_bytes(res.content)

    item = library.Item(
        id=item_id,
        path=str(dest),
        kind="music",
        name=result.name,
        duration=result.duration,
        bpm=library.detect_bpm(dest),
        tags=list(result.tags),
        source="jamendo",
        source_id=result.source_id,
        source_url=result.page_url,
        licence=result.licence,
        attribution=result.attribution,
        added_at=datetime.now(timezone.utc).isoformat(),
    )
    return library.add_item(item)
