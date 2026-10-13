"""The Instagram sync store — the loop that makes the score provable.

Four locked decisions (SYNC-DESIGN.md, 2026-08-11) shape this module:

#10 Linking: manually-posted Reels sync into an unlinked tray with
    auto-suggested matches (duration + timing + caption); the user confirms.
    (Auto-publish stays deferred: Meta ingests publish media by public URL,
    which a local-first app doesn't have.)
#11 Outcome metric: raw `views` is what the analytics screen compares the
    predicted score against. The FIT, however, only ever uses within-account
    ranking of views (pairwise accuracy / Spearman ρ, arXiv 2512.21402
    protocol) — magnitudes are account-size noise, ranks are not.
#12 Cadence: opportunistic in-app syncs append age-stamped snapshots on a
    6h/24h/48h/72h/7d/14d/30d ladder; calibration uses the snapshot nearest
    48 h (≥36 h) and records its real age. Gaps are shown, never hidden.
#13 Calibration: once ≥20 linked outcomes exist, the four cross-validation
    constants are re-fitted automatically — loudly: versioned config, per-
    score provenance, user-visible changelog, never a silent rescore.

Everything is local; nothing is shared.
"""

from __future__ import annotations

import json
import math
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .. import config
from ..jobs import queue

SCHEMA = """
CREATE TABLE IF NOT EXISTS published_clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    clip_index INTEGER NOT NULL,
    ig_media_id TEXT UNIQUE,
    linked_at REAL NOT NULL,
    score REAL NOT NULL,
    platform TEXT,
    provenance_json TEXT NOT NULL,     -- full scored-clip entry (t1_raw included)
    metrics_json TEXT,                 -- latest pulled insights (denormalized)
    metrics_fetched_at REAL
);
CREATE TABLE IF NOT EXISTS ig_media (
    media_id TEXT PRIMARY KEY,
    caption TEXT,
    permalink TEXT,
    media_type TEXT,
    posted_at REAL,
    duration_s REAL,
    copyright_flagged INTEGER DEFAULT 0,
    thumb_path TEXT,
    deleted INTEGER DEFAULT 0,
    first_seen REAL,
    raw_json TEXT
);
CREATE TABLE IF NOT EXISTS insight_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id TEXT NOT NULL,
    pulled_at REAL NOT NULL,
    age_hours REAL,
    metrics_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_media ON insight_snapshots(media_id);
CREATE TABLE IF NOT EXISTS match_rejections (
    media_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    clip_index INTEGER NOT NULL,
    rejected_at REAL NOT NULL,
    PRIMARY KEY (media_id, job_id, clip_index)
);
CREATE TABLE IF NOT EXISTS loop_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added after the first shipped schema — applied best-effort on open.
_MIGRATIONS = [
    ("published_clips", "link_source", "TEXT DEFAULT 'manual'"),
    ("published_clips", "reels_score", "REAL"),
    ("published_clips", "config_version", "INTEGER"),
]

MIN_PAIRS_FOR_REPORT = 8
FIT_MIN_OUTCOMES = 20          # decision #13 threshold
FIT_MIN_NEW = 5                # refit at most once per 5 new outcomes
QUALIFY_MIN_AGE_H = 36.0       # snapshot must be near-stable (48 h ± window)
QUALIFY_TARGET_AGE_H = 48.0
RUNGS_H = (6.0, 24.0, 48.0, 72.0, 168.0, 336.0, 720.0)  # snapshot ladder
SUGGEST_THRESHOLD = 0.45
MATCH_WINDOW_DAYS = 14.0
THUMB_BACKFILL_PER_SYNC = 15
DURATION_PROBES_PER_SYNC = 10


def _connect() -> sqlite3.Connection:
    config.ensure_home()
    conn = sqlite3.connect(config.db_path(), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    have = {
        (table, row["name"])
        for table, _, _ in _MIGRATIONS
        for row in conn.execute(f"PRAGMA table_info({table})")
    }
    for table, column, decl in _MIGRATIONS:
        if (table, column) not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return conn


def _parse_ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        try:
            return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except ValueError:
            return None


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM loop_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO loop_meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# --- The local clip library --------------------------------------------------


def rendered_clips() -> list[dict]:
    """Every rendered clip on disk, with the provenance the loop needs.
    Artifacts are the truth (config.py doctrine): this scans jobs/ directly."""
    out: list[dict] = []
    jobs_root = config.jobs_dir()
    if not jobs_root.exists():
        return out
    for job_dir in sorted(jobs_root.iterdir(), reverse=True):
        render_path = job_dir / "render.json"
        score_path = job_dir / "score.json"
        if not render_path.exists() or not score_path.exists():
            continue
        try:
            render = queue.read_json_compat(render_path)["data"]
            score = queue.read_json_compat(score_path)["data"]
        except (json.JSONDecodeError, KeyError, OSError):
            continue
        clips = score.get("clips", [])
        config_version = score.get("scoring_config_version", 1)
        for output in render.get("outputs", []):
            idx = output.get("clip")
            if idx is None or not 0 <= idx < len(clips):
                continue
            entry = clips[idx]
            clip_file = Path(output.get("path", ""))
            rendered_at = (
                clip_file.stat().st_mtime if clip_file.exists() else render_path.stat().st_mtime
            )
            out.append(
                {
                    "job_id": job_dir.name,
                    "clip_index": idx,
                    "path": str(clip_file),
                    "exists": clip_file.exists(),
                    "duration": output.get("duration"),
                    "rendered_at": rendered_at,
                    "summary": entry.get("summary", ""),
                    "score": entry.get("score"),
                    "reels_score": (entry.get("platform_scores") or {}).get("reels"),
                    "config_version": config_version,
                    "entry": entry,
                }
            )
    return out


def clip_thumb(job_id: str, clip_index: int, clip_path: str | None = None) -> str | None:
    """The clip's local thumbnail — its hook frame (~1 s in; the hook IS the
    opening seconds). Extracted lazily once per clip, cached in the job dir,
    so every clip has a thumbnail from birth, published or not."""
    clips_dir = config.jobs_dir() / job_id / "clips"
    dest = clips_dir / f"clip_{clip_index:02d}_thumb.jpg"
    if dest.exists():
        return str(dest)
    src = Path(clip_path) if clip_path else clips_dir / f"clip_{clip_index:02d}.mp4"
    if not src.exists():
        return None
    from ..render import ffmpeg_bin

    try:
        subprocess.run(
            [
                ffmpeg_bin.ffmpeg(), "-y", "-v", "error", "-ss", "1.0",
                "-i", str(src), "-frames:v", "1", "-vf", "scale=272:-2",
                "-q:v", "4", str(dest),
            ],
            capture_output=True,
            timeout=config.PROBE_TIMEOUT,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return str(dest) if dest.exists() else None


# --- Links -------------------------------------------------------------------


def link_clip(
    job_id: str,
    clip_index: int,
    ig_media_id: str,
    clip: dict,
    *,
    link_source: str = "manual",
    config_version: int | None = None,
) -> None:
    """Link a scored clip to a posted Reel. The FULL clip entry is stored so
    the fit can replay its scoring math under candidate constants."""
    provenance = {k: v for k, v in clip.items() if k != "transcript"}
    reels_score = (clip.get("platform_scores") or {}).get("reels")
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO published_clips"
            " (job_id, clip_index, ig_media_id, linked_at, score, platform,"
            "  provenance_json, link_source, reels_score, config_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id, clip_index, ig_media_id, time.time(),
                float(clip.get("score", 0.0)), clip.get("best_platform"),
                json.dumps(provenance), link_source, reels_score, config_version,
            ),
        )


def unlink(ig_media_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM published_clips WHERE ig_media_id = ?", (ig_media_id,))
        return cur.rowcount > 0


def reject_match(ig_media_id: str, job_id: str, clip_index: int) -> None:
    """'Not this' — the pair is never suggested again."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO match_rejections (media_id, job_id, clip_index, rejected_at)"
            " VALUES (?, ?, ?, ?)",
            (ig_media_id, job_id, clip_index, time.time()),
        )


