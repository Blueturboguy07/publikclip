"""T0.5: the brain reads the whole transcript and proposes moments by
sentence number; they join the curve's windows and the same rubric rates
them. Oct 13 2026: the curve's 42 s windows left the payoff outside the
clip."""

import json
from types import SimpleNamespace

import pytest

from publikclip_pipeline import config
from publikclip_pipeline.scoring import llm as llm_mod
from publikclip_pipeline.scoring import propose
from publikclip_pipeline.scoring import rubric
from publikclip_pipeline.scoring.stage import ScoreStage


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PUBLIKCLIP_NO_PROPOSE", raising=False)
    yield


def _words(a, b, step=0.5):
    out, t = [], a
    while t < b:
        out.append({"word": f"w{int(t * 10)}", "start": round(t, 3), "end": round(t + step * 0.8, 3)})
        t += step
    return out


def _segments(spans, speaker=0):
    return [{"start": a, "end": b, "speaker": speaker, "words": _words(a, b)} for a, b in spans]


# --- lines and chunks ---------------------------------------------------------

def test_lines_are_numbered_timestamped_sentences():
    segs = _segments([(0, 5), (5, 10)]) + [{"start": 10, "end": 12, "speaker": 1, "words": []}]
    lines = propose.lines_from(segs)
    assert [ln.idx for ln in lines] == [0, 1], "a sentence without words is not a line"
    text = propose.format_lines(lines)
    assert text.startswith("#0 [00:00] S0: w0 ")
    assert "\n#1 [00:05] S0: " in text


def test_an_hour_mark_prints_with_hours():
    assert propose._stamp(3725) == "1:02:05"
    assert propose._stamp(65) == "01:05"


def test_chunks_respect_the_word_budget():
    lines = propose.lines_from(_segments([(i * 10, i * 10 + 10) for i in range(10)]))  # 20 words each
    chunks = propose.chunk(lines, max_words=50)
    assert [len(c) for c in chunks] == [2, 2, 2, 2, 2]
    assert [ln.idx for ln in chunks[1]] == [2, 3]


# --- parsing the answer onto sentence bounds -------------------------------

def _lines(n=12, sec=10):
    return propose.lines_from(_segments([(i * sec, i * sec + sec) for i in range(n)]))


def test_a_moment_is_cut_on_the_sentences_named():
    lines = _lines()
    out = propose.parse({"moments": [{"first_line": 2, "last_line": 4, "title": "t", "payoff": "p"}]}, lines)
    assert len(out) == 1
    assert out[0]["start"] == 20.0 and out[0]["end"] == pytest.approx(49.9, abs=0.01)
    assert out[0]["source"] == "llm"


def test_a_moment_that_runs_long_is_cut_back_to_the_limit():
    lines = _lines()
    out = propose.parse({"moments": [{"first_line": 0, "last_line": 11, "title": "t", "payoff": "p"}]}, lines)
    assert out and out[0]["end"] - out[0]["start"] <= 75.0


def test_a_moment_that_runs_short_grows_to_the_next_sentences():
    lines = _lines(sec=5)  # 5 s sentences
    out = propose.parse({"moments": [{"first_line": 1, "last_line": 1, "title": "t", "payoff": "p"}]}, lines)
    assert out and out[0]["end"] - out[0]["start"] >= 15.0
    assert out[0]["start"] == 5.0


def test_bad_line_numbers_and_shapes_are_dropped():
    lines = _lines()
    answer = {"moments": [
        {"first_line": 5, "last_line": 2, "title": "", "payoff": ""},      # backwards
        {"first_line": 0, "last_line": 40, "title": "", "payoff": ""},     # off the end
        {"first_line": "x", "last_line": 2, "title": "", "payoff": ""},    # not a number
        "nope",
    ]}
    assert propose.parse(answer, lines) == []
    assert propose.parse("garbage", lines) == []


# --- merging with the curve's windows ---------------------------------------

def test_a_proposal_replaces_the_curve_window_it_mostly_overlaps():
    curve = [0.1] * 100 + [0.9] * 100 + [0.1] * 100
    curve_cands = [
        {"start": 110.0, "end": 150.0, "peak_time": 130.0, "curve_score": 0.9, "channel_scores": {}},
        {"start": 10.0, "end": 50.0, "peak_time": 30.0, "curve_score": 0.1, "channel_scores": {}},
    ]
    proposed = [{"start": 115.0, "end": 155.0, "title": "the one", "payoff": "lands", "source": "llm"}]
    merged = propose.merge(curve_cands, proposed, curve)
    assert [(c["start"], c["source"]) for c in merged] == [(10.0, "curve"), (115.0, "llm")]
    llm = merged[1]
    assert llm["curve_score"] == pytest.approx(0.9, abs=0.01), "scored on the curve like any window"
    assert llm["title"] == "the one"


def test_two_proposals_of_one_moment_keep_the_first():
    proposed = [
        {"start": 10.0, "end": 50.0, "title": "a", "payoff": "", "source": "llm"},
        {"start": 12.0, "end": 52.0, "title": "b", "payoff": "", "source": "llm"},
    ]
    merged = propose.merge([], proposed, [0.5] * 60)
    assert [c["title"] for c in merged] == ["a"]


