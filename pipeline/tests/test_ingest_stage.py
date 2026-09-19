"""Regression tests for cluster publikclip-pipeline-exit-ingest-ytdlp.

Symptom: a yt-dlp failure during ingest (bad URL, stale extractor, missing
ffmpeg for the merge step, self-update-retry exhausted, watchdog kill...)
raised `ingest.ytdlp.YtDlpError`, which is NOT a `jobs.queue.StageError` and
therefore escaped `cli.py`'s `except queue.StageError` uncaught, crashing the
sidecar process with no final `{"event": "result", ...}` JSONL line. The
Tauri shell then emitted a generic `{"event": "exited"}` and the UI showed
"The pipeline exited unexpectedly..." with zero attribution.

These tests exercise the two independent layers of the fix without needing a
real yt-dlp subprocess (see oracle.sh in bugfix-lab/work for the
full-process, real-subprocess version of this same check):

1. `IngestStage.run()` now catches `ytdlp.YtDlpError` for both call sites in
   the url branch (`fetch_meta` and `download`) and re-raises it as
   `queue.StageError`, matching the `normalize.probe()`/`FfmpegError` wrap
   immediately below it in the same function.
2. `cli._execute()` now has a last-resort `except Exception` fallback (in
   addition to the specific `except queue.StageError`) so that ANY stage
   exception -- including ones not yet wrapped as StageError anywhere in the
   codebase -- still produces a graceful `{"ok": False, ...}` result instead
   of an uncaught crash.
"""

from __future__ import annotations

import json

import pytest

from publikclip_pipeline import cli, config
from publikclip_pipeline.ingest import ytdlp
from publikclip_pipeline.ingest.stage import IngestStage
from publikclip_pipeline.jobs import queue


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    yield


def _settings_json() -> str:
    return json.dumps(config.Settings().to_json())


def _noop_progress(stage, fraction, message):
    pass


def test_fetch_meta_failure_raises_stage_error_not_ytdlp_error(monkeypatch):
    """A url job whose ytdlp.fetch_meta() fails must surface as StageError."""

    def _boom(source, progress):
        progress(-1, "Updating yt-dlp…")
        raise ytdlp.YtDlpError("This video is unavailable")

    monkeypatch.setattr(ytdlp, "fetch_meta", _boom)

    job = queue.create_job("url", "https://www.youtube.com/watch?v=zzzzzzzzzzz", _settings_json())
    with pytest.raises(queue.StageError, match="This video is unavailable"):
        queue.run_stages(job, [IngestStage()], _noop_progress)

    # And specifically NOT the raw YtDlpError escaping uncaught:
    fetched = queue.get_job(job.id)
    assert fetched.status == "failed"
    assert "This video is unavailable" in (fetched.error or "")


def test_download_failure_raises_stage_error_not_ytdlp_error(monkeypatch, tmp_path):
    """A url job whose ytdlp.download() fails (post-fetch_meta) must also
    surface as StageError -- the majority-variant screenshots show the crash
    at "Downloading video…", a different call site than fetch_meta."""

    def _fake_fetch_meta(source, progress):
        return ytdlp.UrlMeta(
            id="zzzzzzzzzzz", title="t", duration_sec=1.0, webpage_url=source, heatmap=None
        )

    def _boom_download(source, out_path, progress):
        progress(0.5, "Downloading video…")
        raise ytdlp.YtDlpError("Download finished but no output file was produced.")

    monkeypatch.setattr(ytdlp, "fetch_meta", _fake_fetch_meta)
    monkeypatch.setattr(ytdlp, "download", _boom_download)

    job = queue.create_job("url", "https://www.youtube.com/watch?v=jNQXAC9IVRw", _settings_json())
    with pytest.raises(queue.StageError, match="no output file was produced"):
        queue.run_stages(job, [IngestStage()], _noop_progress)


def test_cli_execute_reports_ytdlp_failure_gracefully_not_uncaught(monkeypatch):
    """End-to-end through cli._execute(): a YtDlpError from ingest must
    produce a final {"ok": False, ...} result, never an uncaught exception
    escaping _execute() (which would crash the sidecar process)."""

    def _boom(source, progress):
        progress(-1, "Updating yt-dlp…")
        raise ytdlp.YtDlpError("This video is unavailable")

    monkeypatch.setattr(ytdlp, "fetch_meta", _boom)
    monkeypatch.setattr(cli, "_stages", lambda: [IngestStage()])

    job = queue.create_job("url", "https://www.youtube.com/watch?v=zzzzzzzzzzz", _settings_json())
    # _execute() must not raise -- that's the crash this cluster is about.
    code = cli._execute(job, jsonl=False)
    assert code == 1  # CLI signals failure via exit code...
    fetched = queue.get_job(job.id)
    assert fetched.status == "failed"
    assert "This video is unavailable" in (fetched.error or "")  # ...but reported it, not crashed


def test_cli_execute_catches_non_stage_error_from_any_stage(monkeypatch):
    """Defense-in-depth: cli._execute()'s fallback except-Exception clause
    must catch a non-StageError exception from ANY stage (not just
    YtDlpError specifically) so an unanticipated future exception type
    doesn't crash the sidecar the same way this cluster's bug did."""

    class ExplodingStage(queue.Stage):
        name = "ingest"
        schema_version = 1

        def run(self, ctx):
            raise RuntimeError("unexpected, unwrapped failure")

    monkeypatch.setattr(cli, "_stages", lambda: [ExplodingStage()])

    job = queue.create_job("file", "/tmp/does-not-matter.mp4", _settings_json())
    code = cli._execute(job, jsonl=False)
    assert code == 1
    fetched = queue.get_job(job.id)
    assert fetched.status == "failed"
    assert fetched.error  # some attribution was recorded, not silence
