"""Scoring stage: T1 rubric per candidate → cross-validation → rank → T2
frames on the finalists → per-platform composites + music briefs, all with
full provenance (decision #3).

Cost shape: ~35 T1 text calls + ~12 T2 vision calls + ~12 music calls per
video on a Gemini-class model (publik API's publik-vision alias, or the
user's own Gemini key). In Ollama mode T2 is skipped (recorded as a missing
signal) and scores are labeled local-estimate."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..jobs.queue import Stage, StageContext, StageError
from ..music import brief as music_brief
from . import constants as constants_mod
from . import frames as frames_mod
from . import llm as llm_mod
from . import propose as propose_mod
from . import rank as rank_mod
from . import rubric

SELECT_COUNT = 12

# Selection rules (Oct 14 2026). A candidate window is ~42 s cut around a
# loudness peak; these make sure what ships is a moment: speech, not air;
# ends on the payoff; twelve different ones.
MIN_CLIP_SEC = 15.0          # same floor as candidates/windows.MIN_LEN
EDGE_SILENCE_MAX = 3.0       # leading/trailing dead air a clip may keep
# Dead air is "nothing happening", not "nobody talking": a second counts as
# active with speech, an audio event, vocal arousal in the video's top
# quarter, or a loudness swing in its top fifth. A gym sprint with 38 %
# speech and screaming the whole way is a moment; silence over nothing is not.
ACTIVE_AROUSAL_PCT = 0.75
ACTIVE_DYNAMICS_PCT = 0.8
TAIL_AFTER_PUNCHLINE = 1.0   # breath kept after the sentence that lands the punchline
MIN_PUNCHLINE_CUT = 4.0      # a tail shorter than this is not worth a cut
LAUGHS_PER_HOUR_ALIVE = 6.0  # fewer heard in the whole source: the detector, not the room, is silent
FINAL_OVERLAP_SEC = 10.0     # finalists sharing more than this are one moment


def _chunks(items: list, size: int) -> list[list]:
    size = max(1, size)
    return [items[i : i + size] for i in range(0, len(items), size)]


def _score_t1(client, items: list[dict], batch_size: int, ctx: StageContext) -> dict[int, dict]:
    """T1 rubric answers keyed by candidate index. Gemini-class brains rate
    T1_BATCH_SIZE moments per call; a moment the batch answer skipped or
    returned incomplete is rated again on its own, so a sloppy answer costs
    one more call, never a lost moment. Ollama rates one at a time."""
    out: dict[int, dict] = {}
    total = len(items)
    done = 0
    for chunk in _chunks(items, batch_size):
        if len(chunk) > 1:
            ctx.emit(done / max(1, total) * 0.6, f"Scoring moments {done + 1}–{done + len(chunk)} of {total}…")
        else:
            ctx.emit(done / max(1, total) * 0.6, f"Scoring moment {done + 1}/{total}…")
        leftovers = list(chunk)
        if len(chunk) > 1:
            prompt = rubric.t1_batch_prompt([(it["i"], it["labeled"], it["context"]) for it in chunk])
            try:
                answer = client.generate_json(prompt, rubric.T1_BATCH_SCHEMA)
            except llm_mod.LlmError as err:
                raise StageError(str(err)) from err
            except Exception as err:  # noqa: BLE001 — fall back to one call per moment
                ctx.emit(-1, f"batch scoring failed, scoring one by one: {err}")
                answer = None
            if isinstance(answer, dict):
                by_index = {
                    int(entry["index"]): entry
                    for entry in (answer.get("moments") or [])
                    if rubric.t1_entry_ok(entry)
                }
                leftovers = []
                for it in chunk:
                    entry = by_index.get(it["i"])
                    if entry is None:
                        leftovers.append(it)
                    else:
                        out[it["i"]] = entry
        for it in leftovers:
            try:
                out[it["i"]] = client.generate_json(
                    rubric.t1_prompt(it["labeled"], it["context"]), rubric.T1_SCHEMA
                )
            except llm_mod.LlmError as err:
                # StageError, not a bare re-raise: the CLI turns it into the
                # {"ok": false, "error": …} result the app shows (a 402's
                # top-up link included) instead of a crashed sidecar.
                raise StageError(str(err)) from err
            except Exception as err:  # noqa: BLE001
                ctx.emit(-1, f"moment {it['i'] + 1} scoring failed, skipping: {err}")
        done += len(chunk)
    return out


def _window_words(segments: list[dict], start: float, end: float) -> list[dict]:
    """The words inside a window, in time order — the same words, in the
    same order, as the transcript the LLM rates (punchline_index counts them)."""
    out: list[dict] = []
    for seg in segments:
        if seg["end"] < start or seg["start"] > end:
            continue
        out.extend(w for w in seg.get("words", []) if start <= w["start"] < end)
    return out


def _speech_coverage(words: list[dict], start: float, end: float) -> float:
    """Share of the window's seconds that carry speech."""
    n = max(1, int(np.ceil(end - start)))
    covered = np.zeros(n, dtype=bool)
    for w in words:
        a = max(0, int(w["start"] - start))
        b = min(n, max(a + 1, int(np.ceil(w.get("end", w["start"] + 0.3) - start))))
        covered[a:b] = True
    return float(covered.mean())


