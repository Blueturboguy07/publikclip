"""Auto-suggest tests — deterministic music/sfx picks from a fake library
and a fake clip brief, never touching real audio files."""

from __future__ import annotations

from publikclip_pipeline.audio_library.library import Item as LibItem
from publikclip_pipeline.edits import audio_suggest as sug


def _music(item_id, tags, bpm=None):
    return LibItem(id=item_id, path=f"/lib/music/{item_id}.mp3", kind="music", name=item_id,
                    duration=180.0, bpm=bpm, tags=tags)


def _sfx(item_id, tags, duration=1.0):
    return LibItem(id=item_id, path=f"/lib/sfx/{item_id}.wav", kind="sfx", name=item_id,
                    duration=duration, bpm=None, tags=tags)


# --------------------------------------------------------------------------
# bpm_range parsing
# --------------------------------------------------------------------------


def test_parse_bpm_range():
    assert sug._parse_bpm_range("80-95") == (80.0, 95.0)
    assert sug._parse_bpm_range("95-80") == (80.0, 95.0)  # reversed -> normalized
    assert sug._parse_bpm_range("120") is None
    assert sug._parse_bpm_range("") is None
    assert sug._parse_bpm_range(None) is None


# --------------------------------------------------------------------------
# Music
# --------------------------------------------------------------------------


def test_music_prefers_tag_and_bpm_match_over_brief():
    clip_result = {
        "music": {
            "genre": "lo-fi hip hop", "mood": "chill", "instruments": ["piano"],
            "bpm_range": "80-95", "energy": "low", "duck_intensity": "medium",
        },
        "arousal_pct": 0.3,
    }
    best = _music("best", tags=["lofi", "chill"], bpm=85.0)     # tags + bpm range + energy band all hit
    off_bpm = _music("off-bpm", tags=["lofi", "chill"], bpm=170.0)  # same tags, bpm well outside
    no_tags = _music("no-tags", tags=["rock"], bpm=85.0)         # bpm hits, no tag overlap

    result = sug.suggest(clip_result, [off_bpm, no_tags, best], events=[], keep_ranges=[(0.0, 20.0)], duration=20.0)
    music_items = [a for a in result if a.kind == "music"]
    assert len(music_items) == 1
    assert music_items[0].library_id == "best"
    assert music_items[0].start == 0.0
    assert music_items[0].duration == 20.0
    assert music_items[0].gain_db == sug.MUSIC_GAIN_DB
    assert music_items[0].fade_in == sug.MUSIC_FADE_IN
    assert music_items[0].fade_out == sug.MUSIC_FADE_OUT
    assert music_items[0].loop is True
    assert music_items[0].duck is True
    assert music_items[0].suggested is True


def test_music_no_brief_falls_back_to_mood_prior():
    clip_result = {"music": None, "arousal_pct": 0.1}  # low arousal -> "calm/reflective..." prior
    calm_item = _music("calm", tags=["calm", "reflective"])
    energetic_item = _music("hype", tags=["energetic", "loud"])

    result = sug.suggest(clip_result, [energetic_item, calm_item], events=[], keep_ranges=[(0.0, 10.0)], duration=10.0)
    music_items = [a for a in result if a.kind == "music"]
    assert len(music_items) == 1
    assert music_items[0].library_id == "calm"


def test_no_music_in_library_yields_no_music_suggestion():
    clip_result = {"music": {"genre": "pop", "mood": "fun", "bpm_range": "100-120", "energy": "medium"}}
    sfx_item = _sfx("whoosh1", tags=["whoosh"])
    result = sug.suggest(clip_result, [sfx_item], events=[], keep_ranges=[(0.0, 10.0)], duration=10.0)
    assert all(a.kind != "music" for a in result)


