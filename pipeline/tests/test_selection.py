"""Clip selection rules (Oct 14 2026): a clip is speech, not air; it ends on
the payoff; twelve clips are twelve moments; and "funny but nobody laughed"
is only held against a joke when the laughter detector heard laughter
somewhere in the source. The Oct 13 report: "one of these clips is literally
just dead space"."""

import json
from types import SimpleNamespace

import pytest

from publikclip_pipeline import config
from publikclip_pipeline.jobs.queue import StageError
from publikclip_pipeline.scoring import constants as constants_mod
from publikclip_pipeline.scoring import llm as llm_mod
from publikclip_pipeline.scoring import rubric
from publikclip_pipeline.scoring.stage import ScoreStage


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    yield


def _t1(index=None, **over):
    entry = {
        "hook": 5, "hook_type": "question", "funniness": 3, "punchline_index": -1,
        "shock": 2, "curiosity_gap": 4, "value": 5, "self_contained": True,
        "bait_phrases": [], "summary": f"moment {index}",
    }
    if index is not None:
        entry["index"] = index
    entry.update(over)
    return entry


class FakeClient:
    def __init__(self, backend="gemini", per_index=None):
        self.backend = backend
        self.model = "fake-model"
        self.per_index = per_index or {}

    def generate_json(self, prompt, schema, images=None):
        if schema is rubric.T1_BATCH_SCHEMA:
            indices = [int(line.split()[2]) for line in prompt.splitlines() if line.startswith("=== Moment ")]
            return {"moments": [_t1(i, **self.per_index.get(i, {})) for i in indices]}
        if schema is rubric.T1_SCHEMA:
            return _t1()
        if schema is rubric.T2_SCHEMA:
            return {"visual_interest": 5, "faces_visible": True, "expressive_peak": False, "on_screen_text": False, "notes": ""}
        return {
            "genre": "lo-fi", "instruments": [], "mood": "calm", "theme": "", "energy": "low",
            "bpm_range": "80-90", "duck_intensity": "light", "alternatives": [],
        }


def _words(a, b, step=0.5):
    """One word every `step` seconds from a to b."""
    out = []
    t = a
    while t < b:
        out.append({"word": f"w{int(t * 10)}", "start": round(t, 3), "end": round(t + step * 0.8, 3)})
        t += step
    return out


def _segments(spans):
    """One ASR sentence per (start, end) span, speaker 0."""
    return [{"start": a, "end": b, "speaker": 0, "words": _words(a, b)} for a, b in spans]


def _cand(start, end):
    return {"start": float(start), "end": float(end), "curve_score": 0.5, "channel_scores": {}}


def _ctx(tmp_path, segments, cands, timeline=(), duration=300.0):
    curves = tmp_path / "curves.json"
    curves.write_text(json.dumps({"arousal": [0.1] * int(duration * 2), "arousal_grid_sec": 0.5}))
    messages = []
    return SimpleNamespace(
        prior={
            "ingest": {"heatmap": None, "probe": {"duration_sec": duration}, "media_path": str(tmp_path / "x.mp4")},
            "diarize": {"segments": segments},
            "events": {"timeline": list(timeline), "curves_path": str(curves)},
            "candidates": {"candidates": cands},
        },
        settings=config.Settings(),
        job_dir=tmp_path,
        emit=lambda f, m: messages.append(m),
        messages=messages,
    )


def _run(monkeypatch, ctx, client):
    monkeypatch.setattr(llm_mod, "make_client", lambda mode, notify=None: client)
    from publikclip_pipeline.scoring import frames as frames_mod
    monkeypatch.setattr(frames_mod, "extract_frames", lambda *a, **k: [])
    return ScoreStage().run(ctx)


def _rules(clip):
    return [a["rule"] for a in clip["adjustments"]]


# --- speech, not air --------------------------------------------------------

def test_trailing_silence_is_cut_from_the_clip(monkeypatch, tmp_path):
    # 42 s window, people talk for the first 20 s, then nothing.
    ctx = _ctx(tmp_path, _segments([(0, 20)]), [_cand(0, 42)])
    out = _run(monkeypatch, ctx, FakeClient())
    clip = out["clips"][0]
    assert clip["start"] == 0.0
    assert 20.0 <= clip["end"] <= 21.5
    assert clip["speech_coverage"] >= 0.9
    assert "dead_air" not in _rules(clip)


