"""Audio library tests — local import (real ffmpeg-synthesized files, real
librosa BPM detection) and online sources (Freesound/Jamendo response
parsing, licence filtering, download-into-library), never touching the
network: every HTTP call is mocked."""

from __future__ import annotations

import subprocess
import wave

import numpy as np
import pytest

from publikclip_pipeline.audio_library import library
from publikclip_pipeline.audio_library.sources import (
    Result,
    is_allowed,
    parse_cc_licence,
)
from publikclip_pipeline.audio_library.sources import freesound, jamendo
from publikclip_pipeline.render import ffmpeg_bin


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    yield


def _ffmpeg_silent_wav(path, duration: float) -> None:
    """A real ffmpeg-synthesized silent wav, like the render smoke test's
    synthetic sources."""
    subprocess.run(
        [
            ffmpeg_bin.ffmpeg(), "-v", "error", "-y",
            "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={duration}",
            str(path),
        ],
        check=True, timeout=60,
    )


def _click_track_wav(path, bpm: float = 120.0, duration: float = 12.0) -> None:
    """A real audio file with sharp periodic clicks — enough of a genuine
    onset signal for librosa.beat.beat_track to find a tempo (a smooth
    tone or fades produce no useful transient and always yield None)."""
    sr = 22050
    y = np.zeros(int(sr * duration), dtype=np.float32)
    interval = int(sr * 60 / bpm)
    for i in range(0, len(y) - 200, interval):
        y[i:i + 200] = 0.9
    pcm = (y * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


# --------------------------------------------------------------------------
# Local import
# --------------------------------------------------------------------------


def test_local_import_tags_kind_and_bpm(tmp_path):
    src_dir = tmp_path / "Downloads" / "Whoosh Pack 01"
    src_dir.mkdir(parents=True)
    sfx_path = src_dir / "whoosh_02.wav"
    _ffmpeg_silent_wav(sfx_path, duration=2.0)  # < AUTO_SFX_MAX_SEC -> sfx

    music_dir = tmp_path / "Music" / "Lofi Beats"
    music_dir.mkdir(parents=True)
    music_path = music_dir / "chill_study_120bpm.wav"
    _click_track_wav(music_path, bpm=120.0, duration=12.0)  # >= AUTO_SFX_MAX_SEC -> music

    items = library.import_paths([str(src_dir), str(music_path)], kind="auto")
    assert len(items) == 2

    sfx_item = next(i for i in items if i.kind == "sfx")
    music_item = next(i for i in items if i.kind == "music")

    # tags: filename + parent folder, lowercased, no numbers/punctuation
    assert "whoosh" in sfx_item.tags
    assert "pack" in sfx_item.tags
    assert "02" not in sfx_item.tags and "01" not in sfx_item.tags
    assert all(t == t.lower() for t in sfx_item.tags)

    assert "chill" in music_item.tags
    assert "study" in music_item.tags
    assert "beats" in music_item.tags
    assert "120bpm" in music_item.tags  # only PURELY numeric tokens are dropped; alnum tokens stay

    # kind auto-detection by duration
    assert sfx_item.kind == "sfx"
    assert sfx_item.bpm is None  # bpm is never computed for sfx
    assert music_item.kind == "music"
    assert music_item.bpm is not None
    assert 100.0 <= music_item.bpm <= 140.0  # near the real 120 bpm signal

    # copied into the library, not left pointing at the source
    from pathlib import Path
    assert Path(sfx_item.path).exists()
    assert Path(sfx_item.path) != sfx_path
    assert str(library.root_dir()) in sfx_item.path

    assert sfx_item.source == "local"
    assert sfx_item.licence is None
    assert sfx_item.attribution is None


def test_local_import_silent_music_gets_null_bpm(tmp_path):
    p = tmp_path / "silence_10s.wav"
    _ffmpeg_silent_wav(p, duration=10.0)
    [item] = library.import_paths([str(p)], kind="music")
    assert item.kind == "music"
    assert item.bpm is None  # silence: beat_track's tempo 0.0 -> treated as failure


def test_import_kind_forced_overrides_duration_heuristic(tmp_path):
    p = tmp_path / "short.wav"
    _ffmpeg_silent_wav(p, duration=2.0)
    [item] = library.import_paths([str(p)], kind="music")
    assert item.kind == "music"  # forced, even though 2s would auto-classify as sfx


def test_list_query_filtering_and_remove(tmp_path):
    a = tmp_path / "laugh_track.wav"
    b = tmp_path / "applause_crowd.wav"
    _ffmpeg_silent_wav(a, duration=1.5)
    _ffmpeg_silent_wav(b, duration=1.5)
    items = library.import_paths([str(a), str(b)], kind="sfx")
    laugh, applause = (items if "laugh" in items[0].tags else items[::-1])

    all_items = library.list_items()
    assert {i.id for i in all_items} == {laugh.id, applause.id}

    sfx_only = library.list_items(kind="sfx")
    assert len(sfx_only) == 2
    music_only = library.list_items(kind="music")
    assert music_only == []

    by_tag = library.list_items(query="laugh")
    assert [i.id for i in by_tag] == [laugh.id]

    assert library.remove_item(laugh.id) is True
    assert library.remove_item(laugh.id) is False  # already gone
    remaining = library.list_items()
    assert [i.id for i in remaining] == [applause.id]
    from pathlib import Path
    assert not Path(laugh.path).exists()  # file removed, not just the index entry


def test_update_tags(tmp_path):
    p = tmp_path / "ding.wav"
    _ffmpeg_silent_wav(p, duration=1.0)
    [item] = library.import_paths([str(p)], kind="sfx")
    updated = library.update_tags(item.id, ["ding", "notification", "pop"])
    assert updated.tags == ["ding", "notification", "pop"]
    assert library.update_tags("nonexistent-id", ["x"]) is None


# --------------------------------------------------------------------------
# Licence parsing (shared by both sources)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Creative Commons 0", "CC0-1.0"),
        ("http://creativecommons.org/publicdomain/zero/1.0/", "CC0-1.0"),
        ("https://creativecommons.org/licenses/by/4.0/", "CC-BY-4.0"),
        ("Attribution", "CC-BY-4.0"),
        ("https://creativecommons.org/licenses/by-nc/4.0/", None),
        ("https://creativecommons.org/licenses/by-nd/4.0/", None),
        ("https://creativecommons.org/licenses/by-nc-nd/4.0/", None),
        ("", None),
    ],
)
def test_parse_cc_licence(value, expected):
    assert parse_cc_licence(value) == expected