def tracked() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM published_clips ORDER BY linked_at DESC").fetchall()
    return [dict(r) for r in rows]


# --- Media + snapshots -------------------------------------------------------


def upsert_media(media: dict, thumb_path: str | None, duration_s: float | None) -> None:
    posted_at = _parse_ts(media.get("timestamp"))
    with _connect() as conn:
        existing = conn.execute(
            "SELECT thumb_path, duration_s FROM ig_media WHERE media_id = ?", (media["id"],)
        ).fetchone()
        conn.execute(
            "INSERT INTO ig_media (media_id, caption, permalink, media_type, posted_at,"
            " duration_s, copyright_flagged, thumb_path, first_seen, raw_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(media_id) DO UPDATE SET"
            "  caption = excluded.caption, permalink = excluded.permalink,"
            "  media_type = excluded.media_type, posted_at = excluded.posted_at,"
            "  duration_s = COALESCE(excluded.duration_s, ig_media.duration_s),"
            "  copyright_flagged = excluded.copyright_flagged,"
            "  thumb_path = COALESCE(excluded.thumb_path, ig_media.thumb_path),"
            "  raw_json = excluded.raw_json, deleted = 0",
            (
                media["id"], media.get("caption"), media.get("permalink"),
                media.get("media_type"), posted_at,
                duration_s if duration_s is not None else (existing["duration_s"] if existing else None),
                0 if media.get("media_url") else 1,  # omitted media_url ⇒ copyright-flagged
                thumb_path or (existing["thumb_path"] if existing else None),
                time.time(), json.dumps(media),
            ),
        )


