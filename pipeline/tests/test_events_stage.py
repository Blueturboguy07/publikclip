"""Regression tests for cluster publikclip-pipeline-exit-listen-panns.

Symptom: the LISTEN stage crashes while "Detecting audio events (PANNs)…"
after the PANNs AudioSet checkpoint (models/specs.py's PANNS_CNN14_MAX, no
sha256 pinned) is a cached-but-corrupted file. models/registry.py's
ensure() trusts an already-cached file with zero validation
(`if dest.exists(): return dest`), so a truncated/corrupted download is
cached forever; vendor/panns/models.py's load_model() then calls
torch.load() on it, raising pickle.UnpicklingError / RuntimeError /
zipfile.BadZipFile depending on the truncation point -- none of which is a
`jobs.queue.StageError`. Left unwrapped, that escaped cli.py's
`except queue.StageError` uncaught and crashed the whole Python sidecar
with no final `{"event": "result", ...}` JSONL line: the Tauri shell
(app/src-tauri/src/main.rs) only sees stdout stop and a non-zero exit, and
emits a generic `{"event": "exited"}` that the UI renders as "The pipeline
exited unexpectedly." with zero attribution. The events checkpoint is only
written after a successful stage.run() return, so every "Resume" just
re-ran EventsStage into the identical crash (the reporter's "pipeline keeps
quitting" after hours on one video).

These tests exercise the two independent layers of the fix without a real
PANNs checkpoint or GPU (see oracle.sh in bugfix-lab/work for the
full-process, real-torch.load version of this same check):

1. `EventsStage.run()` now wraps both `registry.ensure(specs.PANNS_CNN14_MAX,
   ...)` and `panns_models.load_model(...)` in their own try/except and
   re-raises as `queue.StageError` with an actionable, attributed message.
2. `cli._execute()` now has a last-resort `except Exception` fallback (in
   addition to the specific `except queue.StageError`) so that ANY stage
   exception -- including ones not wrapped as StageError anywhere in the
   codebase -- still produces a graceful `{"ok": False, ...}` result
   instead of an uncaught crash.
"""

from __future__ import annotations

import _pickle
import json

import numpy as np
import pytest

from publikclip_pipeline import cli, config
from publikclip_pipeline.events.stage import EventsStage
from publikclip_pipeline.jobs import queue
from publikclip_pipeline.models import registry
from publikclip_pipeline.vendor.panns import models as panns_models


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    yield


def _settings_json() -> str:
    return json.dumps(config.Settings().to_json())


def _noop_progress(stage, fraction, message):
    pass


def _job_with_ingest_and_asr(tmp_path, monkeypatch):
    """A file-source job with ingest+asr prior data and a fake 16 kHz wav
    (librosa.load is monkeypatched below so its content is never decoded)."""
    media = tmp_path / "media.mp4"
    media.write_bytes(b"not a real video, never opened by this test")
    audio16 = tmp_path / "audio16k.wav"
    audio16.write_bytes(b"not a real wav, librosa.load is monkeypatched")

    job = queue.create_job("file", str(media), _settings_json())
    prior = {
        "ingest": {"media_path": str(media), "audio_path": str(audio16)},
        "asr": {"language": "en", "segments": []},
    }
    monkeypatch.setattr(
        "librosa.load", lambda *a, **k: (np.zeros(1600, dtype=np.float32), 16000)
    )
    return job, prior


def _run_events_stage(job, prior):
    ctx = queue._ctx_for(  # noqa: SLF001 - tests are a queue friend, same as prod _execute
        queue.StageContext(
            job=job,
            settings=config.Settings.from_json(json.loads(job.settings_json)),
            progress=_noop_progress,
        ),
        "events",
        prior,
    )
    return EventsStage().run(ctx)


def test_registry_ensure_failure_raises_stage_error_not_runtime_error(
    tmp_path, monkeypatch
):
    """registry.ensure() raising a bare RuntimeError (failed download,
    checksum mismatch) must surface as StageError, not escape uncaught."""
    job, prior = _job_with_ingest_and_asr(tmp_path, monkeypatch)

    def _boom(spec, progress):
        raise RuntimeError("Model download failed for panns-cnn14-decisionlevelmax: HTTP 503")

    monkeypatch.setattr(registry, "ensure", _boom)

    with pytest.raises(queue.StageError, match="Couldn't prepare the audio-event model"):
        _run_events_stage(job, prior)


def test_corrupted_panns_checkpoint_raises_stage_error_not_uncaught(tmp_path, monkeypatch):
    """A cached-but-corrupted checkpoint makes torch.load() raise inside
    load_model() -- that must surface as StageError naming the file, not
    escape uncaught (the exact crash this cluster reports)."""
    job, prior = _job_with_ingest_and_asr(tmp_path, monkeypatch)

    fake_ckpt = tmp_path / "Cnn14_DecisionLevelMax.pth"
    fake_ckpt.write_bytes(b"O\x00not a real torch checkpoint")  # mirrors the real poison payload

    # Pre-seed the 32 kHz wav so EventsStage.run() skips _extract_wav()'s
    # real ffmpeg subprocess (irrelevant to this bug — the fake media.mp4
    # above isn't a decodable file and would fail ffmpeg on its own).
    (job.dir / "audio32k.wav").write_bytes(b"not real audio, librosa.load is monkeypatched")

    monkeypatch.setattr(registry, "ensure", lambda spec, progress: fake_ckpt)

    def _boom(checkpoint_path, device):
        import _pickle

        raise _pickle.UnpicklingError("invalid load key, 'O'.")

    monkeypatch.setattr(panns_models, "load_model", _boom)

    with pytest.raises(queue.StageError, match="audio-event model file looks corrupted"):
        _run_events_stage(job, prior)

    # And specifically NOT the raw UnpicklingError escaping uncaught, and
    # the message names the exact file so the user has somewhere to go:
    try:
        _run_events_stage(job, prior)
    except queue.StageError as err:
        assert str(fake_ckpt) in str(err)
    else:
        pytest.fail("expected StageError")


def test_cli_execute_reports_panns_crash_gracefully_not_uncaught(tmp_path, monkeypatch):
    """End-to-end through cli._execute(): a non-StageError from EventsStage
    (this cluster's corrupted-checkpoint torch.load() failure, simulated
    here without a real checkpoint) must produce a final
    {"ok": False, ...} result, never an uncaught exception escaping
    _execute() -- that's what crashes the sidecar process for real."""

    class ExplodingEventsStage(queue.Stage):
        name = "events"
        schema_version = 2

        def run(self, ctx):
            raise _pickle.UnpicklingError("invalid load key, 'O'.")

    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "_stages", lambda: [ExplodingEventsStage()])

    job = queue.create_job("file", "/tmp/does-not-matter.mp4", _settings_json())
    # _execute() must not raise -- that's the crash this cluster is about.
    code = cli._execute(job, jsonl=False)
    assert code == 1  # CLI signals failure via exit code...
    fetched = queue.get_job(job.id)
    assert fetched.status == "failed"
    assert fetched.error  # ...but reported it, not crashed silently