def test_is_allowed_gates_cc_by_on_the_flag():
    assert is_allowed("CC0-1.0", allow_attribution=False) is True
    assert is_allowed("CC0-1.0", allow_attribution=True) is True
    assert is_allowed("CC-BY-4.0", allow_attribution=False) is False
    assert is_allowed("CC-BY-4.0", allow_attribution=True) is True
    assert is_allowed(None, allow_attribution=True) is False


# --------------------------------------------------------------------------
# Freesound
# --------------------------------------------------------------------------


FREESOUND_FIXTURE = {
    "results": [
        {
            "id": 12345,
            "name": "Whoosh Transition",
            "duration": 1.8,
            "tags": ["whoosh", "transition", "swish"],
            "license": "http://creativecommons.org/publicdomain/zero/1.0/",
            "previews": {
                "preview-hq-mp3": "https://freesound.org/data/previews/123/12345_hq.mp3",
                "preview-lq-mp3": "https://freesound.org/data/previews/123/12345_lq.mp3",
            },
            "username": "soundguy",
        },
        {
            "id": 22222,
            "name": "Gentle Ding",
            "duration": 2.0,
            "tags": ["ding", "notification"],
            "license": "https://creativecommons.org/licenses/by/4.0/",
            "previews": {"preview-hq-mp3": "https://freesound.org/data/previews/222/22222_hq.mp3"},
            "username": "otherguy",
        },
        {
            "id": 33333,
            "name": "NC Sound (must never appear)",
            "duration": 3.0,
            "tags": ["nope"],
            "license": "https://creativecommons.org/licenses/by-nc/4.0/",
            "previews": {"preview-hq-mp3": "https://freesound.org/data/previews/333/33333_hq.mp3"},
            "username": "ncguy",
        },
    ]
}


class _FakeResponse:
    def __init__(self, json_data=None, content=b"", status_code=200):
        self._json_data = json_data
        self.content = content
        self.status_code = status_code

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _write_secrets(monkeypatch, **keys):
    import json

    from publikclip_pipeline import config

    config.ensure_home()
    (config.home_dir() / "secrets.json").write_text(json.dumps(keys))