def known_media_ids() -> set[str]:
    with _connect() as conn:
        return {r["media_id"] for r in conn.execute("SELECT media_id FROM ig_media")}


def snapshots_for(media_id: str) -> list[dict]:
    with _connect() as conn:
        posted = conn.execute(
            "SELECT posted_at FROM ig_media WHERE media_id = ?", (media_id,)
        ).fetchone()
        rows = conn.execute(
            "SELECT pulled_at, age_hours, metrics_json FROM insight_snapshots"
            " WHERE media_id = ? ORDER BY pulled_at ASC",
            (media_id,),
        ).fetchall()
    posted_at = posted["posted_at"] if posted else None
    out = []
    for r in rows:
        # Recompute age from posted_at when known — snapshots taken before a
        # media row was backfilled carry NULL and heal here.
        age = (
            round((r["pulled_at"] - posted_at) / 3600.0, 2)
            if posted_at else r["age_hours"]
        )
        out.append(
            {"pulled_at": r["pulled_at"], "age_hours": age,
             "metrics": json.loads(r["metrics_json"])}
        )
    return out


def insights_due(age_h: float | None, snapshot_ages: list[float]) -> bool:
    """Snapshot ladder: pull when the most recent passed rung has no snapshot
    at or past it. Opportunistic syncing captures whatever rungs the app is
    open for; each media converges to ≤1 snapshot per rung and goes quiet
    after the 30-day rung is covered."""
    if age_h is None:
        return not snapshot_ages  # unknown posting time: take one snapshot
    passed = [r for r in RUNGS_H if r <= age_h]
    if not passed:
        return False
    last_rung = passed[-1]
    return not any(a is not None and a >= last_rung for a in snapshot_ages)


def store_metrics(ig_media_id: str, metrics: dict) -> None:
    """Append an age-stamped snapshot (append-only history) and refresh the
    denormalized latest-metrics column the table has always had."""
    now = time.time()
    with _connect() as conn:
        row = conn.execute(
            "SELECT posted_at FROM ig_media WHERE media_id = ?", (ig_media_id,)
        ).fetchone()
        age_h = (
            round((now - row["posted_at"]) / 3600.0, 2)
            if row and row["posted_at"] else None
        )
        conn.execute(
            "INSERT INTO insight_snapshots (media_id, pulled_at, age_hours, metrics_json)"
            " VALUES (?, ?, ?, ?)",
            (ig_media_id, now, age_h, json.dumps(metrics)),
        )
        conn.execute(
            "UPDATE published_clips SET metrics_json = ?, metrics_fetched_at = ?"
            " WHERE ig_media_id = ?",
            (json.dumps(metrics), now, ig_media_id),
        )


