"""Scoring stage: T1 rubric per candidate → cross-validation → rank → T2
frames on the finalists → per-platform composites + music briefs, all with
full provenance (decision #3).

Cost shape: ~35 T1 text calls + ~12 T2 vision calls + ~12 music calls per
video on the LLM backend. In Ollama mode T2 is skipped (recorded as a missing
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
from . import rubric

SELECT_COUNT = 12


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
            client = llm_mod.make_client(llm_mode)
        except llm_mod.LlmError as err:
            raise StageError(str(err)) from err

        segments = diarize["segments"]
        timeline = events["timeline"]
        curves = json.loads(Path(events["curves_path"]).read_text())
        arousal = np.asarray(curves.get("arousal", []), dtype=float)
        arousal_grid = float(curves.get("arousal_grid_sec", 0.5))
        arousal_source = curves.get("arousal_source", "dsp-proxy")
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

        candidates = cands["candidates"]
        scored: list[dict] = []
        for i, cand in enumerate(candidates):
            start, end = cand["start"], cand["end"]
            ctx.emit(i / max(1, len(candidates)) * 0.6, f"Scoring moment {i + 1}/{len(candidates)}…")
            labeled, flat = _transcript_slice(segments, start, end)
            if len(flat.split()) < 20:
                continue
            window_events = _events_in(timeline, start, end)
            near_laughs = [e for e in _events_in(timeline, start, end, pad=3.0) if e["type"] == "laugh"]
            context = {
                "duration": end - start,
                "events_desc": _events_desc(window_events),
            }
            try:
                t1 = client.generate_json(rubric.t1_prompt(labeled, context), rubric.T1_SCHEMA)
            except llm_mod.LlmError:
                raise
            except Exception as err:  # noqa: BLE001
                ctx.emit(-1, f"moment {i + 1} scoring failed, skipping: {err}")
                continue

            arousal_pct = _window_pct(arousal, arousal_grid, start, end)
            heatmap_pct = (
                _window_pct(heat_values, 1.0, start, end) if heat_values is not None else None
            )
            sub, adjustments = rubric.cross_validate(
                t1,
                laughs_near=near_laughs,
                arousal_pct=arousal_pct,
                heatmap_pct=heatmap_pct,
                constants=cv_constants,
            )
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
            )
            return max(scores.values())

        scored.sort(key=_text_rank, reverse=True)
        finalists = scored[:SELECT_COUNT]

        # T2 visual pass + music brief on finalists only.
        supports_vision = bool(getattr(client, "supports_vision", False))
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
                    except Exception:  # noqa: BLE001 — visual is optional evidence
                        visual = None
            if visual is not None and not isinstance(visual, dict):
                visual = None  # optional evidence — never let a bad response kill the job
            if visual is not None and visual.get("visual_interest") is None:
                visual = None
            entry["t2"] = visual

            platform_scores, comp_adjustments = rubric.composite(
                entry["subscores"], entry["curve_score"], entry["heatmap_pct"], visual,
                constants=cv_constants,
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
            )
            entry["signals_fired"] = fired
            entry["signals_missing"] = missing
            entry["confidence"] = "standard" if client.backend != "ollama" else "local-estimate"

            prior_mood = music_brief.mood_prior(window_events, entry["arousal_pct"])
            try:
                entry["music"] = client.generate_json(
                    music_brief.music_prompt(
                        entry["summary"], entry["transcript"], prior_mood, _events_desc(window_events)
                    ),
                    music_brief.MUSIC_SCHEMA,
                )
                entry["music"]["mood_prior"] = prior_mood
            except Exception:  # noqa: BLE001 — a clip without a music brief still ships
                entry["music"] = None

        finalists.sort(key=lambda e: e["score"], reverse=True)
        for entry in finalists:
            entry.pop("transcript", None)  # bulky; review UI re-slices from diarize

        return {
            "llm_mode": llm_mode,
            "model": client.model,
            "clips": finalists,
            "scored_count": len(scored),
            "t2_ran": supports_vision,
            "scoring_config_version": scoring_config["version"],
            "scoring_constants": cv_constants,
        }
