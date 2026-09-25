"""Audio-track mixing filter graph + credits.txt — unit-level tests on the
graph-building helpers (render_clip_edit itself needs a full job_dir with
every stage checkpoint; these pieces are what's actually new here)."""

from __future__ import annotations

from publikclip_pipeline.audio_library.library import Item as LibItem
from publikclip_pipeline.edits import render_clip as rc
from publikclip_pipeline.edits.timeline import AudioItem


def _lib_item(item_id, duration, licence=None, attribution=None):
    return LibItem(
        id=item_id, path=f"/lib/{item_id}.mp3", kind="music", name=item_id,
        duration=duration, bpm=None, licence=licence, attribution=attribution,
    )


def test_empty_audio_yields_unchanged_speech_label():
    """No audio track -> the mixing step is a no-op: no extra inputs, no
    extra filter chains, and loudnorm reads straight from [ac] — exactly
    the graph render_clip_edit built before this phase."""
    inputs, chains, label = rc._audio_mix_filters([], "medium", input_offset=1, lib_index={})
    assert inputs == []
    assert chains == []
    assert label == "ac"


def test_music_and_sfx_mix_pieces():
    music = AudioItem(
        id="a1", library_id="lib-music", path="/lib/music/song.mp3", kind="music",
        start=2.0, duration=10.0, gain_db=-14.0, fade_in=0.5, fade_out=1.5,
        loop=True, duck=True,
    )
    sfx = AudioItem(
        id="a2", library_id="lib-sfx", path="/lib/sfx/whoosh.wav", kind="sfx",
        start=5.0, duration=1.0, gain_db=0.0, loop=False, duck=False,
    )
    lib_index = {
        "lib-music": _lib_item("lib-music", duration=6.0),  # shorter than the 10s placement -> loops
        "lib-sfx": _lib_item("lib-sfx", duration=1.0),
    }

    inputs, chains, label = rc._audio_mix_filters(
        [music, sfx], duck_intensity="heavy", input_offset=2, lib_index=lib_index
    )
    graph = ";".join(chains)

    assert inputs == ["-i", music.path, "-i", sfx.path]
    assert label == "ac_mixed"

    # speech normalized once, up front, then split — one copy for amix, one
    # per ducked item's sidechaincompress key (sharing a single [ac_fmt]
    # label across both consumers made ffmpeg's format auto-negotiation
    # silently revert it to the source's native rate/mono; asplit avoids that)
    assert "[ac]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[ac_fmt][ac_sc0]" in graph
    assert "[aitem0][ac_sc0]sidechaincompress" in graph  # music's own dedicated sidechain copy
    assert "[ac_fmt][aitem0d][aitem1]amix" in graph  # amix uses the OTHER copy

    # music: loops (source 6s < placement 10s), fades, delayed to its 2.0s start
    assert "aloop=loop=-1:size=2000000000" in chains[1]
    assert "atrim=duration=10.000" in chains[1]
    assert "volume=-14.00dB" in chains[1]
    assert "afade=t=in:st=0:d=0.500" in chains[1]
    assert "afade=t=out:st=8.500:d=1.500" in chains[1]  # 10.0 - 1.5
    assert "adelay=delays=2000:all=1" in chains[1]

    # sfx: no loop (source == placement duration), no fades set, delayed to 5.0s
    # (chains[2] is the music item's sidechaincompress stage; sfx's own chain is chains[3])
    assert "aloop" not in chains[3]
    assert "afade" not in chains[3]
    assert "adelay=delays=5000:all=1" in chains[3]

    # only the music item ducks (duck=True); heavy preset values; sfx never sidechained
    assert graph.count("sidechaincompress") == 1
    assert "sidechaincompress=threshold=0.1:ratio=8.0" in graph

    # amix combines speech + both items (ducked music label + plain sfx label)
    assert "amix=inputs=3:duration=first:normalize=0[ac_mixed]" in graph


