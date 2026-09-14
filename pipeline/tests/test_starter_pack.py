"""Starter pack tests: the cross-check with audio_suggest.SUGGEST_TAGS (so
the two can't silently drift apart), plus bootstrap()'s own logic — rating
sort, top-N cap, skip-already-present, tag merging — with mocked HTTP,
never touching the network."""

from __future__ import annotations

import json
import re

import pytest

from publikclip_pipeline import config
from publikclip_pipeline.audio_library import library, starter_pack
from publikclip_pipeline.audio_library.sources import freesound
from publikclip_pipeline.audio_library.starter_pack import STARTER_PACK
from publikclip_pipeline.edits.audio_suggest import SUGGEST_TAGS

_TAG_SPLIT = re.compile(r"[^a-z0-9]+")


def _query_words(query: str) -> set[str]:
    return {t for t in _TAG_SPLIT.split(query.lower()) if t}


def test_every_suggest_tag_appears_in_a_starter_pack_query():
    all_query_words: set[str] = set()
    for query, _kind, _count in STARTER_PACK:
        all_query_words |= _query_words(query)

    all_tags = {tag for tags in SUGGEST_TAGS.values() for tag in tags}
    missing = all_tags - all_query_words
    assert not missing, f"SUGGEST_TAGS words with no covering starter-pack query: {missing}"


def test_starter_pack_shape():
    assert STARTER_PACK, "starter pack must not be empty"
    for query, kind, count in STARTER_PACK:
        assert isinstance(query, str) and query.strip()
        assert kind in ("music", "sfx")
        assert isinstance(count, int) and count > 0


def test_starter_pack_has_both_kinds():
    kinds = {kind for _q, kind, _c in STARTER_PACK}
    assert kinds == {"music", "sfx"}


# --------------------------------------------------------------------------
# bootstrap() itself, mocked HTTP
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    yield


def _write_secrets(**keys):
    config.ensure_home()
    (config.home_dir() / "secrets.json").write_text(json.dumps(keys))


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


_FREESOUND_SOUNDS = [
    {
        "id": 1, "name": "Sound A", "duration": 2.0, "tags": ["a"],
        "license": "http://creativecommons.org/publicdomain/zero/1.0/",
        "previews": {"preview-hq-mp3": "https://freesound-preview.example/1.mp3"},
        "username": "u1", "avg_rating": 4.5, "num_downloads": 100,
    },
    {
        "id": 2, "name": "Sound B", "duration": 2.0, "tags": ["b"],
        "license": "http://creativecommons.org/publicdomain/zero/1.0/",
        "previews": {"preview-hq-mp3": "https://freesound-preview.example/2.mp3"},
        "username": "u2", "avg_rating": 2.0, "num_downloads": 10,
    },
]


def _fake_get(url, **kwargs):
    """freesound.httpx and jamendo.httpx are the SAME module object (both
    just `import httpx`) — a test exercising both sources in one go needs
    ONE dispatcher covering both URL shapes, not two separate fakes that
    would silently clobber each other via the shared `.get` attribute."""
    if "freesound" in url:
        if "/search/text/" in url:
            return _FakeResponse(json_data={"results": _FREESOUND_SOUNDS})
        return _FakeResponse(content=b"fake mp3 bytes")  # preview download
    if "jamendo" in url:
        params = kwargs.get("params") or {}
        if "id" not in params:
            return _FakeResponse(json_data={"results": [
                {
                    "id": "j1", "name": "Track J", "duration": 180.0, "artist_name": "Artist",
                    "license_ccurl": "https://creativecommons.org/publicdomain/zero/1.0/",
                    "audiodownload": "https://jamendo-download.example/j1.mp3", "audiodownload_allowed": True,
                    "shareurl": "https://jamendo.com/track/j1",
                    "musicinfo": {"tags": {"genres": [], "instruments": [], "vartags": []}},
                },
            ]})
        return _FakeResponse(content=b"fake mp3 bytes")  # download
    raise AssertionError(f"unexpected URL in test: {url}")


def test_bootstrap_top_n_by_rating_and_tags_merged(monkeypatch):
    _write_secrets(freesound_key="fake", jamendo_client_id="fake")
    monkeypatch.setattr(freesound.httpx, "get", _fake_get)
    monkeypatch.setattr(starter_pack, "STARTER_PACK", [("whoosh test", "sfx", 1)])

    added = starter_pack.bootstrap()
    assert len(added) == 1
    assert added[0].source_id == "1"  # higher rating (4.5) beats "2" (2.0)
    assert "whoosh" in added[0].tags and "test" in added[0].tags  # query words merged in
    assert "a" in added[0].tags  # source's own tag preserved too


def test_bootstrap_skips_items_already_in_library(monkeypatch):
    _write_secrets(freesound_key="fake")
    monkeypatch.setattr(freesound.httpx, "get", _fake_get)
    monkeypatch.setattr(starter_pack, "STARTER_PACK", [("whoosh", "sfx", 2)])

    library.add_item(library.Item(
        id="pre-existing", path="/x", kind="sfx", name="x", duration=1.0, bpm=None,
        source="freesound", source_id="1",
    ))
    added = starter_pack.bootstrap()
    assert len(added) == 1
    assert added[0].source_id == "2"  # "1" already present, skipped


def test_bootstrap_music_tries_jamendo_then_freesound(monkeypatch):
    _write_secrets(freesound_key="fake", jamendo_client_id="fake")
    monkeypatch.setattr(freesound.httpx, "get", _fake_get)
    monkeypatch.setattr(starter_pack, "STARTER_PACK", [("lofi", "music", 3)])

    added = starter_pack.bootstrap()
    sources = {a.source for a in added}
    assert "jamendo" in sources
    assert "freesound" in sources


def test_bootstrap_missing_key_skips_that_source_without_crashing(monkeypatch):
    # no secrets.json at all -> both sources raise MissingKeyError internally
    monkeypatch.setattr(starter_pack, "STARTER_PACK", [("whoosh", "sfx", 2), ("lofi", "music", 2)])
    added = starter_pack.bootstrap()
    assert added == []


def test_bootstrap_reports_progress():
    _write_secrets()
    calls = []
    import unittest.mock as mock

    with mock.patch.object(starter_pack, "STARTER_PACK", [("whoosh", "sfx", 1)]), \
         mock.patch.object(starter_pack, "_search_for", return_value=[]):
        starter_pack.bootstrap(lambda f, m: calls.append((f, m)))
    assert calls  # at least the start + final "done" report
    assert calls[-1][0] == 1.0
