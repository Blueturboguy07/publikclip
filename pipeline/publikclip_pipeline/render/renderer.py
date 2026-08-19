"""Clip renderer: one ffmpeg filter chain per clip.

The camera trajectory (per-frame crop rects, punches included) becomes a
deduped sendcmd command file driving a labeled crop filter — hard cuts are
just discontinuities in the same array, pans are smooth regions, punch-ins
already live in the w/h values. If that path fails or hangs on a given
ffmpeg build, a zoom-free fallback (constant window, x/y expressions)
still produces the clip.

    sendcmd → crop@c → scale 1080x1920 → subtitles burn → loudnorm

Deduping to change-points matters: a 45 s clip at 25 fps is 1125 frames and
writing every parameter every frame slows the filter measurably (openshorts'
own comment). Even dimensions everywhere — x264/NVENC reject odd ones.

Encoder tiers follow openshorts ffmpeg_utils.py: try hardware
(h264_videotoolbox on macOS), fall back to libx264, mapping quality between
CRF and the hardware encoder's bitrate model.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from . import ffmpeg_bin

OUT_W = 1080
OUT_H = 1920
X264_CRF = 19
VT_BITRATE = "10M"

_vt_checked: bool | None = None


def videotoolbox_available() -> bool:
    """Probe once: encode 0.2 s of black through h264_videotoolbox."""
    global _vt_checked
    if _vt_checked is None:
        proc = subprocess.run(
            [
                ffmpeg_bin.ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "color=black:s=320x240:d=0.2",
                "-c:v", "h264_videotoolbox", "-f", "null", "-",
            ],
            capture_output=True, timeout=60,
        )
        _vt_checked = proc.returncode == 0
    return _vt_checked


def crop_boxes(frames: list[list[float]], src_w: int, src_h: int) -> list[tuple[int, int, int, int]]:
    """Director frames [x, y, w, h] → even-int (w, h, x, y) crop boxes,
    clamped in-bounds (openshorts crop_boxes rounding rules)."""
    boxes: list[tuple[int, int, int, int]] = []
    for x, y, w, h in frames:
        wi = max(2, min(int(w) - int(w) % 2, src_w))
        hi = max(2, min(int(h) - int(h) % 2, src_h))
        xi = max(0, min(int(round(x)), src_w - wi))
        yi = max(0, min(int(round(y)), src_h - hi))
        boxes.append((wi, hi, xi - xi % 2, yi - yi % 2))
    return boxes


def sendcmd_lines(boxes: list[tuple[int, int, int, int]], fps: float, target: str = "crop@c") -> list[str]:
    """Per-frame w/h/x/y commands, deduped to change-points (openshorts)."""
    lines: list[str] = []
    prev: tuple[int, int, int, int] | None = None
    for i, box in enumerate(boxes):
        if box == prev:
            continue
        t = i / fps
        w, h, x, y = box
        pw, ph, px, py = prev if prev else (None, None, None, None)
        if w != pw:
            lines.append(f"{t:.4f} {target} w {w};")
        if h != ph:
            lines.append(f"{t:.4f} {target} h {h};")
        if x != px:
            lines.append(f"{t:.4f} {target} x {x};")
        if y != py:
            lines.append(f"{t:.4f} {target} y {y};")
        prev = box
    return lines


def _q(path: str) -> str:
    """ffmpeg filter-option quoting: single quotes make the value literal;
    an embedded quote closes, escapes, reopens ('\\'').

    Windows adds two wrinkles the mac path never sees: backslash is
    ffmpeg's escape character even inside quotes (av_get_token), and the
    drive-letter colon reads as an option separator on some parse levels.
    Forward slashes (fine for libass and every filter) plus an escaped
    colon is the canonical portable form: 'C\\:/Users/…/clip.ass'."""
    text = str(path)
    if os.name == "nt":
        text = text.replace("\\", "/").replace(":", "\\:")
    return "'" + text.replace("'", "'\\''") + "'"


def _piecewise_expr(pairs: list[tuple[float, int]], default: int) -> str:
    """Nested if(lt(t,T),v,...) expression for a crop parameter.

    ``pairs`` = [(change_time, value), ...] in ascending time order.
    Built back-to-front so the final value is the fallthrough:
        if(lt(t,T1),V1,if(lt(t,T2),V2,...,Vn))
    """
    expr = str(default)
    for t, v in reversed(pairs):
        expr = f"if(lt(t,{t:.4f}),{v},{expr})"
    return expr


def crop_expr(boxes: list[tuple[int, int, int, int]], fps: float, src_w: int, src_h: int) -> str:
    """Zoom-free fallback crop: constant window, x/y piecewise expressions.

    ffmpeg's crop filter cannot change its output size mid-stream — reinit
    against a decoder input fails with "Failed to configure input pad" — so
    w/h must stay constant. We fix the window at the largest box (normal
    framing) and recenter it on each box's focal center, which keeps the
    trajectory's pans and hard cuts while giving up punch-in zoom (that's
    the sendcmd path's job — the primary renderer).
    """
    if not boxes:
        boxes = [(src_w, src_h, 0, 0)]
    w0 = max(b[0] for b in boxes)
    h0 = max(b[1] for b in boxes)
    x_pairs: list[tuple[float, int]] = []
    y_pairs: list[tuple[float, int]] = []
    prev: tuple[int, int] | None = None
    for i, (w, h, x, y) in enumerate(boxes):
        t = i / fps
        cx = x + w / 2
        cy = y + h / 2
        nx = min(max(int(round(cx - w0 / 2)), 0), src_w - w0)
        ny = min(max(int(round(cy - h0 / 2)), 0), src_h - h0)
        nx -= nx % 2
        ny -= ny % 2
        if prev is None:
            x_pairs.append((t, nx))
            y_pairs.append((t, ny))
        else:
            if nx != prev[0]:
                x_pairs.append((t, nx))
            if ny != prev[1]:
                y_pairs.append((t, ny))
        prev = (nx, ny)
    x0 = x_pairs[0][1] if x_pairs else 0
    y0 = y_pairs[0][1] if y_pairs else 0
    xe = _piecewise_expr(x_pairs[1:], x0)
    ye = _piecewise_expr(y_pairs[1:], y0)
    return f"crop=w={w0}:h={h0}:x='{xe}':y='{ye}'"


MAX_CHANGE_POINTS = 70  # ffmpeg av_expr nesting: 90 ok / 100 fails on this build


def _chunk_boxes(
    boxes: list[tuple[int, int, int, int]], fps: float
) -> list[dict]:
    """Split a box trajectory into render chunks under the expression-depth
    limit. A change point is a frame where x or y differs from the previous
    frame (w/h are constant in the zoom-free path), so smooth pans burn one
    change point per frame. Greedy: accumulate frames until either parameter
    would exceed MAX_CHANGE_POINTS, then close the chunk.
    """
    chunks: list[dict] = []
    start = 0
    x_prev = boxes[0][2]
    y_prev = boxes[0][3]
    x_changes = 0
    y_changes = 0
    for i, (w, h, x, y) in enumerate(boxes):
        if i == 0:
            continue
        if x != x_prev:
            x_changes += 1
            x_prev = x
        if y != y_prev:
            y_changes += 1
            y_prev = y
        if x_changes >= MAX_CHANGE_POINTS or y_changes >= MAX_CHANGE_POINTS:
            chunks.append({
                "t0": start / fps,
                "t1": i / fps,
                "boxes": boxes[start : i + 1],
            })
            start = i
            x_prev = boxes[i][2]
            y_prev = boxes[i][3]
            x_changes = 0
            y_changes = 0
    if start < len(boxes):
        chunks.append({
            "t0": start / fps,
            "t1": len(boxes) / fps,
            "boxes": boxes[start:],
        })
    return chunks


FONT_SUFFIXES = {".ttf", ".otf", ".ttc", ".woff", ".woff2"}


def _clean_fonts_dir(fonts_dir: Path | None, work_dir: Path) -> Path | None:
    """libass errors on non-font files inside fontsdir (observed live:
    OFL-Anton.txt → "Error opening memory font" → filter reinit failure,
    which deadlocked the old sendcmd path). Hand it a dir with only fonts.
    """
    if fonts_dir is None:
        return None
    clean = work_dir / ".fonts"
    clean.mkdir(parents=True, exist_ok=True)
    for f in sorted(fonts_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in FONT_SUFFIXES:
            shutil.copy2(f, clean / f.name)
    return clean if any(clean.iterdir()) else None


def render_clip(
    media_path: str,
    out_path: Path,
    clip_start: float,
    clip_end: float,
    trajectory: dict,
    ass_path: Path | None,
    fonts_dir: Path | None,
    lufs: float = -14.0,
    true_peak: float = -1.0,
    src_w: int = 1920,
    src_h: int = 1080,
    timeout: float = 1800.0,
) -> None:
    duration = clip_end - clip_start
    boxes = crop_boxes(trajectory["frames"], src_w, src_h)
    if not boxes:
        boxes = [(src_h * 9 // 16 // 2 * 2, src_h - src_h % 2, 0, 0)]
    fps = float(trajectory.get("fps", 25))

    if videotoolbox_available():
        vcodec = ["-c:v", "h264_videotoolbox", "-b:v", VT_BITRATE, "-allow_sw", "1"]
    else:
        vcodec = ["-c:v", "libx264", "-preset", "medium", "-crf", str(X264_CRF)]

    # Expression-depth limit: nested if(lt(t,T),...) breaks past ~150-200
    # levels on this ffmpeg build, and sendcmd deadlocks at scale — so the
    # trajectory renders in chunks that stay under the depth budget and the
    # parts are concatenated. crop's `t` rebases to ~0 after each -ss input
    # seek (verified), so per-chunk expressions are correct; setpts restores
    # absolute PTS before the subtitles filter so caption timing survives
    # the chunking.
    chunks = _chunk_boxes(boxes, fps)
    part_paths: list[Path] = []
    fonts_clean = _clean_fonts_dir(fonts_dir, out_path.parent)
    try:
        for chunk in chunks:
            part = out_path.with_name(f"{out_path.stem}.p{len(part_paths)}.mp4")
            part_paths.append(part)
            _render_chunk(
                media_path, part,
                clip_start + chunk["t0"], chunk["t1"] - chunk["t0"],
                chunk["boxes"], fps, clip_start + chunk["t0"],
                ass_path, fonts_clean, lufs, true_peak, src_w, src_h,
                timeout, vcodec,
            )
        _concat_parts(part_paths, out_path)
    finally:
        for part in part_paths:
            part.unlink(missing_ok=True)
        if fonts_clean is not None:
            shutil.rmtree(fonts_clean, ignore_errors=True)


def _render_chunk(
    media_path: str,
    out_path: Path,
    start: float,
    duration: float,
    boxes: list[tuple[int, int, int, int]],
    fps: float,
    absolute_start: float,
    ass_path: Path | None,
    fonts_dir: Path | None,
    lufs: float,
    true_peak: float,
    src_w: int,
    src_h: int,
    timeout: float,
    vcodec: list[str],
) -> None:
    vf = [
        crop_expr(boxes, fps, src_w, src_h),
        f"setpts=PTS+{absolute_start:.4f}",
        f"scale={OUT_W}:{OUT_H}:flags=lanczos",
        "setsar=1",
    ]
    if ass_path is not None:
        sub = f"subtitles=filename={_q(ass_path)}"
        if fonts_dir is not None:
            sub += f":fontsdir={_q(fonts_dir)}"
        vf.append(sub)

    args = [
        ffmpeg_bin.ffmpeg(), "-y", "-v", "error",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
        "-i", media_path,
        "-vf", ",".join(vf),
        "-af", f"loudnorm=I={lufs}:TP={true_peak}:LRA=11",
        *vcodec,
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-map_metadata", "-1",  # metadata scrub (openshorts ffmpeg_utils)
        str(out_path),
    ]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # No-progress watchdog: a deadlocked filter graph leaves the output file
    # frozen while the process lives. Kill it so the caller can move on.
    last_size = -1
    stale_for = 0.0
    while proc.poll() is None:
        size = out_path.stat().st_size if out_path.exists() else 0
        if size != last_size:
            last_size = size
            stale_for = 0.0
        else:
            stale_for += 10.0
        if stale_for >= 120.0:
            proc.kill()
            proc.wait()
            raise RuntimeError(
                f"Render stalled (output frozen for {stale_for:.0f}s) — killed."
            )
        time.sleep(10)
    stdout, stderr = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Render failed: {(stderr or '')[-800:]}")


def _concat_parts(parts: list[Path], out_path: Path) -> None:
    """Stream-copy concat of identically-encoded chunk parts."""
    list_path = out_path.with_suffix(".concat.txt")
    list_path.write_text("".join(f"file '{str(part).replace(chr(92), '/')}'\n" for part in parts))
    try:
        args = [
            ffmpeg_bin.ffmpeg(), "-y", "-v", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            "-movflags", "+faststart",
            "-map_metadata", "-1",
            str(out_path),
        ]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(f"Concat failed: {(proc.stderr or '')[-500:]}")
    finally:
        list_path.unlink(missing_ok=True)


def verify_output(out_path: Path, expected_duration: float) -> dict:
    """Post-render sanity: exists, has both streams, duration within 1.5 s."""
    proc = subprocess.run(
        [
            ffmpeg_bin.ffprobe(), "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(out_path),
        ],
        capture_output=True, text=True, timeout=120,
    )
    import json

    info = json.loads(proc.stdout or "{}")
    streams = info.get("streams", [])
    has_v = any(s.get("codec_type") == "video" for s in streams)
    has_a = any(s.get("codec_type") == "audio" for s in streams)
    duration = float(info.get("format", {}).get("duration", 0.0))
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {
        "ok": has_v and has_a and abs(duration - expected_duration) < 1.5,
        "duration": duration,
        "width": video.get("width"),
        "height": video.get("height"),
    }
