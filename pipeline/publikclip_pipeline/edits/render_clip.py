"""Single-clip render with edits applied: free bounds, dead-space keep-
ranges, per-clip caption preset + camera mode, visual overlays, and a
music/sfx audio track.

One ffmpeg graph: split → per-range trim/atrim → concat → sendcmd crop
(remapped trajectory) → scale → overlays (enable windows, opt-in fade
animations) → caption burn (remapped words) → audio mix (music/sfx ducked
under speech) → loudnorm. Camera re-directs only when bounds or camera
mode differ from what the run produced.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .. import config
from ..audio_library import library as audio_lib
from ..captions import ass as ass_mod
from ..render import ffmpeg_bin, renderer
from . import store
from .timeline import AudioItem, ClipEdit, TimeRemap, detect_dead_space, keep_ranges

# sidechaincompress presets keyed by the clip's music-brief duck_intensity
# (threshold is LINEAR 0..1 — lower fires on quieter speech; ratio is the
# standard compression ratio — higher squashes harder once triggered).
DUCK_PRESETS = {
    "light": {"threshold": 0.5, "ratio": 2.0},
    "medium": {"threshold": 0.25, "ratio": 4.0},
    "heavy": {"threshold": 0.1, "ratio": 8.0},
}
# aloop's `size` (max samples to buffer+loop) is capped at INT_MAX; this is
# safely under that and far more samples than any realistic music/sfx file.
ALOOP_MAX_SIZE = 2_000_000_000


def _load_stage(job_dir: Path, stage: str) -> dict:
    return json.loads((job_dir / f"{stage}.json").read_text())["data"]


def context_for_clip(job_dir: Path, clip_idx: int, pad: float = 45.0) -> dict:
    """Everything the timeline UI needs, in one JSON blob."""
    ingest = _load_stage(job_dir, "ingest")
    diarize = _load_stage(job_dir, "diarize")
    events = _load_stage(job_dir, "events")
    score = _load_stage(job_dir, "score")
    clip = score["clips"][clip_idx]
    edit = store.edit_for_clip(job_dir, clip_idx, clip)

    duration = float(ingest["probe"]["duration_sec"])
    win_a = max(0.0, edit.start - pad)
    win_b = min(duration, edit.end + pad)

    words = [
        {"word": w["word"], "start": w["start"], "end": w["end"], "speaker": w.get("speaker", 0)}
        for seg in diarize["segments"]
        for w in seg.get("words", [])
        if win_a <= w["start"] <= win_b
    ]
    curves = json.loads(Path(events["curves_path"]).read_text())
    grid = float(curves["grid_sec"])
    rms = curves["rms"][int(win_a / grid) : int(win_b / grid)]
    clip_events = [
        e for e in events["timeline"]
        if e["type"] != "pause" and e["end"] > win_a and e["start"] < win_b
    ]
    all_words = [
        w for seg in diarize["segments"] for w in seg.get("words", [])
    ]
    cuts = detect_dead_space(all_words, events["timeline"], edit.start, edit.end)

    trajectory = None
    camera_path = job_dir / "camera.json"
    if camera_path.exists():
        cam = json.loads(camera_path.read_text())["data"]
        traj_file = cam.get("trajectories", {}).get(str(clip_idx))
        if traj_file and Path(traj_file).exists():
            t = json.loads(Path(traj_file).read_text())
            trajectory = {"fps": t.get("fps", 25), "frames": t.get("frames", [])}

    return {
        "clip_index": clip_idx,
        "window": {"start": win_a, "end": win_b},
        "media_path": ingest["media_path"],
        "probe": {"width": ingest["probe"]["width"], "height": ingest["probe"]["height"]},
        "trajectory": trajectory,
        "source_duration": duration,
        "edit": edit.to_json(),
        "words": words,
        "rms": rms,
        "rms_grid": grid,
        "events": clip_events,
        "auto_cuts": cuts,
        "run_caption_preset": _load_stage(job_dir, "render").get("caption_preset", "classic")
        if (job_dir / "render.json").exists()
        else "classic",
    }


def _camera_needs_redirect(job_dir: Path, clip_idx: int, edit: ClipEdit, score_clip: dict) -> bool:
    if abs(edit.start - score_clip["start"]) > 0.05 or abs(edit.end - score_clip["end"]) > 0.05:
        return True
    if edit.camera_mode:
        camera = _load_stage(job_dir, "camera")
        run_mode = (camera.get("camera_settings") or {}).get("speaker_change", "cut")
        return edit.camera_mode != run_mode
    return False


def _trajectory_for(job_dir: Path, clip_idx: int, edit: ClipEdit, score_clip: dict, settings: config.Settings, emit) -> dict:
    if not _camera_needs_redirect(job_dir, clip_idx, edit, score_clip):
        traj_path = _load_stage(job_dir, "camera")["trajectories"][str(clip_idx)]
        return json.loads(Path(traj_path).read_text())

    emit(-1, "Re-directing camera for new bounds…")
    import numpy as np

    from ..camera import asd as asd_mod
    from ..camera import director
    from ..camera.detect import FaceDetector
    from ..models import registry, specs

    ingest = _load_stage(job_dir, "ingest")
    diarize = _load_stage(job_dir, "diarize")
    events = _load_stage(job_dir, "events")
    curves = json.loads(Path(events["curves_path"]).read_text())

    detector = FaceDetector(str(registry.ensure(specs.ULTRAFACE, lambda f, m: None)))
    model = asd_mod.AsdModel(
        str(registry.ensure(specs.LR_ASD_FRONTEND, lambda f, m: None)),
        str(registry.ensure(specs.LR_ASD_BACKEND, lambda f, m: None)),
    )
    cam_settings = config.CameraSettings(**{**settings.camera.__dict__})
    if edit.camera_mode:
        cam_settings.speaker_change = edit.camera_mode

    src_w, src_h = int(ingest["probe"]["width"]), int(ingest["probe"]["height"])
    analysis = asd_mod.analyze_clip(
        ingest["media_path"], edit.start, edit.end, detector, model, src_w, src_h
    )
    clip_turns = [t for t in diarize["turns"] if t["end"] > edit.start and t["start"] < edit.end]
    traj = director.build_trajectory(
        analysis, clip_turns, events["timeline"],
        np.asarray(curves["dynamics"], dtype=float), float(curves["grid_sec"]),
        edit.start, edit.end, src_w, src_h, cam_settings,
    )
    return {"fps": traj.fps, "frames": traj.frames, "cuts": traj.cuts, "punches": traj.punches}


def _overlay_filters(overlays, input_offset: int, out_w: int, out_h: int) -> tuple[list[str], list[str], str, int]:
    """(extra -i args, filter chains, final label, count of -i inputs added).
    Base video label [vb]."""
    inputs: list[str] = []
    chains: list[str] = []
    label = "vb"
    added = 0
    for k, ov in enumerate(overlays):
        if not ov.image_path or not Path(ov.image_path).exists():
            continue
        idx = input_offset + added
        added += 1
        # Bound the looped image stream — an infinite input slows the final
        # filter flush and burns CPU decoding frames nothing will consume.
        inputs += ["-loop", "1", "-t", f"{ov.end + 1.0:.2f}", "-i", ov.image_path]
        w_px = int(out_w * ov.scale)
        pre = f"[{idx}:v]scale={w_px}:-2,pad=iw+16:ih+16:8:8:white@0.95,format=rgba"
        if ov.animation == "ping":
            dur = max(0.3, ov.end - ov.start)
            pre += (
                f",fade=in:st={ov.start:.2f}:d=0.18:alpha=1"
                f",fade=out:st={max(ov.start, ov.end - 0.18):.2f}:d=0.18:alpha=1"
            )
        elif ov.animation == "pop":
            pre += f",fade=in:st={ov.start:.2f}:d=0.1:alpha=1"
        chains.append(pre + f"[ov{k}]")
        x = f"(W-w)*{ov.x:.3f}"
        y = f"(H-h)*{ov.y:.3f}"
        nxt = f"vo{k}"
        chains.append(
            f"[{label}][ov{k}]overlay=x='{x}':y='{y}':enable='between(t,{ov.start:.2f},{ov.end:.2f})'[{nxt}]"
        )
        label = nxt
    return inputs, chains, label, added


def _needs_loop(item: AudioItem, lib_index: dict) -> bool:
    """Whether to apply aloop: the item wants looping AND its source file
    (looked up by library_id) is actually shorter than the placement. An
    item whose library entry vanished (removed from the library after
    being placed) loops anyway — safe default, never leaves silence."""
    if not item.loop:
        return False
    src = lib_index.get(item.library_id)
    return src is None or src.duration < item.duration


def _audio_mix_filters(
    audio_items: list[AudioItem],
    duck_intensity: str,
    input_offset: int,
    lib_index: dict,
) -> tuple[list[str], list[str], str]:
    """(extra -i args, filter chains, label to feed loudnorm instead of
    [ac]). Empty audio_items returns ([], [], "ac") — the graph is then
    byte-identical to the no-audio-track path."""
    if not audio_items:
        return [], [], "ac"

    inputs: list[str] = []
    chains: list[str] = []
    preset = DUCK_PRESETS.get(duck_intensity, DUCK_PRESETS["medium"])
    duck_count = sum(1 for item in audio_items if item.duck)

    # The normalized speech stream is consumed by amix AND, once per ducked
    # item, as a sidechaincompress key. Referencing one [ac_fmt] label from
    # multiple filters lets ffmpeg's format auto-negotiation silently pick a
    # DIFFERENT format for it (observed: reverts to the source's native rate/
    # mono instead of the requested 48k stereo) — asplit gives every consumer
    # its own copy of the already-formatted stream so none of them renegotiate.
    if duck_count:
        split_labels = ["ac_fmt"] + [f"ac_sc{i}" for i in range(duck_count)]
        chains.append(
            "[ac]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"asplit={len(split_labels)}" + "".join(f"[{lbl}]" for lbl in split_labels)
        )
        sidechain_labels = iter(split_labels[1:])
    else:
        chains.append("[ac]aformat=sample_rates=48000:channel_layouts=stereo[ac_fmt]")
        sidechain_labels = iter(())

    mix_labels = ["ac_fmt"]

    for k, item in enumerate(audio_items):
        idx = input_offset + k
        inputs += ["-i", item.path]

        steps = []
        if _needs_loop(item, lib_index):
            steps.append(f"aloop=loop=-1:size={ALOOP_MAX_SIZE}")
        steps.append(f"atrim=duration={item.duration:.3f}")
        steps.append("asetpts=PTS-STARTPTS")
        steps.append(f"volume={item.gain_db:.2f}dB")
        if item.fade_in > 0:
            steps.append(f"afade=t=in:st=0:d={item.fade_in:.3f}")
        if item.fade_out > 0:
            fade_start = max(0.0, item.duration - item.fade_out)
            steps.append(f"afade=t=out:st={fade_start:.3f}:d={item.fade_out:.3f}")
        delay_ms = max(0, round(item.start * 1000))
        steps.append(f"adelay=delays={delay_ms}:all=1")
        steps.append("aformat=sample_rates=48000:channel_layouts=stereo")

        label = f"aitem{k}"
        chains.append(f"[{idx}:a]" + ",".join(steps) + f"[{label}]")

        if item.duck:
            duck_label = f"{label}d"
            sc_label = next(sidechain_labels)
            chains.append(
                f"[{label}][{sc_label}]sidechaincompress="
                f"threshold={preset['threshold']}:ratio={preset['ratio']}[{duck_label}]"
            )
            mix_labels.append(duck_label)
        else:
            mix_labels.append(label)

    mix_in = "".join(f"[{lbl}]" for lbl in mix_labels)
    chains.append(f"{mix_in}amix=inputs={len(mix_labels)}:duration=first:normalize=0[ac_mixed]")
    return inputs, chains, "ac_mixed"


def _write_credits(out_dir: Path, clip_idx: int, audio_items: list[AudioItem], lib_index: dict) -> None:
    """credits.txt next to the rendered clip, listing attribution for every
    CC-BY item used — CC0 and local items need no credit."""
    lines = []
    for item in audio_items:
        src = lib_index.get(item.library_id)
        if src and src.licence and src.licence.startswith("CC-BY") and src.attribution:
            lines.append(src.attribution)
    credits_path = out_dir / f"clip_{clip_idx:02d}.credits.txt"
    if lines:
        credits_path.write_text("\n".join(lines) + "\n")
    else:
        credits_path.unlink(missing_ok=True)  # stale credits from a prior render


def render_clip_edit(job_dir: Path, clip_idx: int, emit) -> dict:
    """The per-clip render path. Returns the updated output entry."""
    ingest = _load_stage(job_dir, "ingest")
    diarize = _load_stage(job_dir, "diarize")
    events = _load_stage(job_dir, "events")
    score = _load_stage(job_dir, "score")
    settings = config.Settings.from_json(json.loads((job_dir / "settings.json").read_text()))
    clip = score["clips"][clip_idx]
    edit = store.edit_for_clip(job_dir, clip_idx, clip)

    # --- keep ranges + remap ------------------------------------------------
    if edit.remove_dead_space:
        all_words = [w for seg in diarize["segments"] for w in seg.get("words", [])]
        cuts = detect_dead_space(all_words, events["timeline"], edit.start, edit.end)
        ranges = keep_ranges(edit.start, edit.end, cuts, edit.disabled_cuts)
    else:
        ranges = [(edit.start, edit.end)]
    remap = TimeRemap(ranges)

    # --- camera -------------------------------------------------------------
    trajectory = _trajectory_for(job_dir, clip_idx, edit, clip, settings, emit)
    fps = float(trajectory.get("fps", 25))
    # Trajectory frames start at edit.start whether reused (bounds unchanged
    # → edit.start == run start) or freshly re-directed for new bounds.
    frames = remap.remap_trajectory(trajectory["frames"], fps, edit.start)

    src_w, src_h = int(ingest["probe"]["width"]), int(ingest["probe"]["height"])
    boxes = renderer.crop_boxes(frames, src_w, src_h)
    if not boxes:
        boxes = [(src_h * 9 // 16 // 2 * 2, src_h - src_h % 2, 0, 0)]

    # --- captions (remapped) ------------------------------------------------
    words_src = [
        {"word": w["word"], "start": w["start"], "end": w["end"]}
        for seg in diarize["segments"]
        for w in seg.get("words", [])
        if edit.start <= w["start"] < edit.end
    ]
    words_out = remap.remap_words(words_src)
    curves = json.loads(Path(events["curves_path"]).read_text())
    cap_words = [ass_mod.Word(text=w["word"], start=w["start"], end=w["end"]) for w in words_out]
    # Emphasis is a SOURCE-time property (per-word RMS in the original
    # audio): mark it on source-timed copies, then carry each surviving
    # word's flag across in order.
    src_cap = [
        ass_mod.Word(text=w["word"], start=w["start"] - edit.start, end=w["end"] - edit.start)
        for w in words_src
    ]
    ass_mod.mark_emphasis(src_cap, curves["rms"], float(curves["grid_sec"]), clip_start=edit.start)
    out_idx = 0
    for w_src, w_flagged in zip(words_src, src_cap):
        survives = remap.to_output((w_src["start"] + w_src["end"]) / 2) is not None
        if survives and out_idx < len(cap_words):
            cap_words[out_idx].emphasized = w_flagged.emphasized
            out_idx += 1

    clip_events_out = []
    for e in events["timeline"]:
        if e["type"] == "pause" or e["end"] <= edit.start or e["start"] >= edit.end:
            continue
        mid = (e["start"] + e["end"]) / 2
        if remap.to_output(mid) is None:
            continue
        clip_events_out.append(
            {
                "type": e["type"],
                "start": remap.to_output_clamped(e["start"]),
                "end": remap.to_output_clamped(e["end"]),
            }
        )

    preset = edit.caption_preset or settings.caption_preset
    captions_ok = ffmpeg_bin.supports_captions()
    emoji_ok = ass_mod.emoji_probe() if captions_ok else False
    out_dir = job_dir / "clips"
    out_dir.mkdir(exist_ok=True)
    ass_path = out_dir / f"clip_{clip_idx:02d}.ass"
    ass_path.write_text(ass_mod.build_ass(cap_words, clip_events_out, preset_name=preset, emoji_ok=emoji_ok))

    # --- build the graph ----------------------------------------------------
    emit(-1, "Rendering clip…")
    span_a = ranges[0][0]
    span_b = ranges[-1][1]
    n = len(ranges)
    trims = []
    for i, (a, b) in enumerate(ranges):
        ra, rb = a - span_a, b - span_a
        trims.append(f"[0:v]trim=start={ra:.3f}:end={rb:.3f},setpts=PTS-STARTPTS[v{i}]")
        trims.append(f"[0:a]atrim=start={ra:.3f}:end={rb:.3f},asetpts=PTS-STARTPTS[a{i}]")
    concat_in = "".join(f"[v{i}][a{i}]" for i in range(n))
    graph = trims + [f"{concat_in}concat=n={n}:v=1:a=1[vc][ac]"]

    cmd_path = out_dir / f"clip_{clip_idx:02d}.cmd"
    cmd_path.write_text("\n".join(renderer.sendcmd_lines(boxes, fps)) + "\n")
    vchain = (
        f"[vc]sendcmd=f={renderer._q(cmd_path)},"  # noqa: SLF001
        f"crop@c=w={boxes[0][0]}:h={boxes[0][1]}:x={boxes[0][2]}:y={boxes[0][3]},"
        f"scale={renderer.OUT_W}:{renderer.OUT_H}:flags=lanczos,setsar=1[vb]"
    )
    graph.append(vchain)

    ov_inputs, ov_chains, vlabel, ov_added = _overlay_filters(edit.overlays, 1, renderer.OUT_W, renderer.OUT_H)
    graph.extend(ov_chains)
    if captions_ok:
        graph.append(
            f"[{vlabel}]subtitles=filename={renderer._q(ass_path)}:fontsdir={renderer._q(ass_mod.FONTS_DIR)}[vf]"  # noqa: SLF001
        )
        vlabel = "vf"

    # --- audio track (music/sfx, ducked under speech) ------------------------
    lib_index = audio_lib.load_index()
    duck_intensity = (clip.get("music") or {}).get("duck_intensity") or "medium"
    audio_inputs, audio_chains, speech_label = _audio_mix_filters(
        edit.audio, duck_intensity, 1 + ov_added, lib_index
    )
    graph.extend(audio_chains)
    graph.append(f"[{speech_label}]loudnorm=I={settings.lufs_target}:TP={settings.true_peak_db}:LRA=11[af]")

    if renderer.videotoolbox_available():
        vcodec = ["-c:v", "h264_videotoolbox", "-b:v", renderer.VT_BITRATE, "-allow_sw", "1"]
    else:
        vcodec = ["-c:v", "libx264", "-preset", "medium", "-crf", str(renderer.X264_CRF)]

    out_path = out_dir / f"clip_{clip_idx:02d}.mp4"
    args = [
        ffmpeg_bin.ffmpeg(), "-y", "-v", "error",
        "-ss", f"{span_a:.3f}", "-t", f"{span_b - span_a:.3f}", "-i", ingest["media_path"],
        *ov_inputs,
        *audio_inputs,
        "-filter_complex", ";".join(graph),
        "-map", f"[{vlabel}]", "-map", "[af]",
        *vcodec,
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", "-map_metadata", "-1",
        str(out_path),
    ]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    cmd_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Clip render failed: {(proc.stderr or '')[-800:]}")
    _write_credits(out_dir, clip_idx, edit.audio, lib_index)

    check = renderer.verify_output(out_path, remap.output_duration)
    entry = {
        "clip": clip_idx,
        "path": str(out_path),
        "ass": str(ass_path),
        "score": clip["score"],
        "best_platform": clip["best_platform"],
        "duration": round(check["duration"], 2),
        "words": len(cap_words),
        "event_tags": len(clip_events_out),
        "edited": True,
    }

    # keep render.json in sync so the review UI reflects the new file
    render_ckpt_path = job_dir / "render.json"
    if render_ckpt_path.exists():
        ckpt = json.loads(render_ckpt_path.read_text())
        outputs = ckpt["data"].get("outputs", [])
        for i, o in enumerate(outputs):
            if o["clip"] == clip_idx:
                outputs[i] = entry
                break
        else:
            outputs.append(entry)
        tmp = render_ckpt_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(ckpt))
        tmp.replace(render_ckpt_path)
    return entry
