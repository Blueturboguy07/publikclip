"""TRANSCRIBE on a host without the memory for large-v3-turbo.

Oct 10 2026, Windows, 8 GB laptop (Ayaan): INGEST done, then TRANSCRIBE
died at "Loading speech model (downloads ~1.6 GB on first run)…" with
``asr: RuntimeError: mkl_malloc: failed to allocate memory`` — eight runs
in a row. ctranslate2 reads the 1.6 GB fp16 file and quantizes to int8 in
memory, and its allocator throws that exact text when the host says no.
The stage used one hard-coded model and let the exception escape.

These tests pin the contract: free memory picks the starting model, a
failed allocation (at load or at transcribe) steps down the ladder once per
model, the switch is reported to the progress panel and recorded in the
checkpoint, every other error stops at once, and running out of ladder is
one human line."""

from __future__ import annotations

import json
import subprocess
import sys
import types
import wave

import numpy as np
import pytest

from publikclip_pipeline import config, sysinfo
from publikclip_pipeline.asr import stage as asr_stage
from publikclip_pipeline.jobs import queue

GIB = sysinfo.GIB
MKL = "mkl_malloc: failed to allocate memory"
TORCH_OOM = "[enforce fail at alloc_cpu.cpp:114] . DefaultCPUAllocator: not enough memory: you tried to allocate 1234567890 bytes."


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PUBLIKCLIP_ASR_MODEL", raising=False)
    monkeypatch.delenv("PUBLIKCLIP_MEM_AVAILABLE_MB", raising=False)


def _wav(path, seconds: float = 1.0) -> None:
    t = np.arange(int(asr_stage.SAMPLE_RATE * seconds)) / asr_stage.SAMPLE_RATE
    samples = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(asr_stage.SAMPLE_RATE)
        wav.writeframes(samples.tobytes())


class _FakeModel:
    def __init__(self, name: str, transcribe_error: str | None):
        self.name = name
        self.transcribe_error = transcribe_error

    def transcribe(self, audio, batch_size):
        if self.transcribe_error:
            raise RuntimeError(self.transcribe_error)
        return {
            "language": "en",
            "segments": [{"start": 0.0, "end": 1.0, "text": f"hello from {self.name}"}],
        }


def _fake_whisperx(load_errors: dict[str, str], transcribe_errors: dict[str, str] | None = None):
    """A whisperx whose load_model/transcribe fail per model name with the
    given message. Records every load attempt and batch size."""
    transcribe_errors = transcribe_errors or {}
    mod = types.ModuleType("whisperx")
    mod.loads: list[str] = []
    mod.batches: list[int] = []

    def load_model(name, device, compute_type, vad_method):
        mod.loads.append(name)
        if name in load_errors:
            raise RuntimeError(load_errors[name])
        model = _FakeModel(name, transcribe_errors.get(name))
        original = model.transcribe

        def transcribe(audio, batch_size):
            mod.batches.append(batch_size)
            return original(audio, batch_size)

        model.transcribe = transcribe
        return model

    def load_align_model(language_code, device):
        return object(), {"language": language_code}

    def align(segments, model, meta, audio, device, return_char_alignments):
        return {
            "segments": [
                {
                    "start": s["start"],
                    "end": s["end"],
                    "text": s["text"],
                    "words": [
                        {"word": w, "start": s["start"] + i * 0.1, "end": s["start"] + i * 0.1 + 0.05, "score": 0.9}
                        for i, w in enumerate(s["text"].split())
                    ],
                }
                for s in segments
            ]
        }

    mod.load_model = load_model
    mod.load_align_model = load_align_model
    mod.align = align
    return mod


def _run(tmp_path, monkeypatch, whisperx_mod, available):
    monkeypatch.setitem(sys.modules, "whisperx", whisperx_mod)
    monkeypatch.setitem(sys.modules, "torch", types.ModuleType("torch"))  # no .mps attr: short-circuits the cleanup branch
    monkeypatch.setattr(sysinfo, "available_memory_bytes", lambda: available)
    media = tmp_path / "media.mp4"
    media.write_bytes(b"never opened")
    audio = tmp_path / "audio16k.wav"
    _wav(audio)
    job = queue.create_job("file", str(media), json.dumps(config.Settings().to_json()))
    messages: list[str] = []
    ctx = queue._ctx_for(  # noqa: SLF001 - same friend access as test_events_stage
        queue.StageContext(
            job=job,
            settings=config.Settings(),
            progress=lambda stage, fraction, message: messages.append(message),
        ),
        "asr",
        {"ingest": {"media_path": str(media), "audio_path": str(audio)}},
    )
    return asr_stage.AsrStage().run(ctx), messages


# ---------------------------------------------------------------- the ladder


def test_failed_allocation_on_turbo_moves_to_small(tmp_path, monkeypatch):
    wx = _fake_whisperx({"large-v3-turbo": MKL})

    out, messages = _run(tmp_path, monkeypatch, wx, available=16 * GIB)

    assert wx.loads == ["large-v3-turbo", "small"]
    assert out["model"] == "small"
    assert out["model_fallback"] is True
    assert out["memory_failures"] == [f"large-v3-turbo: {MKL}"]
    assert out["word_count"] == 3
    assert any("Not enough free memory for the large-v3-turbo" in m and "small" in m for m in messages)


