"""Diarization stage: CAM++ embeddings over ASR speech windows → speaker
turns → word-level speaker labels merged back into the transcript."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ..jobs.queue import Stage, StageContext, StageError
from ..models import registry, specs


class DiarizeStage(Stage):
    name = "diarize"
    schema_version = 2  # v2: refined affinity (v1 collapsed real two-host audio to one speaker)

    def run(self, ctx: StageContext) -> dict:
        prior = ctx.prior or {}
        ingest, asr = prior.get("ingest"), prior.get("asr")
        if not ingest or not asr:
            raise StageError("Diarization needs ingest + asr outputs.")
        audio_path = Path(ingest["audio_path"])
        if not audio_path.exists():
            raise StageError("Analysis audio missing — re-run ingest.")

        import torch

        from . import campplus, cluster

        ctx.emit(-1, "Loading speaker model…")
        try:
            ckpt = registry.ensure(specs.CAMPPLUS, lambda f, m: ctx.emit(f * 0.2, m))
            device = torch.device("cpu")
            model = campplus.load_model(str(ckpt), device)
        except Exception as err:
            # registry.ensure() trusts an already-cached file with zero
            # integrity check (CAM++ has no sha256 pinned in models/specs.py,
            # and even a *fresh* download is only verified `if spec.sha256:`)
            # so a silently truncated/corrupted download is cached forever
            # and campplus.load_model()'s torch.load() fails the identical
            # way on every subsequent run — matching the report ("it always
            # fails in loading speaker model step") since Resume just re-runs
            # this stage against the same bad file. Clear the cache so the
            # next attempt re-downloads instead of repeating the crash, and
            # raise a specific, resumable StageError instead of letting the
            # raw torch/pickle exception escape uncaught — an uncaught
            # exception here crashes the whole sidecar process with its
            # stderr discarded (app/src-tauri/src/main.rs redirects it to
            # Stdio::null()), which is what produced the generic "pipeline
            # exited unexpectedly" banner with no stage attribution.
            try:
                registry.model_path(specs.CAMPPLUS).unlink(missing_ok=True)
            except OSError:
                pass
            raise StageError(
                "Speaker model checkpoint failed to load and has been "
                "cleared from the cache — resume to re-download it and "
                "retry diarization."
            ) from err

        import librosa

        y16k, _ = librosa.load(str(audio_path), sr=16000, mono=True)
        duration = len(y16k) / 16000.0

        segments = asr["segments"]
        windows = campplus.speech_windows(segments, duration)
        if not windows:
            return {"speakers": 0, "turns": [], "segments": segments}

        # Mid-stage cache: embedding an hour of speech costs real minutes and
        # the stage checkpoint only lands at the end — a crash after embedding
        # shouldn't re-pay it.
        cache_path = ctx.job_dir / "diar_embeddings.npy"
        embeddings = None
        if cache_path.exists():
            cached = np.load(cache_path)
            if len(cached) == len(windows):
                embeddings = cached
                ctx.emit(0.8, "Embeddings cached")
        if embeddings is None:
            ctx.emit(0.25, f"Embedding {len(windows)} speech windows…")
            embeddings = campplus.embed_windows(
                model, y16k, windows, device,
                progress=lambda f: ctx.emit(0.25 + f * 0.55, "Embedding speech…"),
            )
            np.save(cache_path, embeddings)

        ctx.emit(0.85, "Clustering speakers…")
        labels = cluster.cluster_windows(embeddings)
        turns = cluster.build_turns(windows, labels)
        cluster.assign_words(segments, turns)

        speakers = int(len(np.unique(labels))) if len(labels) else 0
        return {
            "speakers": speakers,
            "turns": turns,
            "segments": segments,  # transcript enriched with word/segment speakers
        }
