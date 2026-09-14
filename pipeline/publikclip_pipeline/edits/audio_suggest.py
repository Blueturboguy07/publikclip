"""Deterministic music + SFX suggestions from the clip's music brief and
event timeline. Pure and reproducible — the same inputs always produce the
same suggestions, and it never invents files: a category with no eligible
library item is simply skipped.

Music: score every music-kind library item against the brief (tag overlap
with genre/mood/instruments, bpm_range match, an energy-vs-bpm heuristic on
top of that) and take the best one, spanning the whole clip. No brief ->
fall back to the SAME deterministic mood_prior() the brief generator itself
uses (music/brief.py), scored by tag overlap with ITS words. No music in
the library at all -> no music suggestion.

SFX: one candidate per cut boundary (a whoosh/swoosh/transition-tagged
item) and per laugh event (a laugh/rimshot/comedy-tagged item), capped and
spaced out so the track doesn't turn into a wall of stings.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from ..audio_library.library import Item as LibraryItem
from .timeline import AudioItem

# The tag vocabulary the suggester matches against, in one place so Phase 6's
# starter pack can be checked against it — a test asserts every word here
# shows up in at least one starter-pack query, so the two can't drift apart.
SUGGEST_TAGS: dict[str, list[str]] = {
    "cut": ["whoosh", "swoosh", "transition"],
    "laugh": ["laugh", "rimshot", "comedy"],
}

MUSIC_GAIN_DB = -14.0
MUSIC_FADE_IN = 0.5
MUSIC_FADE_OUT = 1.5

SFX_MAX_PER_CLIP = 6
SFX_MIN_SPACING_SEC = 1.5

# Directional bpm bands per brief.energy — a soft signal ON TOP OF an
# explicit bpm_range match, not a replacement for it (a brief with no
# parseable bpm_range still gets SOME bpm signal this way).
ENERGY_BPM_BANDS = {"low": (0.0, 95.0), "medium": (85.0, 130.0), "high": (115.0, 999.0)}

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_BPM_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)")


def _words(text: str) -> set[str]:
    return {t for t in _TOKEN_SPLIT.split(text.lower()) if t}


def _parse_bpm_range(bpm_range: Any) -> tuple[float, float] | None:
    """"80-95" -> (80.0, 95.0). None for anything unparseable."""
    if not bpm_range:
        return None
    m = _BPM_RANGE.search(str(bpm_range))
    if not m:
        return None
    lo, hi = float(m.group(1)), float(m.group(2))
    return (lo, hi) if lo <= hi else (hi, lo)


def _music_score(item: LibraryItem, brief: dict) -> float:
    brief_words = (
        _words(brief.get("genre") or "")
        | _words(brief.get("mood") or "")
        | _words(" ".join(brief.get("instruments") or []))
    )
    score = 2.0 * sum(1 for t in item.tags if t in brief_words)

    bpm_span = _parse_bpm_range(brief.get("bpm_range"))
    if item.bpm is not None and bpm_span is not None and bpm_span[0] <= item.bpm <= bpm_span[1]:
        score += 3.0

    band = ENERGY_BPM_BANDS.get(brief.get("energy") or "")
    if item.bpm is not None and band is not None and band[0] <= item.bpm <= band[1]:
        score += 1.0

    return score


def _music_score_no_brief(item: LibraryItem, mood_words: set[str]) -> float:
    return float(sum(1 for t in item.tags if t in mood_words))


def _suggest_music(
    music_items: list[LibraryItem], clip_result: dict, events: list[dict], duration: float
) -> AudioItem | None:
    if not music_items:
        return None

    brief = clip_result.get("music")
    if brief:
        scored = [(_music_score(i, brief), i) for i in music_items]
    else:
        # No brief (LLM call failed/skipped) -> the same deterministic prior
        # the brief generator itself would have fed to the LLM.
        from ..music.brief import mood_prior

        prior = mood_prior(events, float(clip_result.get("arousal_pct", 0.5) or 0.5))
        mood_words = _words(prior)
        scored = [(_music_score_no_brief(i, mood_words), i) for i in music_items]

    # Highest score wins; item id breaks ties so the pick is deterministic
    # even when nothing scored above zero — some music beats none.
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    best = scored[0][1]

    return AudioItem(
        id=str(uuid4()),
        library_id=best.id,
        path=best.path,
        kind="music",
        start=0.0,
        duration=duration,
        gain_db=MUSIC_GAIN_DB,
        fade_in=MUSIC_FADE_IN,
        fade_out=MUSIC_FADE_OUT,
        suggested=True,
        # loop/duck left unset -> AudioItem.__post_init__ applies the
        # music defaults (loop=True, duck=True), i.e. "duck from brief".
    )


def _best_sfx(tag_words: list[str], sfx_items: list[LibraryItem]) -> LibraryItem | None:
    candidates = [i for i in sfx_items if any(t in i.tags for t in tag_words)]
    if not candidates:
        return None

    def key(item: LibraryItem) -> tuple[int, float, str]:
        overlap = sum(1 for t in tag_words if t in item.tags)
        return (-overlap, item.duration, item.id)  # most overlap, then shortest, then stable

    return min(candidates, key=key)


def _suggest_sfx(
    sfx_items: list[LibraryItem], events: list[dict], keep_ranges: list[tuple[float, float]]
) -> list[AudioItem]:
    if not sfx_items:
        return []

    # Cut boundaries: the seam between consecutive keep ranges on the
    # OUTPUT timeline (keep_ranges is contiguous, so range[i].end ==
    # range[i+1].start — one cue point per interior seam).
    cue_points: list[tuple[float, str]] = [
        (a[1], "cut") for a, b in zip(keep_ranges, keep_ranges[1:])
    ]
    cue_points += [(e["start"], "laugh") for e in events if e.get("type") == "laugh"]
    cue_points.sort(key=lambda cp: cp[0])

    placed: list[AudioItem] = []
    last_t: float | None = None
    for t, category in cue_points:
        if len(placed) >= SFX_MAX_PER_CLIP:
            break
        if last_t is not None and t - last_t < SFX_MIN_SPACING_SEC:
            continue
        item = _best_sfx(SUGGEST_TAGS[category], sfx_items)
        if item is None:  # only tags present in the library are used
            continue
        placed.append(
            AudioItem(
                id=str(uuid4()), library_id=item.id, path=item.path, kind="sfx",
                start=max(0.0, t), duration=item.duration, suggested=True,
            )
        )
        last_t = t
    return placed


def suggest(
    clip_result: dict,
    library_items: list[LibraryItem],
    events: list[dict],
    keep_ranges: list[tuple[float, float]],
    duration: float,
) -> list[AudioItem]:
    """Deterministic music + sfx suggestions for one clip.

    `events` and `keep_ranges` are OUTPUT-timeline (post dead-space-removal,
    matching what render_clip_edit burns into captions/mixing) — pass
    [(0.0, duration)] for keep_ranges when no dead space was removed."""
    music_items = [i for i in library_items if i.kind == "music"]
    sfx_items = [i for i in library_items if i.kind == "sfx"]

    result: list[AudioItem] = []
    music = _suggest_music(music_items, clip_result, events, duration)
    if music is not None:
        result.append(music)
    result.extend(_suggest_sfx(sfx_items, events, keep_ranges))
    return result