def _per_sec_pct(values: np.ndarray, grid_sec: float, n: int) -> np.ndarray:
    """A gridded curve as per-second percentile ranks within the video:
    the share of seconds at or below each second's value, so a plateau at
    the top reads as the top. A flat curve reads 1.0 everywhere; callers
    that gate on a top share must check how much of the video is up there."""
    out = np.zeros(n)
    if len(values) == 0:
        return out
    per = max(1, int(round(1.0 / grid_sec)))
    coarse = values[: (len(values) // per) * per].reshape(-1, per).mean(axis=1) if per > 1 else values
    m = min(n, len(coarse))
    out[:m] = coarse[:m]
    return np.searchsorted(np.sort(out), out, side="right") / max(1, n)


def _activity(
    segments: list[dict], timeline: list[dict], arousal_pct: np.ndarray, dynamics_pct: np.ndarray, n: int
) -> np.ndarray:
    """Per second: is anything happening? Speech, an audio event, high vocal
    arousal, or a big loudness swing."""
    active = np.zeros(n, dtype=bool)
    for seg in segments:
        for w in seg.get("words", []):
            a = max(0, int(w["start"]))
            b = min(n, max(a + 1, int(np.ceil(w.get("end", w["start"] + 0.3)))))
            active[a:b] = True
    for e in timeline:
        if e.get("type") == "pause":
            continue
        a = max(0, int(e["start"]))
        b = min(n, max(a + 1, int(np.ceil(e["end"]))))
        active[a:b] = True
    # A channel that puts more than half the video in its top band says
    # nothing (the DSP arousal proxy saturates on a loud gym for 50 % of the
    # seconds): then it marks nothing active.
    for pct, floor in ((arousal_pct, ACTIVE_AROUSAL_PCT), (dynamics_pct, ACTIVE_DYNAMICS_PCT)):
        m = min(n, len(pct))
        if m == 0:
            continue
        top = pct[:m] >= floor
        if top.mean() <= 0.5:
            active[:m] |= top
    return active


def _activity_coverage(active: np.ndarray, start: float, end: float) -> float:
    a, b = int(start), max(int(start) + 1, int(np.ceil(end)))
    part = active[a:min(b, len(active))]
    return float(part.mean()) if len(part) else 0.0


def _trim_edges(
    start: float, end: float, active: np.ndarray, seg_starts: np.ndarray
) -> tuple[float, float] | None:
    """Cut leading/trailing dead air longer than EDGE_SILENCE_MAX. A new
    start snaps to a sentence start nearby. None when what is left is
    shorter than a clip or nothing happens in it."""
    a, b = int(start), max(int(start) + 1, int(np.ceil(end)))
    part = active[a:min(b, len(active))]
    if not part.any():
        return None
    on = np.flatnonzero(part)
    first = a + float(on[0])
    last = a + float(on[-1]) + 1.0
    if first - start > EDGE_SILENCE_MAX:
        new_start = max(start, first - 0.5)
        near = seg_starts[(seg_starts >= new_start - 1.0) & (seg_starts <= new_start + 2.0)]
        start = float(near[0]) if len(near) else new_start
    if end - last > EDGE_SILENCE_MAX:
        end = min(end, last + 1.0)
    if end - start < MIN_CLIP_SEC:
        return None
    return round(start, 3), round(end, 3)


def _end_after_punchline(
    start: float, end: float, words: list[dict], punchline_index: object, seg_ends: np.ndarray
) -> float | None:
    """Where the clip should end when the LLM located the punchline: the end
    of the sentence that lands it, plus a breath. None when there is no
    usable punchline, nothing worth cutting, or the clip would get too short."""
    try:
        idx = int(punchline_index)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if idx < 0 or idx >= len(words):
        return None
    punch = float(words[idx]["start"])
    later = seg_ends[seg_ends >= punch]
    sentence_end = float(later[0]) if len(later) else punch + 1.5
    new_end = min(end, sentence_end + TAIL_AFTER_PUNCHLINE)
    if end - new_end < MIN_PUNCHLINE_CUT or new_end - start < MIN_CLIP_SEC:
        return None
    return round(new_end, 3)


def _overlap_sec(a: dict, b: dict) -> float:
    return max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))


