"""First launch of the packaged desktop app on a machine that has nothing.

A Finder / Start-menu launch gives the app a bare PATH, and a fresh Mac has no
ffmpeg at all. Both used to end a first run before any stage did real work:

* `_ensure_pipeline_deps` ran `uv sync` by bare name -> `[Errno 2] No such file
  or directory: 'uv'`, so the very first job failed with "Couldn't install
  pipeline dependencies".
* the ingest stage probed the file with a bare `ffprobe`; the static ffmpeg
  was only fetched at render time, after ingest had already failed.
"""

from __future__ import annotations

import subprocess

import pytest

from publikclip_pipeline import cli
from publikclip_pipeline.ingest import stage as ingest_stage
from publikclip_pipeline.jobs import queue


def _fake_uv(tmp_path, name="uv"):
    uv = tmp_path / name
    uv.write_text("#!/bin/sh\n")
    uv.chmod(0o755)
    return str(uv)


def test_uv_binary_prefers_the_one_the_shell_bundles(tmp_path, monkeypatch):
    bundled = _fake_uv(tmp_path, "bundled-uv")
    exported = _fake_uv(tmp_path, "exported-uv")
    monkeypatch.setenv("PUBLIKCLIP_UV", bundled)
    monkeypatch.setenv("UV", exported)
    assert cli._uv_binary() == bundled


def test_uv_binary_falls_back_to_the_one_uv_run_exports(tmp_path, monkeypatch):
    exported = _fake_uv(tmp_path)
    monkeypatch.delenv("PUBLIKCLIP_UV", raising=False)
    monkeypatch.setenv("UV", exported)
    assert cli._uv_binary() == exported


def test_uv_binary_ignores_a_path_that_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_UV", str(tmp_path / "gone"))
    monkeypatch.delenv("UV", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing named uv on it
    assert cli._uv_binary() == "uv"


def test_dependency_sync_runs_the_bundled_uv_when_path_has_no_uv(tmp_path, monkeypatch):
    """The failure from the field: a bare PATH and a bundled uv."""
    bundled = _fake_uv(tmp_path)
    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PUBLIKCLIP_UV", bundled)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    seen: dict = {}

    def fake_run(args, **_kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, err = cli._ensure_pipeline_deps(False, lambda *_: None)
    assert (ok, err) == (True, None)
    assert seen["args"][0] == bundled
    assert seen["args"][-4:] == ["sync", "--frozen", "--group", "pipeline"]


def _ingest_ctx(tmp_path, monkeypatch, source):
    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    job = queue.create_job("file", str(source), "{}")
    return queue.StageContext(job=job, settings=None, progress=lambda *_: None)


def test_ingest_fetches_ffmpeg_before_it_probes(tmp_path, monkeypatch):
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"not really a video")
    ctx = _ingest_ctx(tmp_path, monkeypatch, video)

    order: list[str] = []
    monkeypatch.setattr(
        ingest_stage.ffmpeg_bin, "ensure_capable", lambda progress=None: order.append("ensure") or True
    )
    monkeypatch.setattr(ingest_stage.ffmpeg_bin, "available", lambda: True)

    class Stop(Exception):
        pass

    def fake_probe(_path):
        order.append("probe")
        raise Stop

    monkeypatch.setattr(ingest_stage.normalize, "probe", fake_probe)
    with pytest.raises(Stop):
        ingest_stage.IngestStage().run(ctx)
    assert order == ["ensure", "probe"]


def test_ingest_says_so_when_no_ffmpeg_can_be_found_or_fetched(tmp_path, monkeypatch):
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"not really a video")
    ctx = _ingest_ctx(tmp_path, monkeypatch, video)

    monkeypatch.setattr(ingest_stage.ffmpeg_bin, "ensure_capable", lambda progress=None: False)
    monkeypatch.setattr(ingest_stage.ffmpeg_bin, "available", lambda: False)
    monkeypatch.setattr(
        ingest_stage.normalize, "probe", lambda _p: pytest.fail("probed with no ffmpeg")
    )
    with pytest.raises(queue.StageError, match="ffmpeg"):
        ingest_stage.IngestStage().run(ctx)


def test_net_sync_uses_the_bundled_uv_and_never_uninstalls_the_pipeline_group(tmp_path, monkeypatch):
    """The other field failure on the same fresh install: opening the Loop
    screen before any job ran died with `ig tool produced no JSON:
    Traceback … No module named 'httpx'`."""
    bundled = _fake_uv(tmp_path)
    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PUBLIKCLIP_UV", bundled)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    seen: dict = {}

    def fake_run(args, **_kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli._ensure_net_deps(False, lambda *_: None) == (True, None)
    assert seen["args"][0] == bundled
    # --inexact: a `uv sync` without it makes the env match the requested
    # groups exactly, which would rip the multi-GB pipeline group back out.
    assert seen["args"][-5:] == ["sync", "--frozen", "--inexact", "--group", "net"]


def test_net_sync_is_skipped_once_the_pipeline_group_is_in(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".pipeline_deps_synced").write_text("ok")
    monkeypatch.setenv("PUBLIKCLIP_HOME", str(home))
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("synced anyway"))
    assert cli._ensure_net_deps(False, lambda *_: None) == (True, None)


@pytest.mark.parametrize("argv", [["ig", "overview"], ["audio", "list", "--json"]])
def test_loop_and_library_commands_answer_json_when_the_net_sync_fails(argv, tmp_path, monkeypatch, capsys):
    import json

    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "_ensure_net_deps", lambda *_: (False, "no network"))
    assert cli.main(argv) == 1
    last = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(last)
    assert payload["ok"] is False
    assert "no network" in payload["error"]


@pytest.mark.parametrize("argv", [["ig", "overview"], ["audio", "list", "--json"]])
def test_loop_and_library_commands_sync_net_deps_before_importing(argv, tmp_path, monkeypatch, capsys):
    import json

    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    calls: list[str] = []
    monkeypatch.setattr(cli, "_ensure_net_deps", lambda *_: calls.append("net") or (True, None))
    assert cli.main(argv) == 0
    assert calls == ["net"]
    json.loads(capsys.readouterr().out.strip().splitlines()[-1])
