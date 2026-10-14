"""T0.5 — the brain reads the whole transcript and proposes the moments.

Until Oct 2026 the LLM only graded ~35 windows the interest curve had cut:
42 s around a loudness peak. It never chose where a moment starts or ends,
so the "hook" it rated was whatever sentence sat 21 s before a loud peak,
and the payoff was often outside the window ("Speed is helping a friend lift
weights and he doesn't even lift it in the end" — Oct 13 2026 report).

Now one call per ~12k words reads the numbered, timestamped sentences and
names the best self-contained moments by sentence number. Those spans join
the curve's windows as candidates, and the same T1 rubric rates them all —
the judge does not change, the pool it judges does.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from ..candidates.windows import DEDUPE_IOU, MAX_LEN, MIN_LEN

PROPOSALS_PER_VIDEO = 20
MIN_PER_CHUNK = 3
# Words per call. A Gemini-class brain (own key or publik API, both report
# backend "gemini") reads an hour in one call. Ollama is gated off below.
CHUNK_WORDS = {"gemini": 12000, "ollama": 1200}
IDEAL_MAX_SEC = 60.0


@dataclass
class Line:
    idx: int
    start: float
    end: float
    speaker: int
    text: str


PROPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "first_line": {
                        "type": "integer",
                        "description": "number of the sentence the clip opens on (its hook)",
                    },
                    "last_line": {
                        "type": "integer",
                        "description": "number of the sentence the clip ends on (its payoff)",
                    },
                    "title": {"type": "string", "description": "five words naming the moment"},
                    "payoff": {
                        "type": "string",
                        "description": "what lands at the end: a punchline, a reveal, an answer, a strong claim, an emotional beat",
                    },
                },
                "required": ["first_line", "last_line", "title", "payoff"],
            },
        }
    },
    "required": ["moments"],
}


def lines_from(segments: list[dict]) -> list[Line]:
    """One numbered line per ASR sentence that has words."""
    out: list[Line] = []
    for seg in segments:
        words = seg.get("words") or []
        text = " ".join(w["word"] for w in words).strip() or str(seg.get("text") or "").strip()
        if not text:
            continue
        start = float(words[0]["start"]) if words else float(seg["start"])
        end = float(words[-1].get("end", words[-1]["start"])) if words else float(seg["end"])
        out.append(Line(idx=len(out), start=start, end=end, speaker=int(seg.get("speaker", 0)), text=text))
    return out


def _stamp(t: float) -> str:
    m, s = divmod(int(t), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_lines(lines: list[Line]) -> str:
    return "\n".join(f"#{ln.idx} [{_stamp(ln.start)}] S{ln.speaker}: {ln.text}" for ln in lines)


def chunk(lines: list[Line], max_words: int) -> list[list[Line]]:
    chunks: list[list[Line]] = []
    current: list[Line] = []
    words = 0
    for ln in lines:
        n = len(ln.text.split())
        if current and words + n > max_words:
            chunks.append(current)
            current, words = [], 0
        current.append(ln)
        words += n
    if current:
        chunks.append(current)
    return chunks


def propose_prompt(lines: list[Line], want: int) -> str:
    return (
        "You are picking moments from a long video to cut into short-form vertical clips "
        "(TikTok, Reels, Shorts). Below are the video's sentences, numbered, with their "
        "time and speaker.\n\n"
        f"Choose the {want} best moments. A moment is good when:\n"
        "- its first sentence works as a hook with no setup: a question, a bold claim, a "
        "story opening, an absurd line, a conflict;\n"
        "- it ends right after its payoff (the punchline, the reveal, the answer, the "
        "strongest line), not on a trailing remark;\n"
        "- it is understandable with zero outside context;\n"
        f"- it runs {MIN_LEN:.0f} to {IDEAL_MAX_SEC:.0f} seconds, never more than {MAX_LEN:.0f};\n"
        "- it is about something different from the other moments you choose.\n"
        "Prefer the moments a viewer would send to a friend. Skip filler, greetings, "
        "logistics, and asks to like, subscribe or comment.\n\n"
        f"{format_lines(lines)}\n\n"
        "Return first_line and last_line as the sentence numbers printed above. "
        "Moments must not overlap."
    )


def _fit(lines: list[Line], first: int, last: int) -> tuple[float, float] | None:
    """A span on sentence bounds, within the clip length limits."""
    lo, hi = lines[0].idx, lines[-1].idx
    if not (lo <= first <= last <= hi):
        return None
    by_idx = {ln.idx: ln for ln in lines}
    start = by_idx[first].start
    end = by_idx[last].end
    while end - start > MAX_LEN and last > first:
        last -= 1
        end = by_idx[last].end
    while end - start < MIN_LEN and last < hi:
        last += 1
        end = by_idx[last].end
    if end - start < MIN_LEN or end - start > MAX_LEN:
        return None
    return round(start, 3), round(end, 3)


def parse(answer: Any, lines: list[Line]) -> list[dict]:
    out: list[dict] = []
    if not isinstance(answer, dict):
        return out
    for entry in answer.get("moments") or []:
        if not isinstance(entry, dict):
            continue
        try:
            first, last = int(entry["first_line"]), int(entry["last_line"])
        except (KeyError, TypeError, ValueError):
            continue
        span = _fit(lines, first, last)
        if span is None:
            continue
        out.append(
            {
                "start": span[0],
                "end": span[1],
                "title": str(entry.get("title") or "")[:80],
                "payoff": str(entry.get("payoff") or "")[:200],
                "source": "llm",
            }
        )
    return out


def propose(client, segments: list[dict], emit: Callable[[float, str], None]) -> list[dict]:
    """The brain's own picks across the whole transcript. Raises what the
    client raises (quota stops must stop the stage); an unusable answer is an
    empty list, never a crash."""
    if os.environ.get("PUBLIKCLIP_NO_PROPOSE"):
        return []
    if getattr(client, "backend", "") != "gemini":
        # A small local model (qwen2.5:7b, Oct 14 2026) answered with generic
        # back-to-back segments ("Game in progress") and empty payoffs: six
        # slow calls for nothing. The curve's windows are its pool.
        return []
    lines = lines_from(segments)
    if not lines:
        return []
    total_words = sum(len(ln.text.split()) for ln in lines)
    chunks = chunk(lines, CHUNK_WORDS.get(getattr(client, "backend", "gemini"), 12000))
    picked: list[dict] = []
    for n, part in enumerate(chunks):
        share = sum(len(ln.text.split()) for ln in part) / max(1, total_words)
        want = max(MIN_PER_CHUNK, round(PROPOSALS_PER_VIDEO * share))
        emit(-1, "Reading the whole transcript for moments…" if len(chunks) == 1 else f"Reading the transcript for moments, part {n + 1}/{len(chunks)}…")
        answer = client.generate_json(propose_prompt(part, want), PROPOSE_SCHEMA)
        picked.extend(parse(answer, part))
    emit(-1, f"The brain proposed {len(picked)} moments of its own")
    return picked


def _iou(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = min(a[1], b[1]) - max(a[0], b[0])
    if inter <= 0:
        return 0.0
    return inter / (max(a[1], b[1]) - min(a[0], b[0]))


def merge(curve_candidates: list[dict], proposed: list[dict], curve_per_sec: list[float]) -> list[dict]:
    """Proposed spans join the pool with a curve_score of their own; a curve
    window that mostly overlaps a proposal gives way to it (the proposal has
    the better boundaries). The curve's windows keep their order, proposals
    follow: a candidate's index is its position, and the rubric's batch
    answers come back by index."""
    curve = np.asarray(curve_per_sec, dtype=float)
    kept: list[dict] = []
    for prop in proposed:
        a, b = int(prop["start"]), max(int(prop["start"]) + 1, int(np.ceil(prop["end"])))
        score = float(np.mean(curve[a:min(b, len(curve))])) if a < len(curve) else 0.0
        if any(_iou((prop["start"], prop["end"]), (k["start"], k["end"])) >= DEDUPE_IOU for k in kept):
            continue
        kept.append(
            {
                "start": prop["start"],
                "end": prop["end"],
                "peak_time": prop["start"],
                "curve_score": round(score, 4),
                "channel_scores": {},
                "source": "llm",
                "title": prop.get("title", ""),
                "payoff": prop.get("payoff", ""),
            }
        )
    out: list[dict] = []
    for cand in curve_candidates:
        span = (cand["start"], cand["end"])
        if any(_iou(span, (k["start"], k["end"])) >= DEDUPE_IOU for k in kept):
            continue
        out.append({**cand, "source": cand.get("source", "curve")})
    out.extend(sorted(kept, key=lambda c: c["start"]))
    return out