def test_a_window_with_too_little_speech_is_never_a_clip(monkeypatch, tmp_path):
    # A: 10 s of talk then 32 s of air — shorter than a clip once trimmed.
    # B: a full 42 s of talk.
    segments = _segments([(0, 10), (100, 142)])
    ctx = _ctx(tmp_path, segments, [_cand(0, 42), _cand(100, 142)])
    out = _run(monkeypatch, ctx, FakeClient())
    assert [c["start"] for c in out["clips"]] == [100.0]


def test_dead_air_in_the_middle_lowers_the_score(monkeypatch, tmp_path):
    # A talks 0–12 and 30–42 (air in the middle); B talks the whole way.
    segments = _segments([(0, 12), (30, 42), (100, 142)])
    ctx = _ctx(tmp_path, segments, [_cand(0, 42), _cand(100, 142)])
    out = _run(monkeypatch, ctx, FakeClient())
    first, second = out["clips"]
    assert first["start"] == 100.0, "the full-speech moment ranks first"
    assert "dead_air" in _rules(second)
    factor = next(a["factor"] for a in second["adjustments"] if a["rule"] == "dead_air")
    assert rubric.DEAD_AIR_FLOOR <= factor < 1.0
    assert second["score"] < first["score"]


def test_a_silent_span_with_the_room_screaming_is_not_dead_air(monkeypatch, tmp_path):
    # A talks 0–12 only; from 12 to 42 nobody speaks but vocal arousal is in
    # the video's top quarter (a sprint, a lift, a crowd). B talks all the way.
    import numpy as np
    from publikclip_pipeline.scoring import stage as stage_mod
    segments = _segments([(0, 12), (100, 142)])
    ctx = _ctx(tmp_path, segments, [_cand(0, 42), _cand(100, 142)])
    arousal = [0.1] * 600
    for i in range(24, 84):  # 12 s … 42 s on a 0.5 s grid
        arousal[i] = 0.9
    (tmp_path / "curves.json").write_text(json.dumps({"arousal": arousal, "arousal_grid_sec": 0.5}))
    out = _run(monkeypatch, ctx, FakeClient())
    a = next(c for c in out["clips"] if c["start"] == 0.0)
    assert a["end"] == 42.0, "nothing trimmed: the screaming counts as action"
    assert a["speech_coverage"] < 0.4 and a["activity_coverage"] >= 0.9
    assert "dead_air" not in _rules(a)
    # a flat curve reads as all-top, which the saturation guard then ignores
    flat = stage_mod._per_sec_pct(np.full(100, 0.3), 0.5, 50)
    assert flat.min() == 1.0
    assert not stage_mod._activity([], [], flat, np.zeros(50), 50).any()
    # a plateau at the top covering a third of the video is the top
    bumpy = np.r_[np.zeros(40), np.full(20, 0.9)]
    pct = stage_mod._per_sec_pct(bumpy, 1.0, 60)
    assert (pct[40:] >= 0.75).all() and (pct[:40] < 0.75).all()
    assert stage_mod._activity([], [], pct, np.zeros(60), 60)[40:].all()


def test_a_trimmed_start_snaps_to_the_sentence_that_begins_there(monkeypatch, tmp_path):
    # 0–8 s: nothing; the first sentence starts at 8.0.
    segments = _segments([(8, 20), (20, 42), (100, 142)])
    ctx = _ctx(tmp_path, segments, [_cand(0, 42), _cand(100, 142)])
    out = _run(monkeypatch, ctx, FakeClient())
    a = min(out["clips"], key=lambda c: c["start"])
    assert a["start"] == 8.0


# --- ends on the payoff -----------------------------------------------------

