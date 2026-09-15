"""ASR + forced alignment via whisperX (BSD-2-Clause, pinned 3.8.6).

Word-level timestamps are the substrate for everything downstream: captions,
[laughs] tag placement, prosodic emphasis, long-pause detection, ducking,
and sentence-snapped candidate boundaries.

Model choice: large-v3-turbo int8 by default — near-parity accuracy with
large-v3 at a fraction of the compute, which is what makes local-first
viable on Apple Silicon. Silero VAD (MIT) instead of whisperX's bundled
pyannote VAD checkpoint, whose license the research flagged as unresolved.

The stage records wall-clock + realtime factor into its checkpoint — the M1
gate's Apple Silicon benchmark comes from real runs, not synthetic tests.
"""

from __future__ import annotations

import gc
import os
import time
from pathlib import Path

from .. import config
from ..jobs.queue import Stage, StageContext, StageError

ASR_MODEL = "large-v3-turbo"
COMPUTE_TYPE = "int8"
BATCH_SIZE = 8


def _point_caches_at_home() -> None:
    """All model caches live under PUBLIKCLIP_HOME so 'delete the app data
    dir' is a complete uninstall."""
    hf_home = config.models_dir() / "hf"
    hf_home.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(hf_home))
    os.environ.setdefault("TORCH_HOME", str(config.models_dir() / "torch"))
    # TorchCodec/FFmpeg loads native DLLs during ASR initialization on
    # Windows. Ensure the managed static FFmpeg directory is visible to both
    # the process loader and child tools before importing torch/whisperx.
    if os.name == "nt":
        bin_dir = config.bin_dir()
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(str(bin_dir))
        except (AttributeError, OSError):
            pass


class AsrStage(Stage):
    name = "asr"
    schema_version = 1

    def run(self, ctx: StageContext) -> dict:
        ingest = ctx.prior.get("ingest") if ctx.prior else None
        if not ingest:
            raise StageError("ASR needs the ingest stage output.")
        audio_path = Path(ingest["audio_path"])
        if not audio_path.exists():
            raise StageError("Analysis audio missing — re-run ingest.")

        _point_caches_at_home()
        ctx.emit(-1, "Loading speech model (downloads ~1.6 GB on first run)…")
        import torch  # deferred: heavy import
        import whisperx

        device = "cpu"  # ctranslate2 has no MPS backend; int8 CPU is the local path
        t0 = time.monotonic()
        model = whisperx.load_model(
            ASR_MODEL, device, compute_type=COMPUTE_TYPE, vad_method="silero"
        )
        audio = whisperx.load_audio(str(audio_path))
        duration = float(len(audio)) / 16000.0

        ctx.emit(-1, "Transcribing…")
        result = model.transcribe(audio, batch_size=BATCH_SIZE)
        language = result.get("language", "en")
        transcribe_secs = time.monotonic() - t0

        # Free ASR weights before loading the alignment model — peak RSS on a
        # 24 GB machine matters more than reload cost.
        del model
        gc.collect()

        ctx.emit(-1, "Aligning words…")
        t1 = time.monotonic()
        align_model, align_meta = whisperx.load_align_model(language_code=language, device=device)
        aligned = whisperx.align(
            result["segments"], align_model, align_meta, audio, device,
            return_char_alignments=False,
        )
        align_secs = time.monotonic() - t1
        del align_model
        gc.collect()
        if hasattr(torch, "mps") and torch.backends.mps.is_available():
            torch.mps.empty_cache()

        segments = []
        for seg in aligned["segments"]:
            words = [
                {
                    "word": w.get("word", "").strip(),
                    "start": round(float(w["start"]), 3),
                    "end": round(float(w["end"]), 3),
                    "score": round(float(w.get("score", 0.0)), 3),
                }
                for w in seg.get("words", [])
                if "start" in w and "end" in w
            ]
            segments.append(
                {
                    "start": round(float(seg["start"]), 3),
                    "end": round(float(seg["end"]), 3),
                    "text": seg.get("text", "").strip(),
                    "words": words,
                }
            )

        word_count = sum(len(s["words"]) for s in segments)
        if word_count == 0:
            raise StageError(
                "No speech was found in this video. publikclip needs dialogue to find moments."
            )

        total = transcribe_secs + align_secs
        return {
            "language": language,
            "model": ASR_MODEL,
            "compute_type": COMPUTE_TYPE,
            "segments": segments,
            "word_count": word_count,
            "benchmark": {
                "audio_sec": round(duration, 1),
                "transcribe_sec": round(transcribe_secs, 1),
                "align_sec": round(align_secs, 1),
                "realtime_factor": round(duration / total, 2) if total > 0 else None,
            },
        }
