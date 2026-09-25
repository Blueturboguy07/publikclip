"""Freesound API v2 — text search + hq mp3 preview download.

Token auth only (Freesound's "API key" / token scheme, not full OAuth2): the
key rides as a `token` query param on every request. Search asks for
exactly the fields we use, filters server-side on license and (optionally)
duration, and never requests NC/ND-licensed sounds.

Sign up for a key at https://freesound.org/apiv2/apply/ (instant, free).
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

API_BASE = "https://freesound.org/apiv2"
SIGNUP_URL = "https://freesound.org/apiv2/apply/"
SEARCH_FIELDS = "id,name,duration,tags,license,previews,username,avg_rating,num_downloads"
PAGE_SIZE = 20


def _key() -> str:
    secrets_path = config.home_dir() / "secrets.json"
    key = None
    if secrets_path.exists():
        try:
            key = json.loads(secrets_path.read_text()).get("freesound_key")
        except (json.JSONDecodeError, OSError):
            key = None
    if not key:
        raise key_error("Freesound", "freesound_key", SIGNUP_URL)
    return key


def _attribution(name: str, username: str, licence: str) -> str:
    return f"{name} by {username} on Freesound ({licence})"


def _to_result(sound: dict, kind: str) -> Result | None:
    licence = parse_cc_licence(sound.get("license", ""))
    if licence is None:
        return None
    previews = sound.get("previews") or {}
    url = previews.get("preview-hq-mp3") or previews.get("preview-lq-mp3")
    if not url:
        return None
    username = sound.get("username", "")
    name = sound.get("name", f"freesound-{sound.get('id')}")
    return Result(
        source="freesound",
        source_id=str(sound["id"]),
        name=name,
        duration=float(sound.get("duration", 0.0)),
        tags=list(sound.get("tags", [])),
        kind=kind,
        licence=licence,
        attribution=_attribution(name, username, licence),
        download_url=url,
        page_url=f"https://freesound.org/people/{username}/sounds/{sound['id']}/",
        rating=float(sound.get("avg_rating") or 0.0),
        downloads=int(sound.get("num_downloads") or 0),
    )


def _licence_filter(allow_attribution: bool) -> str:
    if allow_attribution:
        return '(license:"Creative Commons 0" OR license:"Attribution")'
    return 'license:"Creative Commons 0"'


def search(
    query: str,
    kind: str = "music",
    max_duration: float | None = None,
    allow_attribution: bool = False,
) -> list[Result]:
    filt = _licence_filter(allow_attribution)
    if max_duration:
        filt += f" duration:[0 TO {max_duration:.0f}]"
    res = httpx.get(
        f"{API_BASE}/search/text/",
        params={
            "query": query,
            "filter": filt,
            "fields": SEARCH_FIELDS,
            "page_size": PAGE_SIZE,
            "token": _key(),
        },
        timeout=config.HTTP_TIMEOUT,
    )
    res.raise_for_status()
    results = []
    for sound in res.json().get("results", []):
        r = _to_result(sound, kind)
        if r is not None and is_allowed(r.licence, allow_attribution):
            results.append(r)
    return results


def get(source_id: str) -> Result | None:
    """Fetch one sound by id (`audio fetch`) — no kind filter, since the
    caller already picked this exact sound; classify music vs sfx the same
    way local import's "auto" mode does (by duration)."""
    res = httpx.get(
        f"{API_BASE}/sounds/{source_id}/",
        params={"fields": SEARCH_FIELDS, "token": _key()},
        timeout=config.HTTP_TIMEOUT,
    )
    if res.status_code == 404:
        return None
    res.raise_for_status()
    sound = res.json()
    kind = "sfx" if float(sound.get("duration", 0.0)) < library.AUTO_SFX_MAX_SEC else "music"
    r = _to_result(sound, kind)
    # Never surface an NC/ND sound even by exact id (is_allowed(..., True):
    # CC0 or CC-BY both fine here — fetch-by-id already means the caller
    # picked this specific, already-vetted item).
    if r is not None and is_allowed(r.licence, allow_attribution=True):
        return r
    return None


def download(result: Result) -> "library.Item":
    library.ensure_root()
    res = httpx.get(result.download_url, timeout=config.HTTP_TIMEOUT, follow_redirects=True)
    res.raise_for_status()

    item_id = str(uuid4())
    dest_dir = library.kind_dir(result.kind)
    suffix = Path(result.download_url.split("?")[0]).suffix or ".mp3"
    dest = library.unique_dest(dest_dir, f"{result.source_id}{suffix}", item_id)
    dest.write_bytes(res.content)

    bpm = library.detect_bpm(dest) if result.kind == "music" else None
    item = library.Item(
        id=item_id,
        path=str(dest),
        kind=result.kind,
        name=result.name,
        duration=result.duration,
        bpm=bpm,
        tags=list(result.tags),
        source="freesound",
        source_id=result.source_id,
        source_url=result.page_url,
        licence=result.licence,
        attribution=result.attribution,
        added_at=datetime.now(timezone.utc).isoformat(),
    )
    return library.add_item(item)
