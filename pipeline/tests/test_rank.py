"""T1.5: the brain ranks every rated moment side by side; its pick order is a
recorded, replayable composite bonus. On a gym stream the rubric alone put
the host's intro above the lift viewers replayed most."""

import json
from types import SimpleNamespace

import pytest

from publikclip_pipeline import config
from publikclip_pipeline.scoring import constants as constants_mod
from publikclip_pipeline.scoring import llm as llm_mod
from publikclip_pipeline.scoring import propose
from publikclip_pipeline.scoring import rank
from publikclip_pipeline.scoring import rubric
from publikclip_pipeline.scoring.stage import ScoreStage


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PUBLIKCLIP_NO_RANK", raising=False)
    yield


def _entry(start, end, **over):
    e = {
        "start": start, "end": end, "arousal_pct": 0.97, "speech_coverage": 0.5, "source": "llm",
        "title": "the lift", "events_in_window": ["cheer", "laugh"],
        "t1_raw": {"summary": "Speed gets stuck under the bar"},
        "subscores": {"hook": 3.0, "funniness": 4.0, "shock": 2.0, "curiosity_gap": 1.0, "value": 1.0},
        "transcript": "S0: " + " ".join(f"w{i}" for i in range(100)),
    }
    e.update(over)
    return e


def test_the_ranker_sees_time_rubric_audio_and_both_ends_of_the_words():
    text = rank.describe(_entry(520.0, 549.0), 7)
    assert text.startswith("=== Candidate 7 [08:40–09:09, 29 s] ===")
    assert "What happens: Speed gets stuck under the bar" in text
    assert "proposed by you from the transcript (\"the lift\")" in text
    assert "vocal arousal in the top 3% of the video" in text
    assert "audio: cheer, laugh" in text
    assert "speech or action 50% of the time" in text
    assert "w0 w1" in text and " … " in text and "w99" in text


def test_the_prompt_asks_for_a_best_first_list_and_rules_out_intros():
    p = rank.rank_prompt([(0, _entry(0, 20)), (1, _entry(30, 50))], want=12)
    assert "Pick the 12 best, best first" in p
    assert "introduction" in p and "=== Candidate 1 " in p


def test_parse_keeps_order_drops_unknown_and_repeats():
    answer = {"picks": [{"index": 4, "why": "a"}, {"index": 9, "why": "b"}, {"index": 4, "why": "c"},
                        {"index": "x", "why": ""}, "junk", {"index": 2, "why": "d"}]}
    assert rank.parse(answer, valid={2, 4, 5}) == [4, 2]
    assert rank.parse("garbage", valid={1}) == []
    assert rank.reasons(answer)[9] == "b"


def test_bonus_falls_from_a_quarter_to_nothing():
    assert rank.bonus(1) == pytest.approx(1.0 + rank.RANK_BONUS)
    assert rank.bonus(rank.RANK_POOL) == pytest.approx(1.0 + rank.RANK_BONUS / rank.RANK_POOL)
    assert rank.bonus(rank.RANK_POOL + 5) == 1.0
    assert rank.bonus(None) == 1.0


class Brain:
    def __init__(self, answer=None, raise_=None):
        self.backend = "gemini"; self.model = "fake"
        self.answer = answer if answer is not None else {"picks": []}
        self.raise_ = raise_; self.prompts = []

    def generate_json(self, prompt, schema, images=None):
        if self.raise_:
            raise self.raise_
        self.prompts.append(prompt)
        return self.answer


def test_rank_returns_one_based_positions_and_reasons():
    brain = Brain(answer={"picks": [{"index": 5, "why": "the fall"}, {"index": 2, "why": "the roast"}]})
    picks, whys = rank.rank(brain, [(2, _entry(0, 20)), (5, _entry(30, 50)), (8, _entry(60, 80))], 12, lambda f, m: None)
    assert picks == {5: 1, 2: 2} and whys[5] == "the fall"
    assert len(brain.prompts) == 1


def test_one_candidate_is_not_ranked_and_the_switch_turns_it_off(monkeypatch):
    brain = Brain()
    assert rank.rank(brain, [(0, _entry(0, 20))], 12, lambda f, m: None) == ({}, {})
    brain.backend = "ollama"
    assert rank.rank(brain, [(0, _entry(0, 20)), (1, _entry(30, 50))], 12, lambda f, m: None) == ({}, {})
    brain.backend = "gemini"
    monkeypatch.setenv("PUBLIKCLIP_NO_RANK", "1")
    assert rank.rank(brain, [(0, _entry(0, 20)), (1, _entry(30, 50))], 12, lambda f, m: None) == ({}, {})
    assert brain.prompts == []