def test_freesound_requires_key(monkeypatch):
    with pytest.raises(freesound.MissingKeyError, match="freesound.org/apiv2/apply"):
        freesound.search("whoosh")


def test_freesound_search_parses_and_filters_licence(monkeypatch):
    _write_secrets(monkeypatch, freesound_key="fake-key")
    monkeypatch.setattr(
        freesound.httpx, "get",
        lambda *a, **kw: _FakeResponse(json_data=FREESOUND_FIXTURE),
    )

    cc0_only = freesound.search("whoosh", kind="sfx", allow_attribution=False)
    assert [r.source_id for r in cc0_only] == ["12345"]
    assert cc0_only[0].licence == "CC0-1.0"
    assert cc0_only[0].attribution == "Whoosh Transition by soundguy on Freesound (CC0-1.0)"

    with_attribution = freesound.search("ding", kind="sfx", allow_attribution=True)
    ids = {r.source_id for r in with_attribution}
    assert ids == {"12345", "22222"}
    assert "33333" not in ids  # NC never appears regardless of the flag


def test_freesound_download_writes_item_into_library(monkeypatch):
    _write_secrets(monkeypatch, freesound_key="fake-key")
    result = Result(
        source="freesound", source_id="12345", name="Whoosh Transition",
        duration=1.8, tags=["whoosh"], kind="sfx", licence="CC0-1.0",
        attribution="Whoosh Transition by soundguy on Freesound (CC0-1.0)",
        download_url="https://freesound.org/data/previews/123/12345_hq.mp3",
        page_url="https://freesound.org/people/soundguy/sounds/12345/",
    )
    monkeypatch.setattr(
        freesound.httpx, "get",
        lambda url, **kw: _FakeResponse(content=b"fake mp3 bytes"),
    )

    item = freesound.download(result)
    from pathlib import Path

    assert Path(item.path).exists()
    assert Path(item.path).read_bytes() == b"fake mp3 bytes"
    assert item.source == "freesound"
    assert item.source_id == "12345"
    assert item.source_url == result.page_url
    assert item.licence == "CC0-1.0"
    assert item.attribution == result.attribution
    assert item.kind == "sfx"
    assert item.bpm is None  # sfx never gets bpm-analyzed
    assert item.id in library.load_index()


def test_freesound_get_by_id_classifies_kind_by_duration(monkeypatch):
    _write_secrets(monkeypatch, freesound_key="fake-key")
    short_sound = dict(FREESOUND_FIXTURE["results"][0])  # 1.8s
    monkeypatch.setattr(freesound.httpx, "get", lambda *a, **kw: _FakeResponse(json_data=short_sound))
    r = freesound.get("12345")
    assert r.kind == "sfx"

    long_sound = dict(FREESOUND_FIXTURE["results"][0])
    long_sound["duration"] = 180.0
    monkeypatch.setattr(freesound.httpx, "get", lambda *a, **kw: _FakeResponse(json_data=long_sound))
    r2 = freesound.get("12345")
    assert r2.kind == "music"


def test_freesound_get_rejects_nc(monkeypatch):
    _write_secrets(monkeypatch, freesound_key="fake-key")
    monkeypatch.setattr(
        freesound.httpx, "get",
        lambda *a, **kw: _FakeResponse(json_data=FREESOUND_FIXTURE["results"][2]),
    )
    assert freesound.get("33333") is None


def test_freesound_get_404(monkeypatch):
    _write_secrets(monkeypatch, freesound_key="fake-key")
    monkeypatch.setattr(freesound.httpx, "get", lambda *a, **kw: _FakeResponse(status_code=404))
    assert freesound.get("00000") is None


# --------------------------------------------------------------------------
# Jamendo
# --------------------------------------------------------------------------


