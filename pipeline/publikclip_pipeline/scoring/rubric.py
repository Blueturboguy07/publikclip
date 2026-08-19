"""The virality rubric: T1 prompt/schema, cross-validation multipliers,
platform-weighted composite, and full provenance assembly.

The design principle (decision #3, full transparency): a score is never a
bare number. Every clip carries its subscores, which local detectors fired,
which didn't, and every adjustment applied — because an auditable score is
the product's answer to OpusClip's opaque 0–100 that reviewers catch being
wrong.

Cross-validation (the differentiating step, grounded in StandUp4AI's use of
audience laughter as humor ground truth): LLM judgments are discounted when
no acoustic evidence corroborates them and boosted when independent
detectors agree. The constants are design values pending calibration
against real outcomes (the M6 feedback loop exists to tune them).
"""

from __future__ import annotations

from typing import Any

# --- T1 structured-output schema (LLM JSON mode / Ollama format) ---

T1_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hook": {"type": "integer", "description": "0-10: do the first ~3 seconds grab attention on their own?"},
        "hook_type": {
            "type": "string",
            "enum": ["question", "bold_claim", "story_open", "absurd", "conflict", "none"],
        },
        "funniness": {"type": "integer", "description": "0-10: genuinely funny to a general audience?"},
        "punchline_index": {
            "type": "integer",
            "description": "word index of the punchline within the transcript, or -1 if none",
        },
        "shock": {"type": "integer", "description": "0-10: surprising/taboo/shocking content, NOT the same as hook"},
        "curiosity_gap": {"type": "integer", "description": "0-10: does it open a question the viewer must stay to resolve?"},
        "value": {"type": "integer", "description": "0-10: practical/emotional value, resonance, personal connection"},
        "self_contained": {"type": "boolean", "description": "understandable with zero outside context?"},
        "bait_phrases": {
            "type": "array",
            "items": {"type": "string"},
            "description": "engagement-bait phrases present (like/subscribe/comment below); NEGATIVE signal",
        },
        "summary": {"type": "string", "description": "one sentence: what happens in this moment"},
    },
    "required": [
        "hook", "hook_type", "funniness", "punchline_index", "shock",
        "curiosity_gap", "value", "self_contained", "bait_phrases", "summary",
    ],
}

T2_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "visual_interest": {"type": "integer", "description": "0-10: motion, expressions, visual variety across frames"},
        "faces_visible": {"type": "boolean"},
        "expressive_peak": {"type": "boolean", "description": "any frame with a strong facial expression (laughing, shocked)?"},
        "on_screen_text": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["visual_interest", "faces_visible", "expressive_peak", "on_screen_text", "notes"],
}


def t1_prompt(transcript_text: str, context: dict) -> str:
    """The candidate's transcript plus the local evidence the LLM is allowed
    to see (events summary) — but the LLM never sees the heatmap or arousal
    numbers it will later be checked against. Judge and evidence stay
    independent; that's what makes the cross-validation meaningful."""
    events_desc = context.get("events_desc", "none detected")
    return (
        "You are rating a candidate short-form clip cut from a longer video. "
        "Rate ONLY what is in this transcript — do not assume missing context makes it better.\n\n"
        f"Speakers and transcript ({context.get('duration', 0):.0f} seconds):\n"
        f"{transcript_text}\n\n"
        f"Audio events detected in this span: {events_desc}\n\n"
        "Score each dimension honestly. Most clips are mediocre; 8+ on any "
        "dimension should be rare. hook rates ONLY the first ~3 seconds. "
        "shock is about content (surprising/taboo), independent of hook. "
        "punchline_index is the 0-based index (counting every word in order) "
        "of the word where the biggest laugh lands, or -1."
    )


# --- Cross-validation multipliers (v1 design values) ------------------------
# The four cross-validation constants are versioned in scoring_config and
# auto-fitted from real Instagram outcomes (decision #13). These literals are
# version 1; live code passes `constants` from scoring.constants.active().

FUNNY_NO_LAUGH = 0.55       # LLM says funny, no laughter within ±3 s
FUNNY_CORROBORATED = 1.25   # laughter confirmed by 2+ independent detectors
SHOCK_NO_AROUSAL = 0.6      # LLM says shocking, flat arousal + no heatmap lift
HEATMAP_BOOST = 1.15        # real humans replayed this span (≥ p80)
BAIT_PENALTY = 0.85         # per detected bait phrase, floor 0.6 (not fitted)


def _c(constants: dict | None, key: str, default: float) -> float:
    return float(constants[key]) if constants and key in constants else default


def cross_validate(
    t1: dict,
    *,
    laughs_near: list[dict],
    arousal_pct: float,
    heatmap_pct: float | None,
    constants: dict | None = None,
) -> tuple[dict, list[dict]]:
    """Returns (adjusted_subscores, adjustments). Every change is recorded."""
    # Clamp to the rubric range — schema enforcement doesn't bound integers,
    # and a small local model once returned value: -1.
    sub = {
        k: min(10.0, max(0.0, float(t1[k])))
        for k in ("hook", "funniness", "shock", "curiosity_gap", "value")
    }
    adjustments: list[dict] = []
    funny_no_laugh = _c(constants, "funny_no_laugh", FUNNY_NO_LAUGH)
    funny_corroborated = _c(constants, "funny_corroborated", FUNNY_CORROBORATED)
    shock_no_arousal = _c(constants, "shock_no_arousal", SHOCK_NO_AROUSAL)

    if sub["funniness"] >= 4:
        if not laughs_near:
            sub["funniness"] *= funny_no_laugh
            adjustments.append(
                {
                    "rule": "funny_no_laugh",
                    "factor": funny_no_laugh,
                    "reason": "LLM rated this funny but no laughter was detected within ±3s",
                }
            )
        elif any(len(l.get("sources", [])) >= 2 for l in laughs_near):
            sub["funniness"] = min(10.0, sub["funniness"] * funny_corroborated)
            adjustments.append(
                {
                    "rule": "funny_corroborated",
                    "factor": funny_corroborated,
                    "reason": "laughter confirmed by two independent detectors",
                }
            )

    if sub["shock"] >= 4 and arousal_pct < 0.6 and not (heatmap_pct and heatmap_pct >= 0.8):
        sub["shock"] *= shock_no_arousal
        adjustments.append(
            {
                "rule": "shock_no_arousal",
                "factor": shock_no_arousal,
                "reason": "LLM rated this shocking but vocal arousal is flat and no replay elevation",
            }
        )

    bait = t1.get("bait_phrases") or []
    if bait:
        factor = max(0.6, BAIT_PENALTY ** len(bait))
        for k in sub:
            sub[k] *= factor
        adjustments.append(
            {
                "rule": "bait_penalty",
                "factor": round(factor, 3),
                "reason": f"engagement bait detected: {', '.join(bait[:3])}",
            }
        )
    return sub, adjustments