def test_the_pick_is_a_recorded_composite_bonus_that_replays():
    sub = {"hook": 3.0, "funniness": 4.0, "shock": 2.0, "curiosity_gap": 1.0, "value": 1.0}
    plain, _ = rubric.composite(sub, 0.4, None, None, constants=constants_mod.DEFAULTS)
    picked, adj = rubric.composite(sub, 0.4, None, None, constants=constants_mod.DEFAULTS, brain_rank=1, brain_why="the fall")
    assert picked["tiktok"] == pytest.approx(min(100.0, plain["tiktok"] * (1 + rank.RANK_BONUS)), abs=0.11)
    assert [a["rule"] for a in adj] == ["brain_rank"]
    assert adj[0]["reason"] == "pick #1 when the brain ranked every moment side by side: the fall"
    entry = {"t1_raw": {"hook": 3, "funniness": 4, "shock": 2, "curiosity_gap": 1, "value": 1, "bait_phrases": [],
                        "self_contained": True},
             "subscores": sub, "adjustments": adj, "arousal_pct": 0.9, "heatmap_pct": None, "curve_score": 0.4,
             "t2": None, "speech_coverage": 0.9, "laugh_detector_alive": False, "brain_rank": 1, "platform_scores": picked}
    assert rubric.recompute_platform_scores(entry, constants_mod.DEFAULTS) == picked


# --- through the stage ----------------------------------------------------------

def _words(a, b, step=0.5):
    out, t = [], a
    while t < b:
        out.append({"word": f"w{int(t * 10)}", "start": round(t, 3), "end": round(t + step * 0.8, 3)})
        t += step
    return out


def _t1(index=None, **over):
    entry = {"hook": 5, "hook_type": "question", "funniness": 3, "punchline_index": -1, "shock": 2,
             "curiosity_gap": 4, "value": 5, "self_contained": True, "bait_phrases": [], "summary": f"moment {index}"}
    if index is not None:
        entry["index"] = index
    entry.update(over)
    return entry


class StageBrain(Brain):
    def __init__(self, picks, weak=()):
        super().__init__(answer={"picks": picks})
        self.weak = set(weak)

    def generate_json(self, prompt, schema, images=None):
        if schema is rank.RANK_SCHEMA:
            self.prompts.append(prompt)
            return self.answer
        if schema is propose.PROPOSE_SCHEMA:
            return {"moments": []}
        if schema is rubric.T1_BATCH_SCHEMA:
            indices = [int(line.split()[2]) for line in prompt.splitlines() if line.startswith("=== Moment ")]
            return {"moments": [_t1(i, hook=1, value=1) if i in self.weak else _t1(i) for i in indices]}
        if schema is rubric.T2_SCHEMA:
            return {"visual_interest": 5, "faces_visible": True, "expressive_peak": False, "on_screen_text": False, "notes": ""}
        return {"genre": "lo-fi", "instruments": [], "mood": "calm", "theme": "", "energy": "low",
                "bpm_range": "80-90", "duck_intensity": "light", "alternatives": []}


def test_the_brains_pick_order_leads_the_finalists(monkeypatch, tmp_path):
    curves = tmp_path / "curves.json"
    curves.write_text(json.dumps({"arousal": [0.1] * 600, "arousal_grid_sec": 0.5}))
    segments = [{"start": 0, "end": 300, "speaker": 0, "words": _words(0, 300)}]
    cands = [{"start": 100.0 * i, "end": 100.0 * i + 42.0, "peak_time": 100.0 * i + 20, "curve_score": 0.5, "channel_scores": {}} for i in range(3)]
    # three equal rubrics; the brain picks 2 then 0, leaving 1 unpicked
    brain = StageBrain(picks=[{"index": 2, "why": "the fall"}, {"index": 0, "why": "the roast"}])
    monkeypatch.setattr(llm_mod, "make_client", lambda mode, notify=None: brain)
    from publikclip_pipeline.scoring import frames as frames_mod
    monkeypatch.setattr(frames_mod, "extract_frames", lambda *a, **k: [])
    messages = []
    ctx = SimpleNamespace(
        prior={"ingest": {"heatmap": None, "probe": {"duration_sec": 300}, "media_path": str(tmp_path / "x.mp4")},
               "diarize": {"segments": segments}, "events": {"timeline": [], "curves_path": str(curves)},
               "candidates": {"candidates": cands}},
        settings=config.Settings(), job_dir=tmp_path, emit=lambda f, m: messages.append(m), messages=messages)
    out = ScoreStage().run(ctx)
    clips = out["clips"]
    assert clips[0]["start"] == 200.0 and clips[0]["brain_rank"] == 1
    assert "brain_rank" in [a["rule"] for a in clips[0]["adjustments"]]
    assert clips[0]["summary"] == "moment 2"
    assert clips[1]["start"] == 0.0 and clips[1]["brain_rank"] == 2
    assert clips[2]["start"] == 100.0 and "brain_rank" not in clips[2]
    assert clips[0]["score"] > clips[1]["score"] > clips[2]["score"]
    assert "=== Candidate 2 " in brain.prompts[0] and "Found as: a loudness peak" in brain.prompts[0]
    assert any("picked 2 moments" in m for m in messages)
    assert all("i" not in c and "events_in_window" not in c for c in clips)