def test_the_clip_ends_after_the_punchline(monkeypatch, tmp_path):
    # Sentences of 10 s; the LLM says word 25 (t = 12.5 s) lands the laugh.
    segments = _segments([(0, 10), (10, 20), (20, 30), (30, 40), (40, 50), (200, 242)])
    ctx = _ctx(tmp_path, segments, [_cand(0, 42), _cand(200, 242)])
    out = _run(monkeypatch, ctx, FakeClient(per_index={0: {"punchline_index": 25}}))
    clip = next(c for c in out["clips"] if c["start"] == 0.0)
    assert clip["end"] == 21.0, "end of the sentence that lands it, plus a breath"
    assert "ends_after_punchline" in _rules(clip)


def test_a_punchline_near_the_end_cuts_nothing(monkeypatch, tmp_path):
    segments = _segments([(0, 10), (10, 20), (20, 30), (30, 40), (40, 50), (200, 242)])
    ctx = _ctx(tmp_path, segments, [_cand(0, 42), _cand(200, 242)])
    out = _run(monkeypatch, ctx, FakeClient(per_index={0: {"punchline_index": 78}}))  # t = 39 s
    clip = next(c for c in out["clips"] if c["start"] == 0.0)
    assert clip["end"] == 42.0
    assert "ends_after_punchline" not in _rules(clip)


def test_a_punchline_that_would_leave_a_stub_cuts_nothing(monkeypatch, tmp_path):
    segments = _segments([(0, 10), (10, 20), (20, 30), (30, 40), (40, 50), (200, 242)])
    ctx = _ctx(tmp_path, segments, [_cand(0, 42), _cand(200, 242)])
    out = _run(monkeypatch, ctx, FakeClient(per_index={0: {"punchline_index": 4}}))  # t = 2 s
    assert next(c for c in out["clips"] if c["start"] == 0.0)["end"] == 42.0


# --- twelve clips are twelve moments ---------------------------------------

def test_finalists_do_not_overlap_by_more_than_ten_seconds(monkeypatch, tmp_path):
    segments = _segments([(0, 300)])
    cands = [_cand(5 * i, 5 * i + 42) for i in range(30)]
    ctx = _ctx(tmp_path, segments, cands)
    out = _run(monkeypatch, ctx, FakeClient())
    clips = out["clips"]
    assert len(clips) >= 5
    for a in clips:
        for b in clips:
            if a is not b:
                assert min(a["end"], b["end"]) - max(a["start"], b["start"]) <= 10.0
    assert len(out["shortlist"]) == 30, "every rated window stays on the shortlist"


def test_a_moment_the_llm_says_needs_context_ranks_below_a_whole_one(monkeypatch, tmp_path):
    segments = _segments([(0, 300)])
    ctx = _ctx(tmp_path, segments, [_cand(0, 42), _cand(100, 142)])
    out = _run(monkeypatch, ctx, FakeClient(per_index={0: {"self_contained": False}}))
    first, second = out["clips"]
    assert first["start"] == 100.0
    assert "needs_context" in _rules(second)


# --- the laughter detector must be alive before silence counts --------------

def _laugh(t):
    return {"type": "laugh", "start": t, "end": t + 1.0, "confidence": 0.9, "sources": ["panns"]}


def test_humour_is_not_discounted_when_the_detector_heard_nothing(monkeypatch, tmp_path):
    segments = _segments([(0, 300)])
    ctx = _ctx(tmp_path, segments, [_cand(0, 42), _cand(200, 242)])
    out = _run(monkeypatch, ctx, FakeClient(per_index={0: {"funniness": 8}}))
    clip = next(c for c in out["clips"] if c["start"] == 0.0)
    assert clip["subscores"]["funniness"] == 8.0
    assert "funny_no_laugh" not in _rules(clip)
    assert clip["laugh_detector_alive"] is False
    assert any("laughter (the detector heard none" in s for s in clip["signals_missing"])
    assert any("laughter detector heard 0 laughs" in m for m in ctx.messages)


def test_humour_without_laughter_is_discounted_when_laughter_was_heard_elsewhere(monkeypatch, tmp_path):
    segments = _segments([(0, 300)])
    # two laughs in a 5-minute source = 24 an hour: the detector works here
    ctx = _ctx(tmp_path, segments, [_cand(0, 42), _cand(100, 142)], timeline=[_laugh(200), _laugh(250)])
    out = _run(monkeypatch, ctx, FakeClient(per_index={0: {"funniness": 8}}))
    clip = next(c for c in out["clips"] if c["start"] == 0.0)
    assert "funny_no_laugh" in _rules(clip)
    assert clip["laugh_detector_alive"] is True