def test_no_duck_item_has_no_sidechaincompress():
    sfx = AudioItem(id="a", library_id="lib-sfx", path="/x.wav", kind="sfx", start=0.0, duration=1.0)
    _, chains, _ = rc._audio_mix_filters([sfx], "medium", input_offset=1, lib_index={})
    assert "sidechaincompress" not in ";".join(chains)


def test_loop_skipped_when_source_already_long_enough():
    music = AudioItem(id="a", library_id="lib-music", path="/x.mp3", kind="music", start=0.0, duration=5.0)
    lib_index = {"lib-music": _lib_item("lib-music", duration=30.0)}  # much longer than needed
    _, chains, _ = rc._audio_mix_filters([music], "medium", input_offset=1, lib_index=lib_index)
    assert "aloop" not in chains[1]


def test_loop_applied_when_library_entry_missing():
    """An item whose library entry vanished (removed after being placed)
    still loops if it asked to — safer than risking silence."""
    music = AudioItem(id="a", library_id="gone", path="/x.mp3", kind="music", start=0.0, duration=5.0)
    _, chains, _ = rc._audio_mix_filters([music], "medium", input_offset=1, lib_index={})
    assert "aloop" in chains[1]


def test_two_ducked_items_each_get_their_own_sidechain_split():
    """Each ducked item must consume a DIFFERENT copy of the split speech
    stream, never the same [ac_fmt] label twice (that's exactly the ffmpeg
    negotiation bug asplit exists to avoid)."""
    m1 = AudioItem(id="a", library_id="l1", path="/1.mp3", kind="music", start=0.0, duration=4.0)
    m2 = AudioItem(id="b", library_id="l2", path="/2.mp3", kind="music", start=0.0, duration=4.0)
    _, chains, label = rc._audio_mix_filters([m1, m2], "light", input_offset=1, lib_index={})
    graph = ";".join(chains)

    assert "asplit=3[ac_fmt][ac_sc0][ac_sc1]" in graph
    assert "[aitem0][ac_sc0]sidechaincompress" in graph
    assert "[aitem1][ac_sc1]sidechaincompress" in graph
    assert graph.count("sidechaincompress") == 2
    assert "amix=inputs=3:duration=first:normalize=0[ac_mixed]" in graph
    assert label == "ac_mixed"


def test_write_credits_only_for_cc_by(tmp_path):
    lib_index = {
        "local-item": _lib_item("local-item", duration=5.0, licence=None, attribution=None),
        "cc0-item": _lib_item("cc0-item", duration=5.0, licence="CC0-1.0", attribution="CC0 credit"),
        "ccby-item": _lib_item(
            "ccby-item", duration=5.0, licence="CC-BY-4.0",
            attribution="Song Name by Artist on Freesound (CC-BY-4.0)",
        ),
    }
    items = [
        AudioItem(id="1", library_id="local-item", path="/x", kind="music", start=0.0, duration=5.0),
        AudioItem(id="2", library_id="cc0-item", path="/y", kind="music", start=0.0, duration=5.0),
        AudioItem(id="3", library_id="ccby-item", path="/z", kind="music", start=0.0, duration=5.0),
    ]

    rc._write_credits(tmp_path, 0, items, lib_index)
    credits_path = tmp_path / "clip_00.credits.txt"
    assert credits_path.exists()
    text = credits_path.read_text()
    assert "Song Name by Artist on Freesound (CC-BY-4.0)" in text
    assert "CC0 credit" not in text  # CC0 needs no credit


def test_write_credits_removes_stale_file_when_no_cc_by_used(tmp_path):
    credits_path = tmp_path / "clip_00.credits.txt"
    credits_path.write_text("stale credit from a prior render\n")

    lib_index = {"cc0-item": _lib_item("cc0-item", duration=5.0, licence="CC0-1.0", attribution="x")}
    items = [AudioItem(id="1", library_id="cc0-item", path="/x", kind="music", start=0.0, duration=5.0)]

    rc._write_credits(tmp_path, 0, items, lib_index)
    assert not credits_path.exists()


def test_write_credits_no_audio_no_file(tmp_path):
    rc._write_credits(tmp_path, 0, [], {})
    assert not (tmp_path / "clip_00.credits.txt").exists()
