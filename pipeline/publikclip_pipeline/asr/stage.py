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
import subprocess
import time
import wave
from pathlib import Path

from .. import config
from ..jobs.queue import Stage, StageContext, StageError

ASR_MODEL = "large-v3-turbo"
COMPUTE_TYPE = "int8"
BATCH_SIZE = 8
SAMPLE_RATE = 16000  # whisperx.audio.SAMPLE_RATE


def load_audio(path: Path):
    """16 kHz mono float32 waveform — the array whisperx's transcribe/align
    take — WITHOUT `whisperx.load_audio`.

    That helper shells out to a bare ``ffmpeg`` on PATH. The packaged app's
    sidecar inherits Finder's PATH (/usr/bin:/bin:/usr/sbin:/sbin), which
    holds no ffmpeg, so on every real install this stage died with
    ``FileNotFoundError(2, 'No such file or directory')`` right after
    "Loading speech model…" — while every other stage went through
    render/ffmpeg_bin and worked. Ingest already wrote the analysis wav as
    16 kHz mono s16le, so the common case needs no decoder at all; anything
    else is decoded by the SAME ffmpeg the rest of the pipeline resolved.
    """
    import numpy as np  # deferred: cli.py imports this module before deps exist

    try:
        with wave.open(str(path), "rb") as wav:
            layout = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate())
            if layout == (1, 2, SAMPLE_RATE):
                raw = wav.readframes(wav.getnframes())
                return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    except (wave.Error, EOFError, OSError):
        pass
    return _decode_with_ffmpeg(path)


def _decode_with_ffmpeg(path: Path):
    """Same command line as whisperx.load_audio, same output — but the binary
    is the one publikclip resolved, by absolute path, not whatever PATH has."""
    import numpy as np

    from ..render import ffmpeg_bin

    cmd = [
        ffmpeg_bin.ffmpeg(), "-nostdin", "-threads", "0", "-i", str(path),
        "-f", "s16le", "-ac", "1", "-acodec", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, check=True).stdout
    except FileNotFoundError as err:
        raise StageError(
            "No ffmpeg found to decode the audio — re-run ingest so publikclip can fetch one."
        ) from err
    except subprocess.CalledProcessError as err:
        tail = err.stderr.decode("utf-8", errors="replace")[-2000:]
        raise StageError(f"ffmpeg could not decode the analysis audio: {tail}") from err
    return np.frombuffer(out, np.int16).flatten().astype(np.float32) / 32768.0


def _point_caches_at_home() -> None:
    """All model caches live under PUBLIKCLIP_HOME so 'delete the app data
    dir' is a complete uninstall."""
    hf_home = config.models_dir() / "hf"
    hf_home.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(hf_home))
    os.environ.setdefault("TORCH_HOME", str(config.models_dir() / "torch"))


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
        audio = load_audio(audio_path)
        duration = float(len(audio)) / SAMPLE_RATE

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