# --- the rubric math, and its replay for calibration ------------------------

def _sub():
    return {"hook": 6.0, "funniness": 5.0, "shock": 2.0, "curiosity_gap": 4.0, "value": 5.0}


def test_dead_air_factor_scales_with_coverage_and_has_a_floor():
    full, adj = rubric.composite(_sub(), 0.4, None, None, speech_coverage=0.9)
    assert adj == []
    half, adj = rubric.composite(_sub(), 0.4, None, None, speech_coverage=0.5)
    assert [a["rule"] for a in adj] == ["dead_air"]
    assert adj[0]["factor"] == pytest.approx(0.5 / rubric.DEAD_AIR_FULL, abs=1e-3)
    assert half["tiktok"] == pytest.approx(full["tiktok"] * adj[0]["factor"], abs=0.11)
    _, adj = rubric.composite(_sub(), 0.4, None, None, speech_coverage=0.05)
    assert adj[0]["factor"] == rubric.DEAD_AIR_FLOOR


def test_needs_context_is_a_recorded_composite_penalty():
    whole, _ = rubric.composite(_sub(), 0.4, None, None, self_contained=True)
    part, adj = rubric.composite(_sub(), 0.4, None, None, self_contained=False)
    assert [a["rule"] for a in adj] == ["needs_context"]
    assert part["reels"] == pytest.approx(whole["reels"] * rubric.NEEDS_CONTEXT, abs=0.11)
    _, adj = rubric.composite(_sub(), 0.4, None, None, self_contained=None)
    assert adj == []


def test_silent_detector_skips_the_funny_discount_only():
    t1 = _t1(funniness=8)
    sub, adj = rubric.cross_validate(t1, laughs_near=[], arousal_pct=0.9, heatmap_pct=None, laugh_detector_alive=False)
    assert sub["funniness"] == 8.0 and adj == []
    sub, adj = rubric.cross_validate(t1, laughs_near=[], arousal_pct=0.9, heatmap_pct=None, laugh_detector_alive=True)
    assert sub["funniness"] < 8.0 and [a["rule"] for a in adj] == ["funny_no_laugh"]


def test_recompute_replays_a_clip_scored_under_the_new_rules_exactly():
    t1 = _t1(funniness=8, self_contained=False)
    sub, adj = rubric.cross_validate(t1, laughs_near=[], arousal_pct=0.3, heatmap_pct=None, laugh_detector_alive=False)
    scores, comp_adj = rubric.composite(
        sub, 0.4, None, None, constants=constants_mod.DEFAULTS, speech_coverage=0.5, self_contained=False
    )
    entry = {
        "t1_raw": t1, "subscores": sub, "adjustments": adj + comp_adj, "arousal_pct": 0.3, "heatmap_pct": None,
        "curve_score": 0.4, "t2": None, "speech_coverage": 0.5, "laugh_detector_alive": False,
        "platform_scores": scores,
    }
    assert rubric.recompute_platform_scores(entry, constants_mod.DEFAULTS) == scores


def test_recompute_leaves_a_clip_scored_before_the_rules_alone():
    # No speech_coverage stored → scored by 0.2.6 or earlier → self_contained
    # false must NOT be penalised on replay.
    t1 = _t1(self_contained=False)
    sub, adj = rubric.cross_validate(t1, laughs_near=[{"sources": ["a"]}], arousal_pct=0.9, heatmap_pct=None)
    scores, comp_adj = rubric.composite(sub, 0.4, None, None, constants=constants_mod.DEFAULTS)
    entry = {"t1_raw": t1, "subscores": sub, "adjustments": adj + comp_adj, "arousal_pct": 0.9,
             "heatmap_pct": None, "curve_score": 0.4, "t2": None, "platform_scores": scores}
    assert rubric.recompute_platform_scores(entry, constants_mod.DEFAULTS) == scores