# --- the call itself ----------------------------------------------------------

class Brain:
    def __init__(self, backend="gemini", answer=None, raise_=None):
        self.backend = backend
        self.model = "fake"
        self.answer = answer if answer is not None else {"moments": []}
        self.raise_ = raise_
        self.prompts: list[str] = []

    def generate_json(self, prompt, schema, images=None):
        if self.raise_:
            raise self.raise_
        self.prompts.append(prompt)
        return self.answer


def test_one_call_reads_a_whole_hour_on_gemini():
    segs = _segments([(i * 10, i * 10 + 10) for i in range(360)])  # 1 h, 7200 words
    brain = Brain()
    propose.propose(brain, segs, lambda f, m: None)
    assert len(brain.prompts) == 1
    assert f"Choose the {propose.PROPOSALS_PER_VIDEO} best moments" in brain.prompts[0]
    assert "#359 [59:50] S0:" in brain.prompts[0]


def test_a_small_local_model_reads_a_few_minutes_at_a_time():
    segs = _segments([(i * 10, i * 10 + 10) for i in range(360)])
    brain = Brain(backend="ollama")
    propose.propose(brain, segs, lambda f, m: None)
    assert len(brain.prompts) == 6  # 7200 words / 1200
    assert all("Choose the 3 best moments" in p for p in brain.prompts)


def test_the_switch_turns_the_call_off(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_NO_PROPOSE", "1")
    brain = Brain()
    assert propose.propose(brain, _segments([(0, 100)]), lambda f, m: None) == []
    assert brain.prompts == []


def test_a_quota_stop_propagates():
    brain = Brain(raise_=llm_mod.LlmError("quota"))
    with pytest.raises(llm_mod.LlmError):
        propose.propose(brain, _segments([(0, 100)]), lambda f, m: None)


# --- through the stage ---------------------------------------------------------

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


class StageBrain(Brain):
    def generate_json(self, prompt, schema, images=None):
        if schema is propose.PROPOSE_SCHEMA:
            self.prompts.append(prompt)
            return self.answer
        if schema is rubric.T1_BATCH_SCHEMA:
            indices = [int(line.split()[2]) for line in prompt.splitlines() if line.startswith("=== Moment ")]
            return {"moments": [_t1(i) for i in indices]}
        if schema is rubric.T1_SCHEMA:
            return _t1()
        if schema is rubric.T2_SCHEMA:
            return {"visual_interest": 5, "faces_visible": True, "expressive_peak": False, "on_screen_text": False, "notes": ""}
        return {"genre": "lo-fi", "instruments": [], "mood": "calm", "theme": "", "energy": "low",
                "bpm_range": "80-90", "duck_intensity": "light", "alternatives": []}


def _ctx(tmp_path, segments, cands, curve=None, duration=300.0):
    curves = tmp_path / "curves.json"
    curves.write_text(json.dumps({"arousal": [0.1] * int(duration * 2), "arousal_grid_sec": 0.5}))
    if curve is not None:
        (tmp_path / "interest_curve.json").write_text(json.dumps({"per_sec": curve}))
    messages = []
    return SimpleNamespace(
        prior={
            "ingest": {"heatmap": None, "probe": {"duration_sec": duration}, "media_path": str(tmp_path / "x.mp4")},
            "diarize": {"segments": segments},
            "events": {"timeline": [], "curves_path": str(curves)},
            "candidates": {"candidates": cands},
        },
        settings=config.Settings(),
        job_dir=tmp_path,
        emit=lambda f, m: messages.append(m),
        messages=messages,
    )


def test_the_brains_moment_joins_the_pool_and_is_rated_like_any_window(monkeypatch, tmp_path):
    segments = _segments([(i * 10, i * 10 + 10) for i in range(30)])
    cands = [
        {"start": 0.0, "end": 42.0, "peak_time": 20.0, "curve_score": 0.5, "channel_scores": {}},
        {"start": 200.0, "end": 242.0, "peak_time": 220.0, "curve_score": 0.5, "channel_scores": {}},
    ]
    brain = StageBrain(answer={"moments": [{"first_line": 1, "last_line": 4, "title": "the lift", "payoff": "he drops it"}]})
    monkeypatch.setattr(llm_mod, "make_client", lambda mode, notify=None: brain)
    from publikclip_pipeline.scoring import frames as frames_mod
    monkeypatch.setattr(frames_mod, "extract_frames", lambda *a, **k: [])
    ctx = _ctx(tmp_path, segments, cands, curve=[0.3] * 300)
    out = ScoreStage().run(ctx)
    assert out["proposed"] == [{"start": 10.0, "end": pytest.approx(49.9, abs=0.01), "title": "the lift", "payoff": "he drops it"}]
    starts = sorted((c["start"], c["source"]) for c in out["clips"])
    assert starts == [(10.0, "llm"), (200.0, "curve")], "the curve window it overlapped gave way"
    assert any("proposed 1 moment" in m for m in ctx.messages)
