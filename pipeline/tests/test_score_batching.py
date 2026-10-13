"""JUDGE rates several moments per call on a Gemini-class brain.

Oct 11 2026, Windows, free Gemini key: ~35 single T1 calls per video against
a 5-a-minute / 20-a-day quota. Now T1_BATCH_SIZE moments share one call; a
moment the answer skips is rated again on its own; Ollama stays one-at-a-time.
No HTTP here — the client is a fake."""

import json
from types import SimpleNamespace

import pytest

from publikclip_pipeline import config
from publikclip_pipeline.jobs.queue import StageError
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
    """Answers batch prompts with one entry per moment heading, unless told
    to skip some; answers single prompts and T2/music with fixed data."""

    def __init__(self, backend="gemini", skip=(), batch_error=None):
        self.backend = backend
        self.model = "fake-model"
        self.calls: list[dict] = []
        self.skip = set(skip)
        self.batch_error = batch_error

    def generate_json(self, prompt, schema, images=None):
        self.calls.append({"prompt": prompt, "schema": schema, "images": images})
        if schema is rubric.T1_BATCH_SCHEMA:
            if self.batch_error:
                raise self.batch_error
            indices = [int(line.split()[2]) for line in prompt.splitlines() if line.startswith("=== Moment ")]
            return {"moments": [_t1(i) for i in indices if i not in self.skip]}
        if schema is rubric.T1_SCHEMA:
            return _t1()
        if schema is rubric.T2_SCHEMA:
            return {"visual_interest": 5, "faces_visible": True, "expressive_peak": False, "on_screen_text": False, "notes": ""}
        return {
            "genre": "lo-fi", "instruments": [], "mood": "calm", "theme": "", "energy": "low",
            "bpm_range": "80-90", "duck_intensity": "light", "alternatives": [],
        }