def tombstone_media(media_id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE ig_media SET deleted = 1 WHERE media_id = ?", (media_id,))


# --- Matching (decision #10) -------------------------------------------------


def _caption_tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return {
        tok for tok in "".join(c.lower() if c.isalnum() else " " for c in text).split()
        if len(tok) > 3
    }


def match_score(media_row: dict, clip: dict) -> float:
    """0–1 confidence that this Reel is this clip. Duration is the strongest
    signal but vanishes on copyright-flagged media (no media_url ⇒ no probe) —
    weights renormalize over what's available instead of penalizing it."""
    posted_at = media_row.get("posted_at")
    rendered_at = clip.get("rendered_at")
    if posted_at is None or rendered_at is None:
        return 0.0
    dt_days = (posted_at - rendered_at) / 86400.0
    if dt_days < -0.05 or dt_days > MATCH_WINDOW_DAYS:
        return 0.0  # posted before the clip existed, or far outside the window
    timing = 1.0 - max(0.0, dt_days) / MATCH_WINDOW_DAYS

    parts: list[tuple[float, float]] = [(0.30, timing)]  # (weight, score)
    media_dur, clip_dur = media_row.get("duration_s"), clip.get("duration")
    if media_dur is not None and clip_dur is not None:
        parts.append((0.45, max(0.0, 1.0 - abs(float(media_dur) - float(clip_dur)) / 3.0)))
    caption = _caption_tokens(media_row.get("caption"))
    if caption:
        summary = _caption_tokens(clip.get("summary"))
        overlap = len(caption & summary) / len(caption) if summary else 0.0
        parts.append((0.25, overlap))
    total_w = sum(w for w, _ in parts)
    return round(sum(w * s for w, s in parts) / total_w, 3)


def suggestions() -> list[dict]:
    """Top match suggestion per unlinked Reel, above threshold, minus
    anything the user already rejected."""
    with _connect() as conn:
        linked_media = {
            r["ig_media_id"]
            for r in conn.execute(
                "SELECT ig_media_id FROM published_clips WHERE ig_media_id IS NOT NULL"
            )
        }
        linked_clips = {
            (r["job_id"], r["clip_index"])
            for r in conn.execute("SELECT job_id, clip_index FROM published_clips")
        }
        rejected = {
            (r["media_id"], r["job_id"], r["clip_index"])
            for r in conn.execute("SELECT media_id, job_id, clip_index FROM match_rejections")
        }
        media_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM ig_media WHERE deleted = 0 AND media_type = 'VIDEO'"
            )
        ]

    clips = [
        c for c in rendered_clips() if (c["job_id"], c["clip_index"]) not in linked_clips
    ]
    out: list[dict] = []
    for media in media_rows:
        if media["media_id"] in linked_media:
            continue
        best: dict | None = None
        for clip in clips:
            if (media["media_id"], clip["job_id"], clip["clip_index"]) in rejected:
                continue
            confidence = match_score(media, clip)
            if confidence >= SUGGEST_THRESHOLD and (best is None or confidence > best["confidence"]):
                best = {
                    "media_id": media["media_id"],
                    "job_id": clip["job_id"],
                    "clip_index": clip["clip_index"],
                    "confidence": confidence,
                    "clip_summary": clip["summary"],
                    "clip_duration": clip["duration"],
                    "clip_thumb": clip_thumb(clip["job_id"], clip["clip_index"], clip["path"]),
                    "clip_reels_score": clip["reels_score"],
                }
        if best:
            out.append(best)
    out.sort(key=lambda s: s["confidence"], reverse=True)
    return out


# --- Outcomes + fit (decisions #11/#13) --------------------------------------


def qualifying_outcomes() -> list[dict]:
    """One (clip, views) pair per linked Reel with a near-stable snapshot:
    the snapshot ≥36 h old nearest to the 48 h target, its real age recorded.
    `can_recompute` marks rows whose provenance carries t1_raw — only those
    enter the fit; older rows still show on the dashboard."""
    out: list[dict] = []
    for row in tracked():
        if not row.get("ig_media_id"):
            continue
        snaps = [
            s for s in snapshots_for(row["ig_media_id"])
            if s["age_hours"] is not None and s["age_hours"] >= QUALIFY_MIN_AGE_H
            and s["metrics"].get("views") is not None
        ]
        if not snaps:
            continue
        best = min(snaps, key=lambda s: abs(s["age_hours"] - QUALIFY_TARGET_AGE_H))
        provenance = json.loads(row["provenance_json"])
        out.append(
            {
                "job_id": row["job_id"],
                "clip_index": row["clip_index"],
                "media_id": row["ig_media_id"],
                "views": float(best["metrics"]["views"]),
                "snapshot_age_h": best["age_hours"],
                "reels_score": row.get("reels_score") or row.get("score"),
                "provenance": provenance,
                "can_recompute": bool(provenance.get("t1_raw")),
            }
        )
    return out


