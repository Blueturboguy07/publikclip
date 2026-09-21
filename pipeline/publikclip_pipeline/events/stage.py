"""Events stage — the shared audio-event timeline (the architectural spine).

Channels:
  - jrgillick laughter specialist (native ~43 fps, high precision)
  - PANNs Cnn14_DecisionLevelMax (laugh/gasp/scream/shout/applause/cheer,
    320 ms effective resolution)
  - transcript long pauses
  - DSP energy/flux/dynamics curves (not events — continuous signals)

Fusion: per-channel DCASE post-processing, then IOU-0.4 cross-model merge
where agreement boosts confidence. Laughter is the only event type with two
independent detectors — by design (decision #2): it is the type that drives
the most visible behavior, so it gets the redundancy.

Consumed by: virality scoring (M2), caption [laughs] tags (M4), camera
punch-ins (M3), music mood (M2). Computed exactly once.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .. import config
from ..jobs.queue import Stage, StageContext, StageError
from ..models import registry, specs
from ..render import ffmpeg_bin


def _extract_wav(media: Path, dst: Path, sr: int) -> None:
    proc = subprocess.run(
        [
            ffmpeg_bin.ffmpeg(), "-y", "-i", str(media),
            "-vn", "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", str(dst),
        ],
        capture_output=True, text=True, timeout=3600,
    )
    if proc.returncode != 0:
        raise StageError(f"Audio extraction failed: {(proc.stderr or '')[-500:]}")


class EventsStage(Stage):
    name = "events"
    schema_version = 2  # v2: measured PANNs thresholds (v1 heard nothing)

    def artifacts_ok(self, ctx: StageContext, data: dict) -> bool:
        return (ctx.job_dir / "curves.json").exists()

    def run(self, ctx: StageContext) -> dict:
        prior = ctx.prior or {}
        ingest, asr = prior.get("ingest"), prior.get("asr")
        if not ingest or not asr:
            raise StageError("Events need ingest + asr outputs.")
        media = Path(ingest["media_path"])
        audio16 = Path(ingest["audio_path"])

        import json

        import librosa
        import numpy as np
        import torch

        from ..vendor.laughter import model as laugh_model
        from ..vendor.laughter import segmenter as laugh_seg
        from ..vendor.panns import models as panns_models
        from . import dsp, panns_channel, post

        device = torch.device("cpu")
        bench: dict[str, float] = {}
        events: list[dict] = []

        y16k, _ = librosa.load(str(audio16), sr=16000, mono=True)
        duration = len(y16k) / 16000.0

        # --- Channel 1 (optional): jrgillick laughter specialist ----------
        # OFF by default — PANNs' laughter classes cover the bus at a
        # fraction of the compute; this adds 10 ms precision + the
        # two-detector agreement boost when enabled.
        if getattr(ctx.settings, "laughter_specialist", False):
            ctx.emit(-1, "Detecting laughter (specialist)…")
            t0 = time.monotonic()
            ckpt = registry.ensure(specs.LAUGHTER, lambda f, m: ctx.emit(f * 0.05, m))
            y8k = librosa.resample(y16k, orig_sr=16000, target_sr=8000)
            lmodel = laugh_model.load_model(str(ckpt), device)
            laughs = laugh_seg.segment(
                lmodel, y8k, duration, device,
                progress=lambda f: ctx.emit(0.05 + f * 0.3, "Detecting laughter (specialist)…"),
            )
            for item in laughs:
                events.append(
                    {
                        "type": "laugh",
                        "start": item["start"],
                        "end": item["end"],
                        "confidence": item["confidence"],
                        "sources": ["jrgillick"],
                    }
                )
            del lmodel
            bench["laughter_sec"] = round(time.monotonic() - t0, 1)
            ctx.emit(0.35, f"{len(laughs)} laughter spans")

        # --- Channel 2: PANNs AudioSet tagger (32 kHz) --------------------
        ctx.emit(0.35, "Detecting audio events (PANNs)…")
        t0 = time.monotonic()
        try:
            ckpt = registry.ensure(specs.PANNS_CNN14_MAX, lambda f, m: ctx.emit(0.35 + f * 0.1, m))
        except Exception as err:
            # registry.ensure() raises a bare RuntimeError on a failed
            # download or a sha256 mismatch (models/registry.py) — neither
            # is a StageError, so it previously escaped cli.py's
            # `except queue.StageError` uncaught and crashed the sidecar.
            raise StageError(f"Couldn't prepare the audio-event model: {err}") from err
        wav32 = ctx.job_dir / "audio32k.wav"
        if not wav32.exists():
            _extract_wav(media, wav32, panns_models.SAMPLE_RATE)
        y32k, _ = librosa.load(str(wav32), sr=panns_models.SAMPLE_RATE, mono=True)
        try:
            pmodel = panns_models.load_model(str(ckpt), device)
        except Exception as err:
            # This spec (models/specs.py PANNS_CNN14_MAX) has no sha256
            # pinned, so registry.ensure() above trusts an already-cached
            # file with zero integrity check — a corrupted/truncated cache
            # makes torch.load() raise pickle.UnpicklingError / RuntimeError
            # / zipfile.BadZipFile depending on the truncation point, none
            # of which is a StageError either. Unwrapped, that crashed the
            # whole Python sidecar with no final result event (bugfix-lab
            # cluster publikclip-pipeline-exit-listen-panns) — the UI then
            # showed a generic "pipeline exited unexpectedly" banner and
            # every "Resume" just re-ran this stage into the same crash,
            # since the events checkpoint is only written after a
            # successful return. Name the exact file so the user has a
            # concrete next step instead of a dead end.
            raise StageError(
                f"The audio-event model file looks corrupted. Delete {ckpt} "
                "and resume the job to re-download it."
            ) from err
        probs_by_type, fps = panns_channel.framewise_probs(
            pmodel, y32k, device,
            progress=lambda f: ctx.emit(0.45 + f * 0.35, "Detecting audio events…"),
        )
        del pmodel
        for etype, probs in probs_by_type.items():
            enter, stay = panns_channel.THRESHOLDS.get(etype, (0.15, 0.08))
            for start, end, peak in post.postprocess(probs, fps, enter=enter, stay=stay):
                events.append(
                    {
                        "type": etype,
                        "start": round(start, 3),
                        "end": round(end, 3),
                        "confidence": round(min(1.0, float(peak) / panns_channel.CONF_SCALE), 3),
                        "sources": ["panns"],
                    }
                )
        bench["panns_sec"] = round(time.monotonic() - t0, 1)
        wav32.unlink(missing_ok=True)  # 32k wav is only needed here

        # --- Channel 3: transcript long pauses ----------------------------
        events.extend(dsp.long_pauses(asr["segments"]))

        # --- Fusion --------------------------------------------------------
        ctx.emit(0.85, "Fusing event timeline…")
        timeline = post.fuse(events)

        # --- Continuous curves (side file: big arrays stay out of the
        #     checkpoint JSON that other stages read constantly) ------------
        t0 = time.monotonic()
        curves = dsp.energy_curves(y16k)
        bench["curves_sec"] = round(time.monotonic() - t0, 1)

        # --- Arousal (SER with DSP fallback) ------------------------------
        ctx.emit(0.9, "Estimating arousal…")
        t0 = time.monotonic()
        from . import ser

        arousal = ser.arousal_curve_ser(
            y16k, asr["segments"], str(config.models_dir() / "ser"),
            progress=lambda f: ctx.emit(0.9 + f * 0.08, "Estimating arousal…"),
        )
        arousal_source = "ser"
        if arousal is None:
            arousal = ser.arousal_curve_dsp(curves["dynamics"], curves["grid_sec"])
            arousal_source = "dsp-proxy"
        bench["arousal_sec"] = round(time.monotonic() - t0, 1)
        curves["arousal"] = [round(float(v), 4) for v in arousal]
        curves["arousal_grid_sec"] = ser.GRID_SEC
        curves["arousal_source"] = arousal_source

        curves_path = ctx.job_dir / "curves.json"
        curves_path.write_text(json.dumps(curves))

        by_type: dict[str, int] = {}
        for event in timeline:
            by_type[event["type"]] = by_type.get(event["type"], 0) + 1

        return {
            "timeline": timeline,
            "counts": by_type,
            "curves_path": str(curves_path),
            "arousal_source": arousal_source,
            "duration_sec": round(duration, 1),
            "benchmark": bench,
            # Measured constant, recorded once for M3's punch-in math:
            "panns_effective_resolution_sec": 0.32,
        }
