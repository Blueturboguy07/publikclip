"""Paths and settings.

Everything lives under PUBLIKCLIP_HOME (default ~/.publikclip):

    ~/.publikclip/
      db.sqlite3          job + stage bookkeeping
      bin/                managed binaries (yt-dlp)
      models/             downloaded model weights
      jobs/<job_id>/      per-job artifacts (media, audio, stage checkpoints)

The desktop app points PUBLIKCLIP_HOME at its own app-data dir; the CLI uses
the default. Artifacts on disk are the source of truth — the DB only records
what should exist so a stage can decide whether to skip itself on resume.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def home_dir() -> Path:
    return Path(os.environ.get("PUBLIKCLIP_HOME", str(Path.home() / ".publikclip")))


def jobs_dir() -> Path:
    return home_dir() / "jobs"


def bin_dir() -> Path:
    return home_dir() / "bin"


def models_dir() -> Path:
    return home_dir() / "models"


def db_path() -> Path:
    return home_dir() / "db.sqlite3"


def ensure_home() -> Path:
    root = home_dir()
    for d in (root, jobs_dir(), bin_dir(), models_dir()):
        d.mkdir(parents=True, exist_ok=True)
    return root


# Hard per-attempt network timeouts (seconds). A blackholed connection must
# never freeze the pipeline — every subprocess/network call takes one of these.
HTTP_TIMEOUT = 60.0
SUBPROCESS_INACTIVITY_TIMEOUT = 120.0  # kill if no output for this long
PROBE_TIMEOUT = 60.0

# Ingest
MAX_HEIGHT = 1080
AUDIO_SR = 16_000  # analysis sample rate; every M1 model consumes this wav


@dataclass
class CameraSettings:
    """User-facing camera preset knobs (locked decision #7: exposed options)."""

    # 'cut' = hard cut on speaker change (default), 'pan' = eased pan between
    # speakers, 'locked' = static crop on the dominant face.
    speaker_change: str = "cut"
    pan_duration_s: float = 0.6
    deadzone_frac: float = 0.05  # ignore drift below this fraction of width
    punch_in: bool = True
    punch_in_sensitivity: float = 1.0  # scales event/energy trigger thresholds
    zoom_lock_per_scene: bool = True


@dataclass
class Settings:
    """Per-job settings snapshot. Serialized into the job dir at creation so a
    resumed job never silently picks up changed defaults."""

    camera: CameraSettings = field(default_factory=CameraSettings)
    lufs_target: float = -14.0  # decision #8: configurable per destination
    true_peak_db: float = -1.0
    llm_mode: str = "auto"  # 'auto' (bedrock→gateway→openrouter) | 'bedrock' | 'gateway' | 'openrouter' | 'ollama'
    caption_preset: str = "classic"
    # jrgillick laughter specialist: 10 ms precision but ~300k CPU forward
    # passes on an hour-plus source. OFF by default — PANNs' AudioSet
    # laughter classes cover the bus at 320 ms resolution for a fraction of
    # the compute; flip on for the two-detector agreement boost.
    laughter_specialist: bool = False

    def to_json(self) -> dict:
        return {
            "camera": self.camera.__dict__.copy(),
            "lufs_target": self.lufs_target,
            "true_peak_db": self.true_peak_db,
            "llm_mode": self.llm_mode,
            "caption_preset": self.caption_preset,
            "laughter_specialist": self.laughter_specialist,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Settings":
        cam = CameraSettings(**data.get("camera", {}))
        return cls(
            camera=cam,
            lufs_target=data.get("lufs_target", -14.0),
            true_peak_db=data.get("true_peak_db", -1.0),
            llm_mode=data.get("llm_mode", "auto"),
            caption_preset=data.get("caption_preset", "classic"),
            laughter_specialist=data.get("laughter_specialist", False),
        )