def _pairwise_acc(pred: list[float], outcome: list[float]) -> float | None:
    correct = total = 0
    n = len(pred)
    for i in range(n):
        for j in range(i + 1, n):
            if pred[i] == pred[j] or outcome[i] == outcome[j]:
                continue
            total += 1
            if (pred[i] > pred[j]) == (outcome[i] > outcome[j]):
                correct += 1
    return correct / total if total else None


def _grid(lo: float, hi: float, steps: int = 6) -> list[float]:
    return [round(lo + (hi - lo) * i / (steps - 1), 4) for i in range(steps)]


def _recomputed_scores(outcomes: list[dict], constants: dict) -> list[float]:
    from ..scoring import rubric

    scores = []
    for o in outcomes:
        platform = rubric.recompute_platform_scores(o["provenance"], constants)
        scores.append(platform["reels"] if platform else float(o["reels_score"] or 0.0))
    return scores


def fit_constants(*, force: bool = False) -> dict:
    """Auto-fit the four cross-validation constants against real outcomes.

    Objective: pairwise ranking accuracy of the recomputed reels score vs
    views (ranks only — decision #11's raw-views display stays untouched).
    Bounded grid search inside scoring.constants.CLAMPS; a candidate is
    applied only if the refit procedure beats the active constants on held-out
    folds (4-fold), otherwise it's discarded and logged. Applying appends a
    scoring_config version — the changelog the UI shows (decision #13)."""
    from itertools import product

    from ..scoring import constants as constants_mod

    outcomes = [o for o in qualifying_outcomes() if o["can_recompute"]]
    n = len(outcomes)
    active = constants_mod.active()
    result = {"applied": False, "n": n, "threshold": FIT_MIN_OUTCOMES}
    if n < FIT_MIN_OUTCOMES:
        result["reason"] = f"collecting — calibration applies automatically at {FIT_MIN_OUTCOMES}"
        return result
    fitted_from = active.get("fitted_from_n") or 0
    if not force and n - fitted_from < FIT_MIN_NEW and fitted_from:
        result["reason"] = f"waiting for {FIT_MIN_NEW} new outcomes since the last fit"
        return result

    views = [o["views"] for o in outcomes]
    keys = list(constants_mod.CLAMPS)
    axes = [_grid(*constants_mod.CLAMPS[k]) for k in keys]
    combos = [dict(zip(keys, vals)) for vals in product(*axes)]

    score_cache: dict[tuple, list[float]] = {}

    def scores_for(constants: dict) -> list[float]:
        key = tuple(constants[k] for k in keys)
        if key not in score_cache:
            score_cache[key] = _recomputed_scores(outcomes, constants)
        return score_cache[key]

    def acc_on(constants: dict, idx: list[int]) -> float:
        s = scores_for(constants)
        a = _pairwise_acc([s[i] for i in idx], [views[i] for i in idx])
        return a if a is not None else 0.5

    # 4-fold validation OF THE PROCEDURE: fit on train, compare against the
    # active constants on the held-out fold.
    order = sorted(range(n), key=lambda i: outcomes[i]["media_id"])
    folds = [order[f::4] for f in range(4)]
    held_new, held_cur = [], []
    for f, test in enumerate(folds):
        if not test:
            continue
        train = [i for g, fold in enumerate(folds) if g != f for i in fold]
        best_train = max(combos, key=lambda c: acc_on(c, train))
        held_new.append(acc_on(best_train, test))
        held_cur.append(acc_on(active["constants"], test))
    mean_new = sum(held_new) / len(held_new) if held_new else 0.0
    mean_cur = sum(held_cur) / len(held_cur) if held_cur else 0.0
    result["held_out_acc_refit"] = round(mean_new, 3)
    result["held_out_acc_active"] = round(mean_cur, 3)
    if mean_new <= mean_cur + 1e-9:
        result["reason"] = (
            "refit did not beat the active constants on held-out folds — kept as-is"
        )
        return result

    all_idx = list(range(n))
    best = max(combos, key=lambda c: acc_on(c, all_idx))
    best_scores = scores_for(best)
    pairwise = _pairwise_acc(best_scores, views)
    rho = None
    try:
        from scipy.stats import spearmanr

        rho = float(spearmanr(best_scores, views)[0])
    except Exception:  # noqa: BLE001 — ρ is reporting, not gating
        pass
    version = constants_mod.save(
        best,
        fitted_from_n=n,
        spearman_rho=round(rho, 3) if rho is not None else None,
        pairwise_acc=round(pairwise, 3) if pairwise is not None else None,
        note=f"auto-fit from {n} Instagram outcomes",
    )
    result.update(
        {
            "applied": True,
            "version": version,
            "constants": best,
            "previous": active["constants"],
            "pairwise_acc": round(pairwise, 3) if pairwise is not None else None,
            "spearman_rho": round(rho, 3) if rho is not None else None,
        }
    )
    return result