JAMENDO_FIXTURE = {
    "results": [
        {
            "id": "999",
            "name": "Lofi Dreams",
            "duration": 180,
            "artist_name": "SomeArtist",
            "license_ccurl": "https://creativecommons.org/publicdomain/zero/1.0/",
            "audiodownload": "https://prod-1.storage.jamendo.com/download/track/999/mp32/",
            "audiodownload_allowed": True,
            "shareurl": "https://www.jamendo.com/track/999/lofi-dreams",
            "musicinfo": {"tags": {"genres": ["lofi", "hiphop"], "instruments": [], "vartags": ["chill"]}},
        },
        {
            "id": "888",
            "name": "Sunny Pop",
            "duration": 200,
            "artist_name": "Artist2",
            "license_ccurl": "https://creativecommons.org/licenses/by/3.0/",
            "audiodownload": "https://prod-1.storage.jamendo.com/download/track/888/mp32/",
            "audiodownload_allowed": True,
            "shareurl": "https://www.jamendo.com/track/888/sunny-pop",
            "musicinfo": {"tags": {"genres": ["pop"], "instruments": [], "vartags": []}},
        },
        {
            "id": "777",
            "name": "NC Track (must never appear)",
            "duration": 210,
            "artist_name": "Artist3",
            "license_ccurl": "https://creativecommons.org/licenses/by-nc-nd/4.0/",
            "audiodownload": "https://prod-1.storage.jamendo.com/download/track/777/mp32/",
            "audiodownload_allowed": True,
            "shareurl": "https://www.jamendo.com/track/777/nc-track",
            "musicinfo": {"tags": {"genres": [], "instruments": [], "vartags": []}},
        },
    ]
}


def test_jamendo_requires_key(monkeypatch):
    with pytest.raises(jamendo.MissingKeyError, match="devportal.jamendo.com"):
        jamendo.search("lofi")


def test_jamendo_search_parses_tags_and_filters_licence(monkeypatch):
    _write_secrets(monkeypatch, jamendo_client_id="fake-id")
    monkeypatch.setattr(jamendo.httpx, "get", lambda *a, **kw: _FakeResponse(json_data=JAMENDO_FIXTURE))

    cc0_only = jamendo.search("lofi", allow_attribution=False)
    assert [r.source_id for r in cc0_only] == ["999"]
    assert cc0_only[0].tags == ["lofi", "hiphop", "chill"]
    assert cc0_only[0].kind == "music"
    assert cc0_only[0].attribution == "Lofi Dreams by SomeArtist on Jamendo (CC0-1.0)"

    with_attribution = jamendo.search("pop", allow_attribution=True)
    ids = {r.source_id for r in with_attribution}
    assert ids == {"999", "888"}
    assert "777" not in ids


def test_jamendo_search_kind_sfx_short_circuits_without_http(monkeypatch):
    _write_secrets(monkeypatch, jamendo_client_id="fake-id")

    def boom(*a, **kw):
        raise AssertionError("jamendo has no sfx content — must not call HTTP")

    monkeypatch.setattr(jamendo.httpx, "get", boom)
    assert jamendo.search("whoosh", kind="sfx") == []


def test_jamendo_max_duration_filters_client_side(monkeypatch):
    _write_secrets(monkeypatch, jamendo_client_id="fake-id")
    monkeypatch.setattr(jamendo.httpx, "get", lambda *a, **kw: _FakeResponse(json_data=JAMENDO_FIXTURE))
    results = jamendo.search("lofi", max_duration=190.0, allow_attribution=True)
    ids = {r.source_id for r in results}
    assert "888" not in ids  # duration 200 > 190
    assert "999" in ids  # duration 180 <= 190


def test_jamendo_download_writes_item_and_attribution(monkeypatch):
    _write_secrets(monkeypatch, jamendo_client_id="fake-id")
    result = Result(
        source="jamendo", source_id="999", name="Lofi Dreams", duration=180.0,
        tags=["lofi", "hiphop", "chill"], kind="music", licence="CC0-1.0",
        attribution="Lofi Dreams by SomeArtist on Jamendo (CC0-1.0)",
        download_url="https://prod-1.storage.jamendo.com/download/track/999/mp32/",
        page_url="https://www.jamendo.com/track/999/lofi-dreams",
    )
    monkeypatch.setattr(jamendo.httpx, "get", lambda url, **kw: _FakeResponse(content=b"fake mp3 bytes"))

    item = jamendo.download(result)
    from pathlib import Path

    assert Path(item.path).exists()
    assert Path(item.path).read_bytes() == b"fake mp3 bytes"
    assert item.kind == "music"
    assert item.source == "jamendo"
    assert item.licence == "CC0-1.0"
    assert item.attribution == result.attribution
    assert item.id in library.load_index()
    # garbage bytes -> bpm detection fails gracefully, never raises
    assert item.bpm is None