# --- frames ---------------------------------------------------------------------

def test_frame_times_sit_near_the_setup_and_the_payoff():
    assert rank.frame_times({"start": 100.0, "end": 140.0}) == [114.0, 134.0]


def test_the_prompt_explains_the_frames_only_when_they_come_along():
    entries = [(0, _entry(0, 20)), (1, _entry(30, 50))]
    assert "frames per candidate" not in rank.rank_prompt(entries, 12)
    with_frames = rank.rank_prompt(entries, 12, frames_per=2)
    assert "After this text come 2 frames per candidate" in with_frames
    assert "a talking head counts less than one" in with_frames


def test_frames_are_sent_only_when_every_candidate_has_its_two():
    class Seeing(Brain):
        def generate_json(self, prompt, schema, images=None):
            self.prompts.append((prompt, images))
            return self.answer
    entries = [(0, _entry(0, 20)), (1, _entry(30, 50))]
    brain = Seeing(answer={"picks": [{"index": 1, "why": "the fall"}]})
    rank.rank(brain, entries, 12, lambda f, m: None, images=[b"a", b"b", b"c", b"d"])
    prompt, images = brain.prompts[-1]
    assert images == [b"a", b"b", b"c", b"d"] and "2 frames per candidate" in prompt
    rank.rank(brain, entries, 12, lambda f, m: None, images=[b"a", b"b", b"c"])  # one short
    prompt, images = brain.prompts[-1]
    assert images is None and "frames per candidate" not in prompt


def test_the_stage_pulls_two_frames_per_rated_moment_for_the_ranker(monkeypatch, tmp_path):
    curves = tmp_path / "curves.json"
    curves.write_text(json.dumps({"arousal": [0.1] * 600, "arousal_grid_sec": 0.5}))
    segments = [{"start": 0, "end": 300, "speaker": 0, "words": _words(0, 300)}]
    cands = [{"start": 100.0 * i, "end": 100.0 * i + 42.0, "peak_time": 100.0 * i + 20, "curve_score": 0.5, "channel_scores": {}} for i in range(3)]

    class Seeing(StageBrain):
        def generate_json(self, prompt, schema, images=None):
            if schema is rank.RANK_SCHEMA:
                self.seen = images
            return super().generate_json(prompt, schema, images)

    brain = Seeing(picks=[{"index": 1, "why": "the fall"}])
    monkeypatch.setattr(llm_mod, "make_client", lambda mode, notify=None: brain)
    from publikclip_pipeline.scoring import frames as frames_mod
    pulled = []

    def fake_extract(media, times, tmp_dir):
        pulled.append(list(times))
        return [b"jpg%d" % len(pulled)] * len(times) if str(tmp_dir).endswith("rankframes") else []

    monkeypatch.setattr(frames_mod, "extract_frames", fake_extract)
    messages = []
    ctx = SimpleNamespace(
        prior={"ingest": {"heatmap": None, "probe": {"duration_sec": 300}, "media_path": str(tmp_path / "x.mp4")},
               "diarize": {"segments": segments}, "events": {"timeline": [], "curves_path": str(curves)},
               "candidates": {"candidates": cands}},
        settings=config.Settings(), job_dir=tmp_path, emit=lambda f, m: messages.append(m), messages=messages)
    out = ScoreStage().run(ctx)
    assert len(brain.seen) == 6, "two frames for each of the three rated moments"
    assert [t for t in pulled if len(t) == 2][:3] == [[14.7, 35.7], [114.7, 135.7], [214.7, 235.7]]
    assert any("with frames" in m for m in messages)
    assert out["clips"][0]["brain_rank"] == 1