def _distinct(ranked: list[dict], limit: int) -> list[dict]:
    """Best first, skipping a span that shares more than FINAL_OVERLAP_SEC
    with one already taken: twelve clips are twelve moments."""
    picked: list[dict] = []
    for entry in ranked:
        if all(_overlap_sec(entry, p) <= FINAL_OVERLAP_SEC for p in picked):
            picked.append(entry)
        if len(picked) >= limit:
            break
    return picked


def _transcript_slice(segments: list[dict], start: float, end: float) -> tuple[str, str]:
    """(speaker-labeled transcript, flat text) for a window."""
    lines: list[str] = []
    flat: list[str] = []
    for seg in segments:
        if seg["end"] < start or seg["start"] > end:
            continue
        words = [w for w in seg.get("words", []) if start <= w["start"] < end]
        if not words:
            continue
        text = " ".join(w["word"] for w in words)
        speaker = seg.get("speaker", 0)
        lines.append(f"S{speaker}: {text}")
        flat.append(text)
    return "\n".join(lines), " ".join(flat)


def _events_in(timeline: list[dict], start: float, end: float, pad: float = 0.0) -> list[dict]:
    return [e for e in timeline if e["end"] >= start - pad and e["start"] <= end + pad]


def _events_desc(events: list[dict]) -> str:
    if not events:
        return "none detected"
    parts = []
    for e in events[:12]:
        parts.append(f"{e['type']} at {e['start'] - 0:.0f}s (conf {e.get('confidence', 0):.2f})")
    return "; ".join(parts)


def _window_pct(values: np.ndarray, grid_sec: float, start: float, end: float) -> float:
    """Percentile rank of this window's mean vs the whole video."""
    if len(values) == 0:
        return 0.0
    a, b = int(start / grid_sec), max(int(start / grid_sec) + 1, int(end / grid_sec))
    window_mean = float(np.mean(values[a : min(b, len(values))])) if a < len(values) else 0.0
    return float(np.mean(values <= window_mean))