def test_low_free_memory_skips_turbo_before_any_download(tmp_path, monkeypatch):
    """2 GB free: the 1.6 GB download that could only fail to load is never
    started; the user is told why in one line."""
    wx = _fake_whisperx({})

    out, messages = _run(tmp_path, monkeypatch, wx, available=2 * GIB)

    assert wx.loads == ["small"]
    assert wx.batches == [4]
    assert out["model"] == "small"
    assert out["memory_available_gb"] == 2.0
    assert out["model_reason"].startswith("2.0 GB free: using the small speech model")
    assert out["model_reason"] in messages


def test_plenty_of_memory_keeps_turbo_and_its_batch_size(tmp_path, monkeypatch):
    wx = _fake_whisperx({})

    out, messages = _run(tmp_path, monkeypatch, wx, available=12 * GIB)

    assert wx.loads == ["large-v3-turbo"]
    assert wx.batches == [asr_stage.BATCH_SIZE]
    assert out["model"] == "large-v3-turbo"
    assert out["model_fallback"] is False
    assert out["model_reason"] is None
    assert not any("less accurate" in m for m in messages)


def test_unknown_memory_tries_the_whole_ladder_from_the_top(tmp_path, monkeypatch):
    wx = _fake_whisperx({"large-v3-turbo": MKL, "small": "aligned_alloc: failed to allocate memory"})

    out, _ = _run(tmp_path, monkeypatch, wx, available=None)

    assert wx.loads == ["large-v3-turbo", "small", "base"]
    assert out["model"] == "base"
    assert out["memory_available_gb"] is None


def test_out_of_memory_while_transcribing_also_steps_down(tmp_path, monkeypatch):
    """The weights fit but the batch did not (torch's allocator wording)."""
    wx = _fake_whisperx({}, transcribe_errors={"large-v3-turbo": TORCH_OOM})

    out, _ = _run(tmp_path, monkeypatch, wx, available=None)

    assert wx.loads == ["large-v3-turbo", "small"]
    assert out["model"] == "small"


def test_every_model_out_of_memory_is_one_human_line(tmp_path, monkeypatch):
    wx = _fake_whisperx({
        "large-v3-turbo": MKL,
        "small": MKL,
        "base": "std::bad_alloc",
    })

    with pytest.raises(queue.StageError) as info:
        _run(tmp_path, monkeypatch, wx, available=None)

    text = str(info.value)
    assert text.startswith("Not enough free memory to run a speech model (")
    assert "large-v3-turbo: mkl_malloc" in text and "base: std::bad_alloc" in text
    assert "Close other apps" in text
    assert wx.loads == ["large-v3-turbo", "small", "base"]


def test_a_download_failure_never_cascades_to_a_smaller_model(tmp_path, monkeypatch):
    wx = _fake_whisperx({"large-v3-turbo": "An error happened while trying to locate the files on the Hub"})

    with pytest.raises(queue.StageError, match="Could not load the large-v3-turbo speech model: An error happened"):
        _run(tmp_path, monkeypatch, wx, available=16 * GIB)

    assert wx.loads == ["large-v3-turbo"]


def test_forced_model_is_the_only_one_tried(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_ASR_MODEL", "base")
    wx = _fake_whisperx({})

    out, messages = _run(tmp_path, monkeypatch, wx, available=1 * GIB)

    assert wx.loads == ["base"]
    assert out["model"] == "base"
    assert "PUBLIKCLIP_ASR_MODEL=base" in messages


def test_memory_errors_are_recognised_by_wording_not_type():
    assert asr_stage.is_memory_error(RuntimeError(MKL))
    assert asr_stage.is_memory_error(RuntimeError(TORCH_OOM))
    assert asr_stage.is_memory_error(MemoryError())
    assert asr_stage.is_memory_error(OSError(12, "Cannot allocate memory"))
    assert not asr_stage.is_memory_error(RuntimeError("HTTP 404"))
    assert not asr_stage.is_memory_error(FileNotFoundError(2, "No such file or directory"))


# ---------------------------------------------------------------- plan_models


def test_plan_starts_at_the_first_model_that_fits():
    plan, reason = asr_stage.plan_models(int(1.0 * GIB))
    assert [m.name for m in plan] == ["base"]
    assert reason.startswith("1.0 GB free: using the base speech model")

    plan, reason = asr_stage.plan_models(int(0.4 * GIB))
    assert [m.name for m in plan] == ["base"]
    assert reason.startswith("Only 0.4 GB free: trying the base speech model")

    plan, reason = asr_stage.plan_models(int(3.6 * GIB))
    assert [m.name for m in plan] == ["large-v3-turbo", "small", "base"]
    assert reason is None


# ---------------------------------------------------------------- sysinfo


def test_sysinfo_override_env_wins(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_MEM_AVAILABLE_MB", "2048")
    assert sysinfo.available_memory_bytes() == 2 * GIB


def test_sysinfo_macos_counts_free_inactive_and_speculative_pages(monkeypatch):
    sample = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               14156.\n"
        "Pages active:                            403927.\n"
        "Pages inactive:                          397923.\n"
        "Pages speculative:                         5062.\n"
        "Pages wired down:                        183027.\n"
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=sample, stderr="")
    )
    assert sysinfo._macos_available() == (14156 + 397923 + 5062) * 16384


def test_sysinfo_never_raises(monkeypatch):
    monkeypatch.setattr(sysinfo, "_macos_available", lambda: 1 / 0)
    monkeypatch.setattr(sysinfo, "_windows_available", lambda: 1 / 0)
    monkeypatch.setattr(sysinfo, "_linux_available", lambda: 1 / 0)
    assert sysinfo.available_memory_bytes() is None
