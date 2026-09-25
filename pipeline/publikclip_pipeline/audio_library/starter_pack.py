"""One-click CC0 starter pack — fills an empty library so the suggester
(edits/audio_suggest.py) has something to work with immediately, without
the user hunting for their own files first.

STARTER_PACK is DATA, not code: tune queries/counts here without touching
bootstrap() or the CLI. Every word audio_suggest.SUGGEST_TAGS relies on
must appear in at least one query below — tests/test_starter_pack.py
asserts it, so the two can't silently drift apart.
"""

from __future__ import annotations

import re

from . import library
from .sources import MissingKeyError, Result, freesound, jamendo

# (query, kind, count) — how many of each to fetch. SFX come from Freesound
# (Jamendo has no sound-effect content); music tries Jamendo first, then
# tops up from Freesound if Jamendo's coverage or key is missing.
STARTER_PACK: list[tuple[str, str, int]] = [
    # SFX
    ("whoosh", "sfx", 6),
    ("swoosh", "sfx", 4),
    ("transition", "sfx", 4),
    ("rimshot", "sfx", 3),
    ("ding", "sfx", 3),
    ("pop", "sfx", 3),
    ("laugh track", "sfx", 2),
    ("applause", "sfx", 2),
    ("boom", "sfx", 3),
    ("riser", "sfx", 3),
    ("comedy sting", "sfx", 2),  # covers SUGGEST_TAGS["laugh"]'s "comedy" word
    # MUSIC
    ("lo-fi hip hop", "music", 4),
    ("cinematic ambient", "music", 3),
    ("upbeat pop", "music", 3),
    ("trap beat", "music", 3),
    ("acoustic guitar", "music", 3),
    ("dramatic tension", "music", 3),
]

_TAG_SPLIT = re.compile(r"[^a-z0-9]+")


def _query_tag_words(query: str) -> list[str]:
    return [t for t in _TAG_SPLIT.split(query.lower()) if t]


def _search_for(query: str, kind: str) -> list[Result]:
    """CC0 only. sfx -> Freesound; music -> Jamendo then Freesound. A
    missing key for one source just means that source contributes nothing
    — never fatal, since the whole point is to work with zero setup."""
    results: list[Result] = []
    if kind == "sfx":
        try:
            results.extend(freesound.search(query, kind="sfx", allow_attribution=False))
        except MissingKeyError:
            pass
        return results
    try:
        results.extend(jamendo.search(query, kind="music", allow_attribution=False))
    except MissingKeyError:
        pass
    try:
        results.extend(freesound.search(query, kind="music", allow_attribution=False))
    except MissingKeyError:
        pass
    return results


def bootstrap(progress=None) -> list[library.Item]:
    """Fetch STARTER_PACK into the library. Skips anything already present
    (matched by (source, source_id)); a query with zero eligible results
    (no key, nothing found) is simply skipped, not an error.

    `progress(fraction, message)` — same 2-arg shape render_clip_edit's
    stages use, so the CLI can wrap it into the app's jsonl progress
    stream exactly like every other long-running pipeline step."""
    existing = {
        (item.source, item.source_id)
        for item in library.load_index().values()
        if item.source_id
    }
    added: list[library.Item] = []
    total = len(STARTER_PACK)

    for i, (query, kind, count) in enumerate(STARTER_PACK):
        if progress:
            progress(i / total, f'Fetching "{query}" ({kind})…')

        results = [r for r in _search_for(query, kind) if (r.source, r.source_id) not in existing]
        results.sort(key=lambda r: (r.rating, r.downloads), reverse=True)

        tag_words = _query_tag_words(query)
        for r in results[:count]:
            module = freesound if r.source == "freesound" else jamendo
            item = module.download(r)
            merged_tags = list(dict.fromkeys(item.tags + tag_words))  # de-dupe, keep order
            library.update_tags(item.id, merged_tags)
            item.tags = merged_tags
            added.append(item)
            existing.add((r.source, r.source_id))

    if progress:
        progress(1.0, f"Starter pack: {len(added)} item(s) added")
    return added