class ScoreStage(Stage):
    name = "score"
    schema_version = 1

    def run(self, ctx: StageContext) -> dict:
        prior = ctx.prior or {}
        ingest = prior.get("ingest")
        diarize = prior.get("diarize")
        events = prior.get("events")
        cands = prior.get("candidates")
        if not (ingest and diarize and events and cands):
            raise StageError("Scoring needs ingest + diarize + events + candidates.")

        llm_mode = ctx.settings.llm_mode
        try:
            # notify: a model switch shows up under JUDGE instead of the
            # moment counter silently taking longer.
            client = llm_mod.make_client(llm_mode, notify=lambda msg: ctx.emit(-1, msg))
        except llm_mod.LlmError as err:
            raise StageError(str(err)) from err

        segments = diarize["segments"]
        seg_starts = np.array([float(seg["start"]) for seg in segments], dtype=float)
        seg_ends = np.array([float(seg["end"]) for seg in segments], dtype=float)
        timeline = events["timeline"]
        duration_sec = float(ingest["probe"]["duration_sec"])
        laugh_count = sum(1 for e in timeline if e["type"] == "laugh")
        laugh_detector_alive = laugh_count / max(1 / 60, duration_sec / 3600.0) >= LAUGHS_PER_HOUR_ALIVE
        if not laugh_detector_alive:
            ctx.emit(
                -1,
                f"The laughter detector heard {laugh_count} laugh{'s' if laugh_count != 1 else ''} "
                "in this video, so humour is not discounted for missing laughter.",
            )
        curves = json.loads(Path(events["curves_path"]).read_text())
        arousal = np.asarray(curves.get("arousal", []), dtype=float)
        arousal_grid = float(curves.get("arousal_grid_sec", 0.5))
        arousal_source = curves.get("arousal_source", "dsp-proxy")
        n_sec = max(1, int(np.ceil(duration_sec)))
        active = _activity(
            segments,
            timeline,
            _per_sec_pct(arousal, arousal_grid, n_sec),
            _per_sec_pct(np.asarray(curves.get("dynamics", []), dtype=float), float(curves.get("grid_sec", 0.5)), n_sec),
            n_sec,
        )
        heatmap = ingest.get("heatmap")
        scene_times = json.loads((ctx.job_dir / "scenes.json").read_text()) if (ctx.job_dir / "scenes.json").exists() else []

        heat_values = None
        if heatmap:
            duration = float(ingest["probe"]["duration_sec"])
            heat_values = np.zeros(int(np.ceil(duration)))
            for seg in heatmap:
                a, b = int(seg["start_time"]), int(np.ceil(seg["end_time"]))
                heat_values[max(0, a) : min(len(heat_values), b)] = seg["value"]

        # Calibrated constants (decision #13): loaded once per run, version
        # stamped into every clip's provenance.
        scoring_config = constants_mod.active()
        cv_constants = scoring_config["constants"]

        # T0.5: the brain reads the whole transcript and names its own
        # moments; they join the curve's windows, and one rubric rates all.
        curve_path = ctx.job_dir / "interest_curve.json"
        curve_per_sec = json.loads(curve_path.read_text()).get("per_sec", []) if curve_path.exists() else []
        try:
            proposed = propose_mod.propose(client, segments, ctx.emit)
        except llm_mod.LlmError as err:
            raise StageError(str(err)) from err
        except Exception as err:  # noqa: BLE001 — the curve's windows still get judged
            ctx.emit(-1, f"Reading the whole transcript failed, judging the curve's windows only: {err}")
            proposed = []
        candidates = propose_mod.merge(cands["candidates"], proposed, curve_per_sec)
        # Moments with too few words never cost a call.
        items: list[dict] = []
        for i, cand in enumerate(candidates):
            start, end = cand["start"], cand["end"]
            trimmed = _trim_edges(start, end, active, seg_starts)
            if trimmed is None:
                continue
            start, end = trimmed
            words = _window_words(segments, start, end)
            labeled, flat = _transcript_slice(segments, start, end)
            if len(flat.split()) < 20:
                continue
            window_events = _events_in(timeline, start, end)
            items.append(
                {
                    "i": i,
                    "cand": cand,
                    "start": start,
                    "end": end,
                    "words": words,
                    "labeled": labeled,
                    "context": {"duration": end - start, "events_desc": _events_desc(window_events)},
                }
            )
        batch_size = rubric.T1_BATCH_SIZE if client.backend == "gemini" else 1
        t1_by_index = _score_t1(client, items, batch_size, ctx)

        scored: list[dict] = []
        for item in items:
            i, cand, labeled = item["i"], item["cand"], item["labeled"]
            t1 = t1_by_index.get(i)
            if t1 is None:
                continue
            start, end = item["start"], item["end"]
            adjustments: list[dict] = []
            new_end = _end_after_punchline(start, end, item["words"], t1.get("punchline_index"), seg_ends)
            if new_end is not None:
                adjustments.append(
                    {
                        "rule": "ends_after_punchline",
                        "factor": 1.0,
                        "reason": f"cut {end - new_end:.0f} s that followed the punchline",
                    }
                )
                end = new_end
                labeled, _ = _transcript_slice(segments, start, end)
            speech = _speech_coverage(_window_words(segments, start, end), start, end)
            coverage = _activity_coverage(active, start, end)
            near_laughs = [e for e in _events_in(timeline, start, end, pad=3.0) if e["type"] == "laugh"]

            arousal_pct = _window_pct(arousal, arousal_grid, start, end)
            heatmap_pct = (
                _window_pct(heat_values, 1.0, start, end) if heat_values is not None else None
            )
            sub, cv_adjustments = rubric.cross_validate(
                t1,
                laughs_near=near_laughs,
                arousal_pct=arousal_pct,
                heatmap_pct=heatmap_pct,
                constants=cv_constants,
                laugh_detector_alive=laugh_detector_alive,
            )
            adjustments.extend(cv_adjustments)
            scored.append(
                {
                    "start": start,
                    "end": end,
                    "curve_score": cand["curve_score"],
                    "channel_scores": cand["channel_scores"],
                    "t1_raw": t1,
                    "subscores": {k: round(v, 2) for k, v in sub.items()},
                    "adjustments": adjustments,
                    "arousal_pct": round(arousal_pct, 3),
                    "heatmap_pct": round(heatmap_pct, 3) if heatmap_pct is not None else None,
                    "speech_coverage": round(speech, 3),
                    "activity_coverage": round(coverage, 3),
                    "laugh_detector_alive": laugh_detector_alive,
                    "source": cand.get("source", "curve"),
                    "title": cand.get("title", ""),
                    "events_in_window": sorted(
                        {e["type"] for e in _events_in(timeline, start, end) if e["type"] != "pause"}
                    ),
                    "i": i,
                    "summary": t1.get("summary", ""),
                    "transcript": labeled,
                }
            )

        if not scored:
            raise StageError("No candidate produced a scoreable transcript.")

        # Rank by best pre-visual platform score, take the finalists.
        def _text_rank(entry: dict) -> float:
            scores, _ = rubric.composite(
                entry["subscores"], entry["curve_score"], entry["heatmap_pct"], None,
                constants=cv_constants,
                speech_coverage=entry["activity_coverage"],
                self_contained=entry["t1_raw"].get("self_contained"),
                brain_rank=entry.get("brain_rank"),
            )
            return max(scores.values())

        # T1.5: the brain sees every rated moment side by side and picks the
        # ones worth sending; its pick order is a recorded composite bonus.
        try:
            picks, whys = rank_mod.rank(
                client, [(e["i"], e) for e in scored], rank_mod.RANK_POOL, ctx.emit
            )
        except llm_mod.LlmError as err:
            raise StageError(str(err)) from err
        except Exception as err:  # noqa: BLE001 — the rubric order still stands
            ctx.emit(-1, f"Ranking the moments side by side failed, keeping the rubric order: {err}")
            picks, whys = {}, {}
        for entry in scored:
            if entry["i"] in picks:
                entry["brain_rank"] = picks[entry["i"]]
                entry["brain_why"] = whys.get(entry["i"], "")

        scored.sort(key=_text_rank, reverse=True)
        # Every rated candidate with its pre-visual rank score, finalists or
        # not: the backtest harness measures the whole ranking against replay
        # data, and the review UI can one day show "also considered".
        shortlist = [
            {
                "start": e["start"],
                "end": e["end"],
                "text_score": round(_text_rank(e), 1),
                "source": e["source"],
                "brain_rank": e.get("brain_rank"),
                "subscores": e["subscores"],
                "activity_coverage": e["activity_coverage"],
                "rules": [a["rule"] for a in e["adjustments"]],
                "summary": e["summary"][:120],
            }
            for e in scored
        ]
        finalists = _distinct(scored, SELECT_COUNT)

        # T2 visual pass + music brief on finalists only.
        supports_vision = client.backend == "gemini"
        for j, entry in enumerate(finalists):
            ctx.emit(0.6 + j / max(1, len(finalists)) * 0.35, f"Visual pass {j + 1}/{len(finalists)}…")
            visual = None
            if supports_vision:
                times = frames_mod.sample_times(entry["start"], entry["end"], scene_times)
                imgs = frames_mod.extract_frames(
                    ingest["media_path"], times, ctx.job_dir / "t2frames"
                )
                if imgs:
                    try:
                        visual = client.generate_json(
                            "Rate these frames sampled from one candidate vertical clip. "
                            "Judge visual interest for short-form: expressions, motion, variety.",
                            rubric.T2_SCHEMA,
                            images=imgs,
                        )
                    except llm_mod.PublikStopError as err:
                        # out of balance / disconnected: stop now, resumable
                        # from checkpoints, rather than ship half-scored clips
                        raise StageError(str(err)) from err
                    except Exception:  # noqa: BLE001 — visual is optional evidence
                        visual = None
            entry["t2"] = visual

            platform_scores, comp_adjustments = rubric.composite(
                entry["subscores"], entry["curve_score"], entry["heatmap_pct"], visual,
                constants=cv_constants,
                speech_coverage=entry["activity_coverage"],
                self_contained=entry["t1_raw"].get("self_contained"),
                brain_rank=entry.get("brain_rank"),
                brain_why=entry.get("brain_why", ""),
            )
            entry["adjustments"].extend(comp_adjustments)
            entry["platform_scores"] = platform_scores
            entry["score"] = max(platform_scores.values())
            entry["best_platform"] = max(platform_scores, key=platform_scores.get)

            window_events = _events_in(timeline, entry["start"], entry["end"])
            fired, missing = rubric.signals_summary(
                laughs_near=[e for e in window_events if e["type"] == "laugh"],
                events_in_window=window_events,
                arousal_pct=entry["arousal_pct"],
                heatmap_pct=entry["heatmap_pct"],
                t2_ran=visual is not None,
                arousal_source=arousal_source,
                laugh_detector_alive=laugh_detector_alive,
            )
            entry["signals_fired"] = fired
            entry["signals_missing"] = missing
            entry["confidence"] = "standard" if client.backend == "gemini" else "local-estimate"

            prior_mood = music_brief.mood_prior(window_events, entry["arousal_pct"])
            try:
                entry["music"] = client.generate_json(
                    music_brief.music_prompt(
                        entry["summary"], entry["transcript"], prior_mood, _events_desc(window_events)
                    ),
                    music_brief.MUSIC_SCHEMA,
                )
                entry["music"]["mood_prior"] = prior_mood
            except llm_mod.PublikStopError as err:
                raise StageError(str(err)) from err
            except Exception:  # noqa: BLE001 — a clip without a music brief still ships
                entry["music"] = None

        finalists.sort(key=lambda e: e["score"], reverse=True)
        for entry in finalists:
            entry.pop("transcript", None)  # bulky; review UI re-slices from diarize
            entry.pop("i", None)
            entry.pop("events_in_window", None)

        return {
            "llm_mode": llm_mode,
            "model": client.model,
            "clips": finalists,
            "scored_count": len(scored),
            "shortlist": shortlist,
            "proposed": [
                {k: p[k] for k in ("start", "end", "title", "payoff")} for p in proposed
            ],
            "t2_ran": supports_vision,
            "scoring_config_version": scoring_config["version"],
            "scoring_constants": cv_constants,
        }
