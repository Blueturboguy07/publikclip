"""The ASR stage's audio loading must not depend on PATH.

Sep 28 2026, packaged app on macOS: ingest finished, then TRANSCRIBE died
with `asr: FileNotFoundError(2, 'No such file or directory')`. The sidecar
inherits Finder's PATH (/usr/bin:/bin:/usr/sbin:/sbin); whisperx.load_audio
execs a bare `ffmpeg` on it. Every other stage resolves ffmpeg through
render/ffmpeg_bin and was fine. These tests pin the contract that ASR now
reads the ingest wav directly, decodes anything else with the resolved
ffmpeg by absolute path, and that the resolver publishes its choice to PATH
for third-party code that still shells out by name."""

import os
import shutil
import subprocess
import wave

import numpy as np
import pytest

from publikclip_pipeline.asr import stage as asr_stage
from publikclip_pipeline.render import ffmpeg_bin

FINDER_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _write_wav(path, rate: int, channels: int, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.astype(np.int16).tobytes())


def _tone(rate: int, seconds: float) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)


@pytest.fixture
def fresh_resolver():
    ffmpeg_bin.resolve.cache_clear()
    yield
    ffmpeg_bin.resolve.cache_clear()


def test_ingest_wav_is_read_without_spawning_any_decoder(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", FINDER_PATH)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("a decoder was spawned")
    )
    samples = _tone(asr_stage.SAMPLE_RATE, 1.0)
    wav = tmp_path / "audio16k.wav"
    _write_wav(wav, asr_stage.SAMPLE_RATE, 1, samples)

    audio = asr_stage.load_audio(wav)

    assert audio.dtype == np.float32
    assert audio.shape == (asr_stage.SAMPLE_RATE,)
    # Bit-identical to what whisperx.load_audio produces for this input.
    np.testing.assert_array_equal(audio, samples.astype(np.float32) / 32768.0)


def test_other_layouts_decode_with_the_resolved_ffmpeg(tmp_path, monkeypatch, fresh_resolver):
    monkeypatch.setenv("PATH", FINDER_PATH)
    if not os.path.isabs(ffmpeg_bin.ffmpeg()):
        pytest.skip("no ffmpeg resolvable outside PATH on this machine")
    stereo = np.stack([_tone(44100, 0.5)] * 2, axis=1).reshape(-1)
    wav = tmp_path / "stereo44k.wav"
    _write_wav(wav, 44100, 2, stereo)

    audio = asr_stage.load_audio(wav)

    assert audio.dtype == np.float32
    assert abs(len(audio) - asr_stage.SAMPLE_RATE // 2) <= 64  # resampled to 16 kHz mono


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="whisperx.load_audio needs ffmpeg on PATH")
def test_matches_whisperx_loader_bit_for_bit(tmp_path):
    whisperx = pytest.importorskip("whisperx")
    samples = _tone(asr_stage.SAMPLE_RATE, 0.25)
    wav = tmp_path / "audio16k.wav"
    _write_wav(wav, asr_stage.SAMPLE_RATE, 1, samples)

    np.testing.assert_array_equal(asr_stage.load_audio(wav), whisperx.load_audio(str(wav)))


def test_missing_ffmpeg_is_a_stage_error_not_a_bare_oserror(tmp_path, monkeypatch, fresh_resolver):
    monkeypatch.setenv("PATH", FINDER_PATH)
    monkeypatch.setattr(ffmpeg_bin, "ffmpeg", lambda: str(tmp_path / "nope" / "ffmpeg"))
    wav = tmp_path / "stereo.wav"
    _write_wav(wav, 44100, 2, np.zeros(2000, dtype=np.int16))

    with pytest.raises(asr_stage.StageError, match="ffmpeg"):
        asr_stage.load_audio(wav)


def test_resolver_publishes_its_choice_on_path(monkeypatch, fresh_resolver):
    monkeypatch.setenv("PATH", FINDER_PATH)
    chosen, _ = ffmpeg_bin.resolve()
    if not os.path.isabs(chosen):
        pytest.skip("no ffmpeg resolvable outside PATH on this machine")

    assert os.environ["PATH"].split(os.pathsep)[0] == os.path.dirname(chosen)
    found = shutil.which("ffmpeg")
    assert found is not None and os.path.samefile(found, chosen)
    # Idempotent: resolving again does not stack a second copy.
    ffmpeg_bin.resolve.cache_clear()
    ffmpeg_bin.resolve()
    assert os.environ["PATH"].split(os.pathsep).count(os.path.dirname(chosen)) == 1