# --- Sync orchestrator (decision #12) ----------------------------------------


def sync() -> dict:
    """One opportunistic sync pass: token → new media + thumbnails →
    duration probes → insights ladder → tombstones → auto-fit. Returns the
    summary the UI shows. Never raises for per-media trouble; errors ride
    along in the summary."""
    from . import instagram

    connection = instagram.load_connection()
    if connection is None:
        return {"ok": False, "error": "not_connected"}
    try:
        connection = instagram.refresh_if_needed(connection)
    except Exception as err:  # noqa: BLE001 — refresh failing ≠ token dead yet
        return {"ok": False, "error": f"token refresh failed: {err}"}

    summary: dict = {
        "ok": True, "username": connection.get("username"),
        "new_media": 0, "thumbs_cached": 0, "durations_probed": 0,
        "snapshots_pulled": 0, "tombstoned": 0, "errors": [],
    }

    # 1. New media, newest-first, stop at anything already seen.
    try:
        media_list = instagram.recent_media(connection, stop_at=known_media_ids())
    except instagram.IgError as err:
        return {"ok": False, "error": str(err)}
    probes_left = DURATION_PROBES_PER_SYNC
    for media in media_list:
        thumb = duration = None
        if media.get("media_type") == "VIDEO":
            thumb = instagram.cache_thumbnail(connection, media)
            if thumb:
                summary["thumbs_cached"] += 1
            if media.get("media_url") and probes_left > 0:
                probes_left -= 1
                duration = instagram.probe_duration(media["media_url"])
                if duration:
                    summary["durations_probed"] += 1
        upsert_media(media, thumb, duration)
        summary["new_media"] += 1

    # 2. Backfill: linked Reels whose media row/thumbnail predates this
    #    schema (or whose signed URL died between list and download).
    with _connect() as conn:
        missing_rows = conn.execute(
            "SELECT pc.ig_media_id FROM published_clips pc"
            " LEFT JOIN ig_media m ON m.media_id = pc.ig_media_id"
            " WHERE pc.ig_media_id IS NOT NULL"
            "   AND (m.media_id IS NULL OR (m.thumb_path IS NULL AND m.deleted = 0))"
            " LIMIT ?",
            (THUMB_BACKFILL_PER_SYNC,),
        ).fetchall()
    for row in missing_rows:
        media_id = row["ig_media_id"]
        try:
            node = instagram.media_node(connection, media_id)
        except instagram.IgError:
            continue
        thumb = instagram.cache_thumbnail(connection, node)
        if thumb:
            summary["thumbs_cached"] += 1
        upsert_media(node, thumb, None)

    # 3. Insights ladder for every linked, non-deleted Reel.
    now = time.time()
    with _connect() as conn:
        linked = conn.execute(
            "SELECT pc.ig_media_id, m.posted_at FROM published_clips pc"
            " JOIN ig_media m ON m.media_id = pc.ig_media_id"
            " WHERE pc.ig_media_id IS NOT NULL AND m.deleted = 0"
        ).fetchall()
    for row in linked:
        media_id = row["ig_media_id"]
        age_h = (now - row["posted_at"]) / 3600.0 if row["posted_at"] else None
        ages = [s["age_hours"] for s in snapshots_for(media_id)]
        if not insights_due(age_h, ages):
            continue
        try:
            metrics = instagram.media_insights(connection, media_id)
        except instagram.IgError as err:
            text = str(err)
            if any(marker in text for marker in ("does not exist", "Unsupported get request", "cannot be loaded")):
                tombstone_media(media_id)
                summary["tombstoned"] += 1
            else:
                summary["errors"].append({"media_id": media_id, "error": text[:200]})
            continue
        store_metrics(media_id, metrics)
        summary["snapshots_pulled"] += 1

    # 4. Auto-fit (decision #13) — cheap no-op below threshold.
    try:
        summary["fit"] = fit_constants()
    except Exception as err:  # noqa: BLE001 — the fit must never kill a sync
        summary["fit"] = {"applied": False, "reason": f"fit error: {err}"}

    with _connect() as conn:
        _meta_set(conn, "last_synced_at", str(now))
        _meta_set(conn, "last_sync_summary", json.dumps(summary))
    return summary