def _ctx(tmp_path, n_candidates):
    curves = tmp_path / "curves.json"
    curves.write_text(json.dumps({"arousal": [0.1] * 600, "arousal_grid_sec": 0.5}))
    words = [{"word": f"w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(600)]
    cands = [
        {"start": i * 25.0, "end": i * 25.0 + 24.0, "curve_score": 0.5, "channel_scores": {}}
        for i in range(n_candidates)
    ]
    messages = []
    ctx = SimpleNamespace(
        prior={
            "ingest": {"heatmap": None, "probe": {"duration_sec": 300}, "media_path": str(tmp_path / "x.mp4")},
            "diarize": {"segments": [{"start": 0, "end": 300, "speaker": 0, "words": words}]},
            "events": {"timeline": [], "curves_path": str(curves)},
            "candidates": {"candidates": cands},
        },
        settings=config.Settings(),
        job_dir=tmp_path,
        emit=lambda f, m: messages.append(m),
        messages=messages,
    )
    return ctx


def _run(monkeypatch, tmp_path, client, n=12):
    monkeypatch.setattr(llm_mod, "make_client", lambda mode, notify=None: client)
    # no frames to extract in a test: the visual pass gets no images
    from publikclip_pipeline.scoring import frames as frames_mod
    monkeypatch.setattr(frames_mod, "extract_frames", lambda *a, **k: [])
    ctx = _ctx(tmp_path, n)
    return ScoreStage().run(ctx), client, ctx


def _t1_calls(client):
    return [c for c in client.calls if c["schema"] in (rubric.T1_SCHEMA, rubric.T1_BATCH_SCHEMA)]


def test_gemini_rates_five_moments_per_call(monkeypatch, tmp_path):
    data, client, ctx = _run(monkeypatch, tmp_path, FakeClient(), n=12)
    t1 = _t1_calls(client)
    assert len(t1) == 3 and all(c["schema"] is rubric.T1_BATCH_SCHEMA for c in t1)
    assert data["scored_count"] == 12
    # every moment kept its own answer (the summary names its index)
    assert sorted(c["summary"] for c in data["clips"]) == sorted(f"moment {i}" for i in range(12))
    assert any("Scoring moments 1–5 of 12" in m for m in ctx.messages)


def test_a_moment_the_batch_skipped_is_rated_on_its_own(monkeypatch, tmp_path):
    data, client, _ = _run(monkeypatch, tmp_path, FakeClient(skip={3, 7}), n=12)
    t1 = _t1_calls(client)
    assert [c["schema"] is rubric.T1_BATCH_SCHEMA for c in t1] == [True, False, True, False, True]
    assert data["scored_count"] == 12


def test_an_incomplete_batch_entry_counts_as_skipped(monkeypatch, tmp_path):
    client = FakeClient()
    real = client.generate_json

    def lossy(prompt, schema, images=None):
        answer = real(prompt, schema, images)
        if schema is rubric.T1_BATCH_SCHEMA:
            answer["moments"][0].pop("hook")  # the model forgot a field
        return answer

    client.generate_json = lossy
    data, _, _ = _run(monkeypatch, tmp_path, client, n=5)
    assert data["scored_count"] == 5
    assert [c["schema"] is rubric.T1_BATCH_SCHEMA for c in _t1_calls(client)] == [True, False]


def test_ollama_rates_one_moment_per_call(monkeypatch, tmp_path):
    data, client, _ = _run(monkeypatch, tmp_path, FakeClient(backend="ollama"), n=7)
    t1 = _t1_calls(client)
    assert len(t1) == 7 and all(c["schema"] is rubric.T1_SCHEMA for c in t1)
    assert data["scored_count"] == 7


def test_a_batch_that_crashes_falls_back_to_single_calls(monkeypatch, tmp_path):
    data, client, ctx = _run(monkeypatch, tmp_path, FakeClient(batch_error=ValueError("bad json")), n=5)
    assert data["scored_count"] == 5
    assert [c["schema"] is rubric.T1_BATCH_SCHEMA for c in _t1_calls(client)] == [True] + [False] * 5
    assert any("scoring one by one" in m for m in ctx.messages)


def test_a_quota_stop_in_a_batch_ends_the_stage_with_the_message(monkeypatch, tmp_path):
    err = llm_mod.QuotaExhaustedError("Your Gemini key has used today's free requests")
    with pytest.raises(StageError) as exc:
        _run(monkeypatch, tmp_path, FakeClient(batch_error=err), n=5)
    assert "today's free requests" in str(exc.value)


def test_short_moments_never_cost_a_call(monkeypatch, tmp_path):
    client = FakeClient()
    monkeypatch.setattr(llm_mod, "make_client", lambda mode, notify=None: client)
    ctx = _ctx(tmp_path, 3)
    ctx.prior["candidates"]["candidates"][1] = {"start": 1000.0, "end": 1001.0, "curve_score": 0.1, "channel_scores": {}}
    from publikclip_pipeline.scoring import frames as frames_mod
    monkeypatch.setattr(frames_mod, "extract_frames", lambda *a, **k: [])
    data = ScoreStage().run(ctx)
    batch = [c for c in client.calls if c["schema"] is rubric.T1_BATCH_SCHEMA]
    assert len(batch) == 1 and "=== Moment 1 " not in batch[0]["prompt"]
    assert data["scored_count"] == 2


def test_single_prompt_text_is_unchanged_so_old_cache_entries_still_hit():
    text = rubric.t1_prompt("S0: hi there", {"duration": 25, "events_desc": "none detected"})
    assert text.startswith("You are rating a candidate short-form clip cut from a longer video. ")
    assert text.endswith("of the word where the biggest laugh lands, or -1.")
    assert "Speakers and transcript (25 seconds):\nS0: hi there\n\nAudio events detected in this span: none detected\n\n" in text


def test_batch_prompt_names_every_moment_by_index():
    text = rubric.t1_batch_prompt([(4, "S0: a", {"duration": 20}), (9, "S1: b", {"duration": 30, "events_desc": "laugh at 3s"})])
    assert "=== Moment 4 (20 seconds) ===" in text and "=== Moment 9 (30 seconds) ===" in text
    assert "laugh at 3s" in text and "There are 2 moments" in text
    assert rubric.T1_BATCH_SCHEMA["properties"]["moments"]["items"]["required"][0] == "index"
