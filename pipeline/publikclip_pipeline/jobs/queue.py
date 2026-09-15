"""SQLite-backed job store with per-stage checkpoints.

Design (PLAN.md §3): artifacts on disk are the truth; the DB is bookkeeping.
A stage is complete iff its DB row says done AND its checkpoint JSON exists
with the current schema_version. Kill the process at any point and `resume`
re-runs only the stages whose checkpoints are missing or stale.

Checkpoint files live at <job_dir>/<stage>.json as an envelope:

    {"stage": ..., "schema_version": ..., "created_at": ..., "data": {...}}

Writes are atomic (tmp + rename) so a crash mid-write never leaves a
half-checkpoint that resume would trust.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .. import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    source_type TEXT NOT NULL,          -- 'url' | 'file'
    source TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
    error TEXT,
    settings_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_runs (
    job_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,               -- running|done|failed
    schema_version INTEGER NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    error TEXT,
    PRIMARY KEY (job_id, stage)
);
"""


@dataclass
class Job:
    id: str
    created_at: float
    source_type: str
    source: str
    title: str | None
    status: str
    error: str | None
    settings_json: str

    @property
    def dir(self) -> Path:
        return config.jobs_dir() / self.id


def _connect() -> sqlite3.Connection:
    config.ensure_home()
    conn = sqlite3.connect(config.db_path(), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        created_at=row["created_at"],
        source_type=row["source_type"],
        source=row["source"],
        title=row["title"],
        status=row["status"],
        error=row["error"],
        settings_json=row["settings_json"],
    )


def create_job(source_type: str, source: str, settings_json: str) -> Job:
    if source_type not in ("url", "file"):
        raise ValueError(f"bad source_type {source_type!r}")
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id, created_at, source_type, source, status, settings_json)"
            " VALUES (?, ?, ?, ?, 'pending', ?)",
            (job_id, time.time(), source_type, source, settings_json),
        )
    job = get_job(job_id)
    assert job is not None
    job.dir.mkdir(parents=True, exist_ok=True)
    # Snapshot settings into the job dir so resume never picks up new defaults.
    _atomic_write_json(job.dir / "settings.json", json.loads(settings_json))
    return job


def get_job(job_id: str) -> Job | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(limit: int = 50) -> list[Job]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_job(r) for r in rows]