# --- The dashboard payload ---------------------------------------------------


def overview() -> dict:
    """Everything the Loop screen renders, in one call. Works disconnected
    (local links + clip library still show; connected=false tells the UI to
    offer the connect flow)."""
    from . import instagram
    from ..scoring import constants as constants_mod

    connection = instagram.load_connection()
    with _connect() as conn:
        media_by_id = {r["media_id"]: dict(r) for r in conn.execute("SELECT * FROM ig_media")}
        last_synced = _meta_get(conn, "last_synced_at")

    clips = rendered_clips()
    clips_by_key = {(c["job_id"], c["clip_index"]): c for c in clips}

    linked_rows = []
    for row in tracked():
        media = media_by_id.get(row.get("ig_media_id") or "", {})
        snaps = snapshots_for(row["ig_media_id"]) if row.get("ig_media_id") else []
        provenance = json.loads(row["provenance_json"])
        clip = clips_by_key.get((row["job_id"], row["clip_index"]))
        latest = json.loads(row["metrics_json"]) if row.get("metrics_json") else None
        posted_at = media.get("posted_at")
        age_h = (time.time() - posted_at) / 3600.0 if posted_at else None
        linked_rows.append(
            {
                "job_id": row["job_id"],
                "clip_index": row["clip_index"],
                "media_id": row["ig_media_id"],
                "link_source": row.get("link_source") or "manual",
                "linked_at": row["linked_at"],
                "score": row["score"],
                "reels_score": row.get("reels_score") or row["score"],
                "config_version": row.get("config_version") or 1,
                "subscores": provenance.get("subscores"),
                "adjustments": provenance.get("adjustments"),
                "signals_fired": provenance.get("signals_fired"),
                "signals_missing": provenance.get("signals_missing"),
                "summary": provenance.get("summary", ""),
                "clip_duration": (
                    round(provenance["end"] - provenance["start"], 1)
                    if provenance.get("end") is not None and provenance.get("start") is not None
                    else (clip["duration"] if clip else None)
                ),
                "clip_thumb": clip_thumb(
                    row["job_id"], row["clip_index"], clip["path"] if clip else None
                ),
                "ig_thumb": media.get("thumb_path"),
                "permalink": media.get("permalink"),
                "caption": media.get("caption"),
                "posted_at": posted_at,
                "media_deleted": bool(media.get("deleted")),
                "media_age_hours": round(age_h, 1) if age_h is not None else None,
                "settling": age_h is not None and age_h < 48.0,
                "metrics": latest,
                "snapshots": [
                    {"age_hours": s["age_hours"], "views": s["metrics"].get("views")}
                    for s in snaps
                ],
                "snapshot_count": len(snaps),
            }
        )
    linked_rows.sort(key=lambda r: r.get("posted_at") or r["linked_at"], reverse=True)

    linked_media_ids = {r["media_id"] for r in linked_rows if r["media_id"]}
    suggestion_by_media = {s["media_id"]: s for s in suggestions()}
    unlinked = []
    for media_id, media in sorted(
        media_by_id.items(), key=lambda kv: kv[1].get("posted_at") or 0, reverse=True
    ):
        if media_id in linked_media_ids or media.get("deleted") or media.get("media_type") != "VIDEO":
            continue
        unlinked.append(
            {
                "media_id": media_id,
                "thumb": media.get("thumb_path"),
                "permalink": media.get("permalink"),
                "caption": (media.get("caption") or "")[:140],
                "posted_at": media.get("posted_at"),
                "duration_s": media.get("duration_s"),
                "copyright_flagged": bool(media.get("copyright_flagged")),
                "suggestion": suggestion_by_media.get(media_id),
            }
        )

    outcomes = qualifying_outcomes()
    linked_keys = {(r["job_id"], r["clip_index"]) for r in linked_rows}
    clip_library = [
        {
            "job_id": c["job_id"],
            "clip_index": c["clip_index"],
            "summary": c["summary"],
            "duration": c["duration"],
            "reels_score": c["reels_score"],
            "thumb": clip_thumb(c["job_id"], c["clip_index"], c["path"]),
            "linked": (c["job_id"], c["clip_index"]) in linked_keys,
        }
        for c in clips
    ]

    return {
        "connected": connection is not None,
        "username": connection.get("username") if connection else None,
        "last_synced_at": float(last_synced) if last_synced else None,
        "linked": linked_rows,
        "unlinked": unlinked,
        "clip_library": clip_library,
        "report": report(),
        "calibration": {
            "active": constants_mod.active(),
            "history": constants_mod.history(),
            "qualifying_outcomes": len(outcomes),
            "recomputable_outcomes": sum(1 for o in outcomes if o["can_recompute"]),
            "threshold": FIT_MIN_OUTCOMES,
        },
    }


