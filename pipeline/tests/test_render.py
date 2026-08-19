"""Renderer + caption engine tests. The render smoke test builds a real
20 s synthetic clip through the FULL ffmpeg path — sendcmd crop, caption
burn, loudnorm — and verifies the output probes clean."""

import json
import subprocess
from pathlib import Path

import pytest

from publikclip_pipeline.captions import ass as ass_mod
from publikclip_pipeline.render import ffmpeg_bin, renderer


def test_crop_boxes_even_and_bounded():
    frames = [[10.7, 5.3, 607.9, 1080.0], [1900.0, 0.0, 608.0, 1080.0]]
    boxes = renderer.crop_boxes(frames, 1920, 1080)
    for w, h, x, y in boxes:
        assert w % 2 == 0 and h % 2 == 0 and x % 2 == 0 and y % 2 == 0
        assert x + w <= 1920 and y + h <= 1080


def test_sendcmd_dedupes_to_change_points():
    boxes = [(608, 1080, 100, 0)] * 50 + [(608, 1080, 700, 0)] * 50
    lines = renderer.sendcmd_lines(boxes, 25.0)
    # initial 4 params + 1 change (x only)
    assert len(lines) == 5
    assert lines[-1].startswith("2.0000 crop@c x 700")


def test_chunking_rules():
    words = [ass_mod.Word(f"w{i}", i * 0.3, i * 0.3 + 0.25) for i in range(6)]
    chunks = ass_mod.chunk_words(words)
    assert [len(c.words) for c in chunks] == [4, 2]  # budget break

    words = [
        ass_mod.Word("hey.", 0.0, 0.3),
        ass_mod.Word("so", 0.4, 0.6),
        ass_mod.Word("anyway", 2.0, 2.4),  # >0.6s pause before this
    ]
    chunks = ass_mod.chunk_words(words)
    assert len(chunks) == 3  # punctuation break + pause break


def test_emphasis_or_combination():
    words = [
        ass_mod.Word("million", 0.0, 0.5),   # power word
        ass_mod.Word("okay", 0.5, 1.0),      # quiet filler
        ass_mod.Word("LOUD", 1.0, 1.5),      # top-RMS word
    ]
    rms = [0.1] * 10 + [0.9] * 5  # 0.1s grid; frames 10-14 are loud
    ass_mod.mark_emphasis(words, rms, 0.1, clip_start=0.0)
    assert words[0].emphasized      # power word
    assert not words[1].emphasized
    assert words[2].emphasized      # prosodic


def test_ass_document_structure():
    words = [ass_mod.Word("hello", 0.0, 0.4), ass_mod.Word("world", 0.4, 0.8)]
    events = [{"type": "laugh", "start": 1.0, "end": 2.0}]
    doc = ass_mod.build_ass(words, events, preset_name="beast")
    assert "[Script Info]" in doc and "[Events]" in doc
    # one Dialogue per word transition + one event tag
    assert doc.count("Dialogue: 0,") == 2
    assert doc.count("Dialogue: 1,") == 1
    assert "[laughs]" in doc
    assert "\\k" not in doc  # never native karaoke tags
    assert "HELLO" in doc    # beast preset uppercases


def test_ass_no_word_scaling():
    """Words must never be individually scaled — only the chunk-entrance pop
    on the first event of a chunk."""
    words = [ass_mod.Word("one", 0.0, 0.4), ass_mod.Word("two", 0.4, 0.8)]
    doc = ass_mod.build_ass(words, [], preset_name="beast")
    lines = [l for l in doc.splitlines() if l.startswith("Dialogue: 0")]
    assert "\\fscx" in lines[0]      # entrance pop on chunk start
    assert "\\fscx" not in lines[1]  # no per-word scaling afterwards


@pytest.mark.slow
def test_crop_expr_constant_window_recenters_on_focal_center():
    """The zoom-free fallback must keep w/h constant (ffmpeg crop cannot
    resize mid-stream) and recenter the window on each box's focal center."""
    n = 10
    # Window = 800x800 inside a 1600x1600 source; then a 2x punch-in at
    # focal center (500, 280) — the window must recenter to (100, 0).
    boxes = [(800, 800, 0, 0)] * 5 + [(400, 400, 300, 80)] * 5
    expr = renderer.crop_expr(boxes, 25.0, 1600, 1600)
    assert expr.startswith("crop=w=800:h=800:")
    assert "if(lt(t,0.2000),100" in expr


def test_chunk_boxes_stays_under_depth_budget():
    """Smooth pans burn one change point per frame; chunks must cap below
    the ffmpeg expression-depth limit (~150-200)."""
    fps = 30.0
    n = 1000  # 33s of per-frame pan
    boxes = [(404, 720, 100 + i % 8, 0) for i in range(n)]
    chunks = renderer._chunk_boxes(boxes, fps)
    assert len(chunks) > 1
    for chunk in chunks:
        xs = [b[2] for b in chunk["boxes"]]
        changes = sum(1 for a, b in zip(xs, xs[1:]) if a != b)
        assert changes <= renderer.MAX_CHANGE_POINTS
    # Contiguous: chunk boundaries tile the full timeline.
    assert chunks[0]["t0"] == 0.0
    assert abs(chunks[-1]["t1"] - n / fps) < 1e-6
    for a, b in zip(chunks, chunks[1:]):
        assert abs(a["t1"] - b["t0"]) < 1e-6


def test_render_smoke(tmp_path):
    """Full path: synthetic source → sendcmd crop with a mid-clip cut →
    caption burn → verified 9:16 output."""
    src = tmp_path / "src.mp4"
    # Resolve like the product does — on a bare machine (Windows CI) the only
    # ffmpeg is the fetched static one, reachable via PUBLIKCLIP_FFMPEG.
    subprocess.run(
        [
            ffmpeg_bin.ffmpeg(), "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=20",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src),
        ],
        check=True, timeout=300,
    )
    n = 20 * 25
    crop_w = 720 * 9 / 16
    frames = [[100.0, 0.0, crop_w, 720.0]] * (n // 2) + [[700.0, 0.0, crop_w, 720.0]] * (n // 2)
    trajectory = {"fps": 25, "frames": frames, "cuts": [n // 2], "punches": []}

    words = [ass_mod.Word(f"word{i}", i * 0.5, i * 0.5 + 0.4) for i in range(30)]
    ass_path = tmp_path / "caps.ass"
    ass_path.write_text(ass_mod.build_ass(words, [{"type": "laugh", "start": 2.0, "end": 3.5}]))

    out = tmp_path / "out.mp4"
    renderer.render_clip(
        str(src), out, 0.0, 20.0, trajectory, ass_path, ass_mod.FONTS_DIR,
        src_w=1280, src_h=720,
    )
    check = renderer.verify_output(out, 20.0)
    assert check["ok"], check
    assert check["width"] == 1080 and check["height"] == 1920