def test_music_still_suggested_with_zero_tag_overlap():
    """Some music beats none — even a zero-scoring item is still picked."""
    clip_result = {"music": {"genre": "polka", "mood": "obscure", "bpm_range": "999-999", "energy": "medium"}}
    only_item = _music("only", tags=["completely", "unrelated"])
    result = sug.suggest(clip_result, [only_item], events=[], keep_ranges=[(0.0, 5.0)], duration=5.0)
    music_items = [a for a in result if a.kind == "music"]
    assert len(music_items) == 1
    assert music_items[0].library_id == "only"


# --------------------------------------------------------------------------
# SFX
# --------------------------------------------------------------------------


def test_sfx_at_cut_boundaries_and_laugh_events():
    keep_ranges = [(0.0, 5.0), (5.0, 10.0), (10.0, 15.0)]  # cut boundaries at 5.0 and 10.0
    events = [{"type": "laugh", "start": 7.0, "end": 7.5}]
    whoosh = _sfx("whoosh1", tags=["whoosh"])
    laugh_sfx = _sfx("laugh1", tags=["laugh"])

    result = sug.suggest({"music": None}, [whoosh, laugh_sfx], events, keep_ranges, duration=15.0)
    sfx_items = sorted([a for a in result if a.kind == "sfx"], key=lambda a: a.start)
    assert [a.start for a in sfx_items] == [5.0, 7.0, 10.0]
    assert sfx_items[0].library_id == "whoosh1"
    assert sfx_items[1].library_id == "laugh1"
    assert sfx_items[2].library_id == "whoosh1"
    assert all(a.suggested for a in sfx_items)
    assert all(a.kind == "sfx" and a.loop is False and a.duck is False for a in sfx_items)


def test_sfx_skips_categories_with_no_matching_library_tag():
    """Only tags present in the library are used — never invent files."""
    keep_ranges = [(0.0, 5.0), (5.0, 10.0)]
    events = [{"type": "laugh", "start": 2.0, "end": 2.5}]
    unrelated = _sfx("applause1", tags=["applause"])  # matches neither "cut" nor "laugh" tags
    result = sug.suggest({"music": None}, [unrelated], events, keep_ranges, duration=10.0)
    assert [a for a in result if a.kind == "sfx"] == []


def test_sfx_respects_minimum_spacing():
    keep_ranges = [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]  # cuts at 1.0 and 2.0 -- only 1.0s apart
    whoosh = _sfx("whoosh1", tags=["whoosh"])
    result = sug.suggest({"music": None}, [whoosh], events=[], keep_ranges=keep_ranges, duration=3.0)
    sfx_items = [a for a in result if a.kind == "sfx"]
    assert len(sfx_items) == 1  # the second cut is within SFX_MIN_SPACING_SEC of the first


def test_sfx_capped_at_max_per_clip():
    # 10 cut boundaries, 2.0s apart -- well clear of the spacing floor
    keep_ranges = [(float(i), float(i + 2)) for i in range(0, 20, 2)]
    whoosh = _sfx("whoosh1", tags=["whoosh"])
    result = sug.suggest({"music": None}, [whoosh], events=[], keep_ranges=keep_ranges, duration=20.0)
    sfx_items = [a for a in result if a.kind == "sfx"]
    assert len(sfx_items) == sug.SFX_MAX_PER_CLIP


def test_best_sfx_prefers_most_tag_overlap_then_shortest():
    events = [{"type": "laugh", "start": 3.0, "end": 3.2}]
    weak = _sfx("weak", tags=["laugh"], duration=2.0)
    strong = _sfx("strong", tags=["laugh", "rimshot", "comedy"], duration=1.5)
    result = sug.suggest({"music": None}, [weak, strong], events, keep_ranges=[(0.0, 5.0)], duration=5.0)
    sfx_items = [a for a in result if a.kind == "sfx"]
    assert len(sfx_items) == 1
    assert sfx_items[0].library_id == "strong"


# --------------------------------------------------------------------------
# Tag vocabulary
# --------------------------------------------------------------------------


def test_suggest_tags_is_used_for_both_categories():
    assert set(sug.SUGGEST_TAGS.keys()) == {"cut", "laugh"}
    assert all(isinstance(v, list) and v for v in sug.SUGGEST_TAGS.values())