def report(outcome_metric: str = "views") -> dict:
    """Score-vs-outcome agreement over every linked clip with a qualifying
    snapshot. Predicted = the reels-platform score each clip actually shipped
    with (outcomes happen on Reels; the cross-platform max would be noise)."""
    outcomes = qualifying_outcomes()
    pairs = [
        (float(o["reels_score"]), float(o["views"]))
        for o in outcomes
        if o["reels_score"] is not None
    ]
    if outcome_metric != "views":  # legacy CLI flag support
        pairs = []
        for row in tracked():
            if not row.get("metrics_json"):
                continue
            value = json.loads(row["metrics_json"]).get(outcome_metric)
            if value is not None:
                pairs.append((float(row.get("reels_score") or row["score"]), float(value)))
    n = len(pairs)
    if n < MIN_PAIRS_FOR_REPORT:
        return {
            "pairs": n,
            "ready": False,
            "note": f"Need at least {MIN_PAIRS_FOR_REPORT} linked clips with stable metrics "
                    f"(have {n}). Keep posting and syncing.",
        }

    scores = [p[0] for p in pairs]
    outcomes_v = [p[1] for p in pairs]
    pairwise = _pairwise_acc(scores, outcomes_v)
    rho = tau = rho_p = None
    try:
        from scipy.stats import kendalltau, spearmanr

        rho_v, rho_p_v = spearmanr(scores, outcomes_v)
        tau_v, _ = kendalltau(scores, outcomes_v)
        rho, rho_p, tau = float(rho_v), float(rho_p_v), float(tau_v)
    except Exception:  # noqa: BLE001 — stats are reporting, not gating
        pass
    if not (rho is not None and not math.isnan(rho)):
        rho = rho_p = None
    return {
        "pairs": n,
        "ready": True,
        "outcome_metric": outcome_metric,
        "spearman_rho": round(rho, 3) if rho is not None else None,
        "spearman_p": round(rho_p, 4) if rho_p is not None else None,
        "kendall_tau": round(tau, 3) if tau is not None and not math.isnan(tau) else None,
        "pairwise_accuracy": round(pairwise, 3) if pairwise is not None else None,
        "interpretation": (
            "ρ > 0.5: the score genuinely ranks your clips. "
            "ρ ≈ 0: it doesn't (yet) — this table is exactly the evidence "
            "the auto-calibration uses to retune the cross-validation constants."
        ),
    }