# --- Composite ------------------------------------------------------------

# Platform subscore weights (research-derived defaults; user-tunable later).
PLATFORM_WEIGHTS = {
    "tiktok": {"hook": 0.32, "funniness": 0.26, "shock": 0.16, "curiosity_gap": 0.16, "value": 0.10},
    "reels": {"hook": 0.28, "funniness": 0.22, "shock": 0.14, "curiosity_gap": 0.16, "value": 0.20},
    "shorts": {"hook": 0.26, "funniness": 0.20, "shock": 0.12, "curiosity_gap": 0.22, "value": 0.20},
}

T0_WEIGHT = 0.30   # local interest curve share of the text composite
TEXT_VISUAL_SPLIT = (0.6, 0.4)  # Kayal et al. — applied only when T2 ran


def composite(
    sub: dict,
    curve_score: float,
    heatmap_pct: float | None,
    visual: dict | None,
    constants: dict | None = None,
) -> tuple[dict[str, float], list[dict]]:
    """Per-platform 0–100 scores + any composite-level adjustments."""
    adjustments: list[dict] = []
    platform_scores: dict[str, float] = {}
    heatmap_boost = _c(constants, "heatmap_boost", HEATMAP_BOOST)
    for platform, weights in PLATFORM_WEIGHTS.items():
        t1_part = sum(sub[k] / 10.0 * w for k, w in weights.items())
        text_score = (1 - T0_WEIGHT) * t1_part + T0_WEIGHT * min(1.0, curve_score)
        if visual is not None:
            tw, vw = TEXT_VISUAL_SPLIT
            score = tw * text_score + vw * (visual["visual_interest"] / 10.0)
        else:
            score = text_score
        platform_scores[platform] = score

    if heatmap_pct is not None and heatmap_pct >= 0.8:
        for platform in platform_scores:
            platform_scores[platform] = min(1.0, platform_scores[platform] * heatmap_boost)
        adjustments.append(
            {
                "rule": "heatmap_boost",
                "factor": heatmap_boost,
                "reason": "real viewers replayed this span (top 20% of the replay heatmap)",
            }
        )
    return {k: round(v * 100, 1) for k, v in platform_scores.items()}, adjustments


def recompute_platform_scores(entry: dict, constants: dict) -> dict[str, float] | None:
    """Replay a stored clip's full scoring math under different constants.

    Works from the provenance every scored clip carries (t1_raw, arousal_pct,
    heatmap_pct, curve_score, t2, adjustments). The only fact not directly
    stored is the laughter state behind the funniness branch — it's recovered
    from which adjustment rule fired. Returns None when the entry predates
    t1_raw storage (can't recompute — such clips are shown but never fitted).

    With version-1 constants this reproduces the stored platform_scores
    exactly; the calibration tests assert that on real job data.
    """
    t1 = entry.get("t1_raw")
    if not t1:
        return None
    rules = {a.get("rule") for a in entry.get("adjustments") or []}
    if "funny_no_laugh" in rules:
        laughs_near: list[dict] = []
    elif "funny_corroborated" in rules:
        laughs_near = [{"sources": ["a", "b"]}]
    else:
        # Laughter present but single-source (no multiplier fires), or the
        # funniness branch never ran (score < 4) — either way one plain laugh
        # reproduces the stored behavior.
        laughs_near = [{"sources": ["a"]}]
    sub, _ = cross_validate(
        t1,
        laughs_near=laughs_near,
        arousal_pct=float(entry.get("arousal_pct", 0.0)),
        heatmap_pct=entry.get("heatmap_pct"),
        constants=constants,
    )
    scores, _ = composite(
        sub,
        float(entry.get("curve_score", 0.0)),
        entry.get("heatmap_pct"),
        entry.get("t2"),
        constants=constants,
    )
    return scores


def signals_summary(
    *,
    laughs_near: list[dict],
    events_in_window: list[dict],
    arousal_pct: float,
    heatmap_pct: float | None,
    t2_ran: bool,
    arousal_source: str,
) -> tuple[list[str], list[str]]:
    fired: list[str] = []
    missing: list[str] = []
    (fired if laughs_near else missing).append("laughter")
    (fired if any(e["type"] != "laugh" for e in events_in_window) else missing).append("audio_events")
    (fired if arousal_pct >= 0.6 else missing).append("arousal")
    if heatmap_pct is None:
        missing.append("replay_heatmap")
    elif heatmap_pct >= 0.6:
        fired.append("replay_heatmap")
    (fired if t2_ran else missing).append("visual")
    if arousal_source != "ser":
        missing.append("ser_model (dsp proxy used)")
    return fired, missing