def set_job_status(job_id: str, status: str, error: str | None = None, title: str | None = None) -> None:
    with _connect() as conn:
        if title is not None:
            conn.execute(
                "UPDATE jobs SET status = ?, error = ?, title = ? WHERE id = ?",
                (status, error, title, job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET status = ?, error = ? WHERE id = ?", (status, error, job_id)
            )


# ---------------------------------------------------------------------------
# Checkpoints


def _atomic_write_json(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    tmp.replace(path)


def checkpoint_path(job: Job, stage: str) -> Path:
    return job.dir / f"{stage}.json"


def write_checkpoint(job: Job, stage: str, schema_version: int, data: dict) -> None:
    envelope = {
        "stage": stage,
        "schema_version": schema_version,
        "created_at": time.time(),
        "data": data,
    }
    _atomic_write_json(checkpoint_path(job, stage), envelope)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO stage_runs (job_id, stage, status, schema_version, started_at, finished_at)"
            " VALUES (?, ?, 'done', ?, ?, ?)"
            " ON CONFLICT(job_id, stage) DO UPDATE SET"
            "   status='done', schema_version=excluded.schema_version,"
            "   finished_at=excluded.finished_at, error=NULL",
            (job.id, stage, schema_version, time.time(), time.time()),
        )


def read_checkpoint(job: Job, stage: str, schema_version: int) -> dict | None:
    """Return the stage's data dict iff a checkpoint with the expected
    schema_version exists and parses. Anything else means 're-run me'."""
    path = checkpoint_path(job, stage)
    if not path.exists():
        return None
    try:
        envelope = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if envelope.get("schema_version") != schema_version:
        return None
    data = envelope.get("data")
    return data if isinstance(data, dict) else None


def _relocate_artifact_paths(value: Any, job_dir: Path) -> Any:
    """Repair checkpoint paths when PUBLIKCLIP_HOME was moved."""
    if isinstance(value, dict):
        return {k: _relocate_artifact_paths(v, job_dir) for k, v in value.items()}
    if isinstance(value, list):
        return [_relocate_artifact_paths(v, job_dir) for v in value]
    if isinstance(value, str):
        path = Path(value)
        if not path.exists() and any("publikclip" in part.lower() for part in path.parts):
            candidate = job_dir / path.name
            if candidate.exists():
                return str(candidate)
    return value


def mark_stage(job_id: str, stage: str, status: str, schema_version: int, error: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO stage_runs (job_id, stage, status, schema_version, started_at, error)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(job_id, stage) DO UPDATE SET"
            "   status=excluded.status, schema_version=excluded.schema_version,"
            "   error=excluded.error,"
            "   started_at=CASE WHEN excluded.status='running' THEN excluded.started_at ELSE stage_runs.started_at END,"
            "   finished_at=CASE WHEN excluded.status='running' THEN NULL ELSE ? END",
            (job_id, stage, status, schema_version, time.time(), error, time.time()),
        )


def stage_statuses(job_id: str) -> dict[str, str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT stage, status FROM stage_runs WHERE job_id = ?", (job_id,)
        ).fetchall()
    return {r["stage"]: r["status"] for r in rows}


# ---------------------------------------------------------------------------
# Stage runner


class StageError(Exception):
    """A stage failed in a way the user can act on. Message is user-facing."""


ProgressFn = Callable[[str, float, str], None]  # (stage, fraction 0..1 or -1, message)


@dataclass
class StageContext:
    job: Job
    settings: "config.Settings"
    progress: ProgressFn

    @property
    def job_dir(self) -> Path:
        return self.job.dir

    def emit(self, fraction: float, message: str, stage: str = "") -> None:
        self.progress(stage, fraction, message)


class Stage:
    """Subclass contract: set `name` + `schema_version`, implement run().

    run() returns a JSON-serializable dict; the runner checkpoints it. A stage
    that also produces file artifacts must verify them in artifacts_ok() so a
    deleted media file invalidates the checkpoint even when the JSON survives.
    """

    name: str = ""
    schema_version: int = 1

    def run(self, ctx: StageContext) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def artifacts_ok(self, ctx: StageContext, data: dict) -> bool:
        return True


def run_stages(job: Job, stages: Iterable[Stage], progress: ProgressFn) -> dict[str, dict]:
    """Run stages in order, skipping fresh checkpoints. Returns stage→data."""
    settings = config.Settings.from_json(json.loads(job.settings_json))
    ctx = StageContext(job=job, settings=settings, progress=progress)
    results: dict[str, dict] = {}
    set_job_status(job.id, "running")
    for stage in stages:
        cached = read_checkpoint(job, stage.name, stage.schema_version)
        if cached is not None:
            cached = _relocate_artifact_paths(cached, job.dir)
        if cached is not None and stage.artifacts_ok(ctx, cached):
            results[stage.name] = cached
            progress(stage.name, 1.0, "cached")
            continue
        mark_stage(job.id, stage.name, "running", stage.schema_version)
        progress(stage.name, -1.0, "starting")
        try:
            data = stage.run(_ctx_for(ctx, stage.name, results))
        except StageError as err:
            mark_stage(job.id, stage.name, "failed", stage.schema_version, str(err))
            set_job_status(job.id, "failed", f"{stage.name}: {err}")
            raise
        except Exception as err:  # noqa: BLE001 - record then re-raise
            mark_stage(job.id, stage.name, "failed", stage.schema_version, repr(err))
            set_job_status(job.id, "failed", f"{stage.name}: {err!r}")
            raise
        write_checkpoint(job, stage.name, stage.schema_version, data)
        results[stage.name] = data
        progress(stage.name, 1.0, "done")
    set_job_status(job.id, "done", None)
    return results


@dataclass
class _StageScopedContext(StageContext):
    stage_name: str = ""
    prior: dict[str, dict] | None = None

    def emit(self, fraction: float, message: str, stage: str = "") -> None:
        self.progress(stage or self.stage_name, fraction, message)


def _ctx_for(ctx: StageContext, stage_name: str, prior: dict[str, dict]) -> _StageScopedContext:
    return _StageScopedContext(
        job=ctx.job,
        settings=ctx.settings,
        progress=ctx.progress,
        stage_name=stage_name,
        prior=prior,
    )
