"""Regression test for the 'pipeline exited unexpectedly' bug.

Reproduces the exact failure mode from publikclip issue #2: a stage raises a
NON-StageError exception (YtDlpError, model-download error, OOM, …). Before
the fix, the sidecar exited with a bare traceback and NO result event, so
the desktop shell showed only 'The pipeline exited unexpectedly' with the
real error lost. After the fix, _execute catches it and emits a JSONL
result event with the error, and the exit code is 1 (recoverable), not a
crash.
"""
import json

from publikclip_pipeline import config
from publikclip_pipeline import cli
from publikclip_pipeline.jobs import queue


class Boom(Exception):
    """Stand-in for YtDlpError / OOM / any non-StageError crash."""


class ExplodingStage(queue.Stage):
    name = "boom"
    schema_version = 1

    def run(self, ctx):
        raise Boom("yt-dlp: HTTP 403 (simulated)")


def _run_sidecar(stage, monkeypatch):
    monkeypatch.setattr(cli, "_stages", lambda: [stage])
    cfg = config.Settings()
    job = queue.create_job("url", "https://example.com/v", json.dumps(cfg.to_json()))
    return job, cli._execute(job, jsonl=True)


def test_generic_stage_crash_emits_result_event(monkeypatch, capsys):
    job, code = _run_sidecar(ExplodingStage(), monkeypatch)
    out = capsys.readouterr().out
    lines = [json.loads(l) for l in out.splitlines() if l.strip()]
    results = [l for l in lines if l.get("event") == "result"]
    assert len(results) == 1, f"expected one result event, got {results}"
    assert results[0]["ok"] is False
    assert "HTTP 403" in results[0]["error"]
    assert code == 1  # recoverable exit, NOT a bare crash
    db_job = queue.get_job(job.id)
    assert db_job.status == "failed"
    assert db_job.error and "boom" in db_job.error
    assert queue.stage_statuses(job.id)["boom"] == "failed"
    assert not queue.checkpoint_path(job, "boom").exists()  # resume re-runs it


def test_stage_error_still_emits(monkeypatch, capsys):
    class ErrStage(queue.Stage):
        name = "boom2"
        schema_version = 1

        def run(self, ctx):
            raise queue.StageError("user-facing failure")

    job, code = _run_sidecar(ErrStage(), monkeypatch)
    out = capsys.readouterr().out
    results = [json.loads(l) for l in out.splitlines() if l.strip() and json.loads(l).get("event") == "result"]
    assert len(results) == 1
    assert results[0]["ok"] is False
    assert "user-facing failure" in results[0]["error"]
    assert code == 1
