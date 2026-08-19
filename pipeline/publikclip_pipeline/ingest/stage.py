"""Ingest stage: URL or file → job-dir media + analysis audio + heatmap.

Artifacts produced in the job dir:
    media.mp4      (URL jobs; file jobs keep the source path unless normalized)
    audio16k.wav   16 kHz mono analysis audio (shared by all M1 models)
    heatmap in the checkpoint data (YouTube most-replayed, when available)
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..jobs.queue import Stage, StageContext, StageError
from . import normalize, ytdlp


def _sample_hash(path: Path) -> str:
    """Cheap staleness fingerprint for external source files: size + first MiB."""
    h = hashlib.sha256()
    h.update(str(path.stat().st_size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(1024 * 1024))
    return h.hexdigest()[:16]


class IngestStage(Stage):
    name = "ingest"
    schema_version = 1

    def artifacts_ok(self, ctx: StageContext, data: dict) -> bool:
        media = Path(data.get("media_path", ""))
        audio = ctx.job_dir / "audio16k.wav"
        if not (media.exists() and audio.exists()):
            return False
        if data.get("source_hash"):
            try:
                return _sample_hash(media) == data["source_hash"] or media.parent == ctx.job_dir
            except OSError:
                return False
        return True

    def run(self, ctx: StageContext) -> dict:
        job = ctx.job

        def prog(fraction: float, message: str) -> None:
            ctx.emit(fraction, message)

        heatmap = None
        title = None
        if job.source_type == "url":
            try:
                meta = ytdlp.fetch_meta(job.source, prog)
            except ytdlp.YtDlpError as err:
                # Wrap into StageError so the error surfaces cleanly through
                # the result event (and the DB record) instead of escaping
                # as a raw exception that kills the sidecar with a bare
                # traceback — the "pipeline exited unexpectedly" reports.
                raise StageError(str(err)) from err
            heatmap = meta.heatmap
            title = meta.title
            media_path = ctx.job_dir / "media.mp4"
            if not media_path.exists():
                try:
                    ytdlp.download(job.source, media_path, prog)
                except ytdlp.YtDlpError as err:
                    raise StageError(str(err)) from err
        else:
            media_path = Path(job.source).expanduser().resolve()
            if not media_path.exists():
                raise StageError(f"File not found: {media_path}")
            title = media_path.stem

        prog(0.96, "Probing media…")
        try:
            info = normalize.probe(media_path)
        except normalize.FfmpegError as err:
            raise StageError(str(err)) from err
        if not info.has_audio:
            raise StageError(
                "This video has no audio track. publikclip needs speech to find moments."
            )

        if info.vfr:
            cfr_path = ctx.job_dir / "media_cfr.mp4"
            if not cfr_path.exists():
                normalize.normalize_to_cfr(media_path, cfr_path, info.fps, prog)
            media_path = cfr_path
            info = normalize.probe(media_path)

        prog(0.98, "Extracting analysis audio…")
        audio_path = ctx.job_dir / "audio16k.wav"
        if not audio_path.exists():
            normalize.extract_analysis_audio(media_path, audio_path)

        from ..jobs import queue as jobs_queue

        jobs_queue.set_job_status(job.id, "running", title=title)

        source_hash = None
        if job.source_type == "file":
            source_hash = _sample_hash(media_path)

        return {
            "media_path": str(media_path),
            "audio_path": str(audio_path),
            "title": title,
            "probe": info.to_json(),
            "heatmap": heatmap,
            "source_hash": source_hash,
        }
