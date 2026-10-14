"""T1.5 — the brain ranks the whole pool at once.

T1 rates every candidate on its own, and a rubric score given in isolation
is a weak ranking: on a gym stream the host's intro out-scored the lift that
viewers replayed most, because the intro "outlines the plan" and the lift's
transcript is "Woo! Get out!". Seeing every candidate side by side, with the
audio evidence the transcript lacks, the brain picks the moments that would
be sent to a friend. The pick order becomes a recorded, replayable bonus on
the composite; the rubric still shows every subscore.
"""

from __future__ import annotations

import os
from typing import Any, Callable

RANK_POOL = 16          # how many the brain is asked to pick, best first
FRAMES_PER_CANDIDATE = 2  # one frame near the setup, one near the payoff (Gemini-class brains)
FRAME_POINTS = (0.35, 0.85)
RANK_BONUS = 0.5        # the first pick's composite factor is 1 + RANK_BONUS; the last pick's ~1.0

RANK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "the candidate's number, copied from its heading"},
                    "why": {"type": "string", "description": "one short sentence: what makes it worth sending"},
                },
                "required": ["index", "why"],
            },
        }
    },
    "required": ["picks"],
}


def _stamp(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m:02d}:{s:02d}"


def _excerpt(text: str, head: int = 40, tail: int = 25) -> str:
    words = text.split()
    if len(words) <= head + tail:
        return " ".join(words)
    return " ".join(words[:head]) + " … " + " ".join(words[-tail:])


def describe(entry: dict, index: int) -> str:
    """One candidate as the ranker sees it: when, how long, what happens,
    what the rubric and the audio say, and the words at both ends."""
    t1 = entry.get("t1_raw") or {}
    sub = entry.get("subscores") or {}
    signals = []
    arousal = entry.get("arousal_pct")
    if arousal is not None:
        signals.append(f"vocal arousal in the top {max(1, round((1 - float(arousal)) * 100))}% of the video")
    events = entry.get("events_in_window") or []
    if events:
        signals.append("audio: " + ", ".join(sorted(set(events))))
    cov = entry.get("activity_coverage", entry.get("speech_coverage"))
    if cov is not None:
        signals.append(f"speech or action {float(cov):.0%} of the time")
    source = "proposed by you from the transcript" if entry.get("source") == "llm" else "a loudness peak"
    if entry.get("title"):
        source += f' ("{entry["title"]}")'
    rubric = ", ".join(f"{k} {float(v):.0f}" for k, v in sub.items())
    return (
        f"=== Candidate {index} [{_stamp(entry['start'])}–{_stamp(entry['end'])}, "
        f"{entry['end'] - entry['start']:.0f} s] ===\n"
        f"What happens: {t1.get('summary', '')}\n"
        f"Found as: {source}. Rubric: {rubric}. Signals: {'; '.join(signals) or 'none'}\n"
        f"Words: {_excerpt(entry.get('transcript', ''))}"
    )


def rank_prompt(entries: list[tuple[int, dict]], want: int, frames_per: int = 0) -> str:
    blocks = [describe(entry, index) for index, entry in entries]
    frames = ""
    if frames_per:
        frames = (
            f"\n\nAfter this text come {frames_per} frames per candidate, in the candidates' "
            "order above (all of the first candidate's frames, then the next candidate's, and "
            "so on), taken near each candidate's setup and payoff. Use them: a face in pain, a "
            "fall, a lift, a stunt, a crowd, a reaction or real motion counts as much as the words, "
            "and a talking head counts less than one."
        )
    return (
        "You have rated these candidate moments from one long video one at a time. Now "
        "look at all of them together and pick the ones that would do best as "
        "short-form vertical clips: watched to the end, replayed, sent to a friend.\n\n"
        f"Pick the {want} best, best first. Judge the moment, not only the words: physical "
        "comedy, a reaction, a stunt, a reveal, a fight, a win or a fail count even when "
        "few words are spoken, and the audio signals tell you when the room reacted. An "
        "introduction, a recap, logistics, or a moment that only sets up a later one is "
        "not a pick. Prefer moments about different things.\n\n"
        + "\n\n".join(blocks)
        + frames
        + "\n\nReturn the picks best first, each with its candidate number copied exactly."
    )


def parse(answer: Any, valid: set[int]) -> list[int]:
    """Ordered, de-duplicated candidate indices the answer named."""
    out: list[int] = []
    if not isinstance(answer, dict):
        return out
    for pick in answer.get("picks") or []:
        if not isinstance(pick, dict):
            continue
        try:
            index = int(pick["index"])
        except (KeyError, TypeError, ValueError):
            continue
        if index in valid and index not in out:
            out.append(index)
    return out


def reasons(answer: Any) -> dict[int, str]:
    out: dict[int, str] = {}
    if isinstance(answer, dict):
        for pick in answer.get("picks") or []:
            if isinstance(pick, dict):
                try:
                    out[int(pick["index"])] = str(pick.get("why") or "")[:160]
                except (KeyError, TypeError, ValueError):
                    continue
    return out


def frame_times(entry: dict) -> list[float]:
    length = float(entry["end"]) - float(entry["start"])
    return [round(float(entry["start"]) + length * p, 2) for p in FRAME_POINTS]


def rank(
    client,
    entries: list[tuple[int, dict]],
    want: int,
    emit: Callable[[float, str], None],
    images: list[bytes] | None = None,
) -> tuple[dict[int, int], dict[int, str]]:
    """{index: 1-based rank} and {index: why} for the picks. `images` are
    FRAMES_PER_CANDIDATE frames per entry in entry order, or nothing. Raises
    what the client raises; an unusable answer ranks nothing (every candidate
    keeps its rubric score)."""
    if os.environ.get("PUBLIKCLIP_NO_RANK") or len(entries) <= 1:
        return {}, {}
    if getattr(client, "backend", "") != "gemini":
        return {}, {}  # a small local model cannot hold ~50 moments in one view
    frames_per = 0
    if images and len(images) == FRAMES_PER_CANDIDATE * len(entries):
        frames_per = FRAMES_PER_CANDIDATE
    else:
        images = None
    emit(-1, f"Ranking {len(entries)} moments against each other" + (" with frames…" if frames_per else "…"))
    answer = client.generate_json(rank_prompt(entries, want, frames_per), RANK_SCHEMA, images=images)
    order = parse(answer, {index for index, _ in entries})
    emit(-1, f"The brain picked {len(order)} moments")
    return {index: n + 1 for n, index in enumerate(order)}, reasons(answer)


def bonus(brain_rank: int | None, pool: int = RANK_POOL) -> float:
    """Composite factor for a pick: 1 + RANK_BONUS at rank 1, falling to 1.0
    past the pool. None (not picked) is 1.0."""
    if brain_rank is None:
        return 1.0
    return 1.0 + RANK_BONUS * max(0, pool - (int(brain_rank) - 1)) / pool
