"""publikclip CLI.

Doubles as the desktop app's sidecar: with --jsonl every progress event and
the final result are emitted as one JSON object per stdout line, so the
Tauri shell just spawns `publikclip --jsonl run <source>` and streams.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import config
from .jobs import queue


def _stages() -> list[queue.Stage]:
    # Grows per milestone: ingest → asr → diarize → events → candidates →
    # score → camera → render. Stage imports are deferred so `publikclip
    # jobs` doesn't pay the torch import tax.
    from .asr.stage import AsrStage
    from .camera.stage import CameraStage
    from .candidates.stage import CandidatesStage
    from .diarize.stage import DiarizeStage
    from .events.stage import EventsStage
    from .ingest.stage import IngestStage
    from .render.stage import RenderStage
    from .scoring.stage import ScoreStage

    return [
        IngestStage(),
        AsrStage(),
        DiarizeStage(),
        EventsStage(),
        CandidatesStage(),
        ScoreStage(),
        CameraStage(),
        RenderStage(),
    ]


def _progress_printer(jsonl: bool):
    def emit(stage: str, fraction: float, message: str) -> None:
        if jsonl:
            print(
                json.dumps(
                    {"event": "progress", "stage": stage, "fraction": fraction, "message": message}
                ),
                flush=True,
            )
        else:
            pct = f"{fraction * 100:5.1f}%" if fraction >= 0 else "  ...."
            print(f"[{stage:<10}] {pct} {message}", file=sys.stderr, flush=True)

    return emit


def _emit_result(jsonl: bool, payload: dict) -> None:
    if jsonl:
        print(json.dumps({"event": "result", **payload}), flush=True)
    else:
        print(json.dumps(payload, indent=2))


def cmd_run(args: argparse.Namespace) -> int:
    source = args.source
    source_type = "url" if source.startswith(("http://", "https://")) else "file"
    settings = config.Settings()
    if args.llm:
        settings.llm_mode = args.llm
    if args.captions:
        settings.caption_preset = args.captions
    if args.camera:
        settings.camera.speaker_change = args.camera
    job = queue.create_job(source_type, source, json.dumps(settings.to_json()))
    return _execute(job, args.jsonl)


def cmd_resume(args: argparse.Namespace) -> int:
    job = queue.get_job(args.job_id)
    if job is None:
        print(f"No job {args.job_id}", file=sys.stderr)
        return 2
    if args.llm or args.captions or args.camera:
        settings = config.Settings.from_json(json.loads(job.settings_json))
        if args.llm:
            settings.llm_mode = args.llm
        if args.captions:
            settings.caption_preset = args.captions
        if args.camera:
            settings.camera.speaker_change = args.camera
        new_json = json.dumps(settings.to_json())
        with queue._connect() as conn:  # noqa: SLF001 — CLI is a queue friend
            conn.execute("UPDATE jobs SET settings_json = ? WHERE id = ?", (new_json, job.id))
        job = queue.get_job(args.job_id)
    return _execute(job, args.jsonl)


def _execute(job: queue.Job, jsonl: bool) -> int:
    emit = _progress_printer(jsonl)
    if jsonl:
        print(json.dumps({"event": "job", "job_id": job.id, "dir": str(job.dir)}), flush=True)
    else:
        print(f"job {job.id} → {job.dir}", file=sys.stderr)
    try:
        results = queue.run_stages(job, _stages(), emit)
    except queue.StageError as err:
        _emit_result(jsonl, {"ok": False, "job_id": job.id, "error": str(err)})
        return 1
    summary = {
        "ok": True,
        "job_id": job.id,
        "stages": list(results.keys()),
        "title": results.get("ingest", {}).get("title"),
        "heatmap_segments": len(results.get("ingest", {}).get("heatmap") or []),
    }
    _emit_result(jsonl, summary)
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    for job in queue.list_jobs():
        stages = queue.stage_statuses(job.id)
        done = sum(1 for s in stages.values() if s == "done")
        print(f"{job.id}  {job.status:<8} {done} stage(s) done  {job.title or job.source}")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    """Per-clip editing verbs. All output is JSON on stdout for the app."""
    from pathlib import Path

    from .edits import render_clip as rc
    from .edits import store, visuals

    job = queue.get_job(args.job_id)
    if job is None:
        print(json.dumps({"ok": False, "error": f"no job {args.job_id}"}))
        return 2
    job_dir = Path(job.dir)

    if args.edit_cmd == "context":
        print(json.dumps({"ok": True, **rc.context_for_clip(job_dir, args.clip)}))
        return 0

    if args.edit_cmd == "suggest-visuals":
        score = json.loads((job_dir / "score.json").read_text())["data"]
        clip = score["clips"][args.clip]
        edit = store.edit_for_clip(job_dir, args.clip, clip)
        # plan against OUTPUT-time words = current bounds without dead-space
        # (suggestions land on the source-bounds timeline the UI shows)
        diarize = json.loads((job_dir / "diarize.json").read_text())["data"]
        words = [
            {"word": w["word"], "start": w["start"] - edit.start, "end": w["end"] - edit.start}
            for seg in diarize["segments"]
            for w in seg.get("words", [])
            if edit.start <= w["start"] < edit.end
        ]
        settings = config.Settings.from_json(json.loads(job.settings_json))
        try:
            suggestions = visuals.suggest(job_dir, words, settings.llm_mode, prefer=args.prefer)
        except Exception as err:  # noqa: BLE001 — surface, don't crash the app
            print(json.dumps({"ok": False, "error": str(err)}))
            return 1
        edits = store.load(job_dir)
        current = edits.get(str(args.clip), edit)
        known = {o.id for o in current.overlays}
        current.overlays.extend(o for o in suggestions if o.id not in known)
        edits[str(args.clip)] = current
        store.save(job_dir, edits)
        print(json.dumps({"ok": True, "edit": current.to_json()}))
        return 0

    if args.edit_cmd == "render-clip":
        emit = _progress_printer(args.jsonl)
        try:
            entry = rc.render_clip_edit(job_dir, args.clip, lambda f, m: emit("render", f, m))
        except Exception as err:  # noqa: BLE001
            _emit_result(args.jsonl, {"ok": False, "error": str(err)})
            return 1
        _emit_result(args.jsonl, {"ok": True, "output": entry})
        return 0
    return 2


def cmd_ig(args: argparse.Namespace) -> int:
    from .insights import calibration, instagram

    if args.ig_cmd == "connect":
        conn = instagram.connect(args.app_id, args.app_secret)
        print(f"Connected as @{conn['username']} (user {conn['user_id']}).")
        return 0

    # App-facing commands: exactly one JSON line on stdout (the shell's
    # ig_tool parses the last JSON line, same contract as edit_tool).
    if args.ig_cmd == "sync":
        summary = calibration.sync()
        print(json.dumps(summary))
        return 0 if summary.get("ok") else 1

    if args.ig_cmd == "overview":
        print(json.dumps(calibration.overview()))
        return 0

    if args.ig_cmd == "link":
        job = queue.get_job(args.job_id)
        if job is None:
            print(json.dumps({"ok": False, "error": f"no job {args.job_id}"}))
            return 2
        score_data = queue.read_checkpoint(job, "score", 1)
        if not score_data:
            print(json.dumps({"ok": False, "error": "job has no score checkpoint"}))
            return 2
        clips = score_data["clips"]
        if not 0 <= args.clip < len(clips):
            print(json.dumps({"ok": False, "error": f"clip index out of range (0..{len(clips) - 1})"}))
            return 2
        calibration.link_clip(
            args.job_id, args.clip, args.media_id, clips[args.clip],
            link_source=args.source,
            config_version=score_data.get("scoring_config_version", 1),
        )
        print(json.dumps({"ok": True, "linked": {"job_id": args.job_id, "clip": args.clip, "media_id": args.media_id}}))
        return 0

    if args.ig_cmd == "unlink":
        removed = calibration.unlink(args.media_id)
        print(json.dumps({"ok": True, "removed": removed}))
        return 0

    if args.ig_cmd == "reject":
        calibration.reject_match(args.media_id, args.job_id, args.clip)
        print(json.dumps({"ok": True}))
        return 0

    # Human/legacy commands.
    conn = instagram.load_connection()
    if args.ig_cmd in ("media", "pull") and conn is None:
        print("Not connected. Run: publikclip ig connect --app-id ... --app-secret ...", file=sys.stderr)
        return 2
    if conn is not None:
        conn = instagram.refresh_if_needed(conn)

    if args.ig_cmd == "media":
        for m in instagram.recent_media(conn):
            if m.get("media_product_type") == "REELS" or m.get("media_type") == "VIDEO":
                caption = (m.get("caption") or "")[:60].replace("\n", " ")
                print(f"{m['id']}  {m.get('timestamp', '')[:10]}  {caption}")
        return 0

    if args.ig_cmd == "pull":
        rows = calibration.tracked()
        if not rows:
            print("No linked clips yet. Post an exported clip, then: publikclip ig link ...")
            return 0
        for row in rows:
            if not row["ig_media_id"]:
                continue
            try:
                metrics = instagram.media_insights(conn, row["ig_media_id"])
            except instagram.IgError as err:
                print(f"{row['ig_media_id']}: {err}", file=sys.stderr)
                continue
            calibration.store_metrics(row["ig_media_id"], metrics)
            views = metrics.get("views")
            watch = metrics.get("ig_reels_avg_watch_time")
            print(
                f"{row['ig_media_id']}  score {row['score']:.0f} → views {views}, "
                f"avg watch {round(watch / 1000, 1) if watch else '?'}s"
            )
        return 0

    if args.ig_cmd == "report":
        print(json.dumps(calibration.report(args.metric), indent=2))
        return 0
    return 2


def cmd_audio(args: argparse.Namespace) -> int:
    """Local music/SFX library — local import + CC-licensed online sources.
    import/remove/tag/fetch are always JSON (edit_tool's convention);
    list/search default to human-readable lines, --json switches them
    over for the app."""
    from dataclasses import asdict

    from .audio_library import library
    from .audio_library.sources import MissingKeyError
    from .audio_library.sources.registry import SOURCES

    if args.audio_cmd == "import":
        items = library.import_paths(args.paths, kind=args.kind)
        print(json.dumps({"ok": True, "items": [i.to_json() for i in items]}))
        return 0

    if args.audio_cmd == "list":
        items = library.list_items(kind=args.kind, query=args.query)
        if args.json:
            print(json.dumps({"ok": True, "items": [i.to_json() for i in items]}))
        else:
            for i in items:
                bpm = f"{i.bpm:.0f}bpm" if i.bpm else "-"
                print(
                    f"{i.id}  {i.kind:<5} {i.duration:6.1f}s {bpm:>7}  "
                    f"{i.licence or 'local':<10} {i.name}  [{', '.join(i.tags)}]"
                )
        return 0

    if args.audio_cmd == "remove":
        removed = library.remove_item(args.item_id)
        print(json.dumps({"ok": removed}))
        return 0 if removed else 2

    if args.audio_cmd == "tag":
        item = library.update_tags(args.item_id, args.tags)
        if item is None:
            print(json.dumps({"ok": False, "error": f"no item {args.item_id}"}))
            return 2
        print(json.dumps({"ok": True, "item": item.to_json()}))
        return 0

    if args.audio_cmd == "search":
        source_names = list(SOURCES) if args.source == "all" else [args.source]
        max_duration = args.max_duration
        # Freesound has no sound-effect facet to filter on server-side — a
        # short default cap keeps --kind sfx results actually sfx-shaped.
        if max_duration is None and args.kind == "sfx":
            max_duration = 20.0
        results = []
        errors = []
        for name in source_names:
            try:
                results.extend(
                    SOURCES[name].search(
                        args.query, kind=args.kind,
                        max_duration=max_duration, allow_attribution=args.allow_attribution,
                    )
                )
            except MissingKeyError as err:
                errors.append(str(err))
        if errors and not results:
            message = "; ".join(errors)
            if args.json:
                print(json.dumps({"ok": False, "error": message}))
            else:
                print(message, file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"ok": True, "results": [asdict(r) for r in results]}))
        else:
            for r in results:
                print(
                    f"{r.source:<9} {r.source_id:<10} {r.duration:6.1f}s  "
                    f"{r.licence:<10} {r.name}  ({r.attribution})"
                )
        return 0

    if args.audio_cmd == "fetch":
        mod = SOURCES.get(args.source)
        if mod is None:
            print(json.dumps({"ok": False, "error": f"unknown source {args.source!r}"}))
            return 2
        try:
            result = mod.get(args.source_id)
        except MissingKeyError as err:
            print(json.dumps({"ok": False, "error": str(err)}))
            return 2
        if result is None:
            print(json.dumps({"ok": False, "error": f"{args.source} {args.source_id} not found, or its licence isn't CC0/CC-BY"}))
            return 2
        item = mod.download(result)
        print(json.dumps({"ok": True, "item": item.to_json()}))
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="publikclip")
    parser.add_argument("--jsonl", action="store_true", help="machine-readable progress on stdout")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="process a YouTube URL or local video file")
    p_run.add_argument("source")
    p_run.add_argument("--llm", choices=["gemini", "ollama"], default=None)
    p_run.add_argument("--captions", default=None, help="caption preset name")
    p_run.add_argument("--camera", choices=["cut", "pan", "locked"], default=None)
    p_run.set_defaults(fn=cmd_run)

    p_resume = sub.add_parser("resume", help="resume a job from its checkpoints")
    p_resume.add_argument("job_id")
    p_resume.add_argument("--llm", choices=["gemini", "ollama"], default=None)
    p_resume.add_argument("--captions", default=None, help="caption preset name")
    p_resume.add_argument("--camera", choices=["cut", "pan", "locked"], default=None)
    p_resume.set_defaults(fn=cmd_resume)

    p_jobs = sub.add_parser("jobs", help="list jobs")
    p_jobs.set_defaults(fn=cmd_jobs)

    p_edit = sub.add_parser("edit", help="per-clip editing (context / visuals / render)")
    edit_sub = p_edit.add_subparsers(dest="edit_cmd", required=True)
    p_ctx = edit_sub.add_parser("context")
    p_ctx.add_argument("job_id")
    p_ctx.add_argument("clip", type=int)
    p_sv = edit_sub.add_parser("suggest-visuals")
    p_sv.add_argument("job_id")
    p_sv.add_argument("clip", type=int)
    p_sv.add_argument("--prefer", choices=["pexels", "gemini"], default="pexels")
    p_rcl = edit_sub.add_parser("render-clip")
    p_rcl.add_argument("job_id")
    p_rcl.add_argument("clip", type=int)
    p_edit.set_defaults(fn=cmd_edit)

    p_ig = sub.add_parser("ig", help="Instagram feedback loop (your own Meta app)")
    ig_sub = p_ig.add_subparsers(dest="ig_cmd", required=True)
    p_connect = ig_sub.add_parser("connect", help="OAuth against your own Meta app")
    p_connect.add_argument("--app-id", required=True)
    p_connect.add_argument("--app-secret", required=True)
    ig_sub.add_parser("sync", help="one sync pass: media + thumbnails + insights ladder + auto-fit (JSON)")
    ig_sub.add_parser("overview", help="everything the Loop screen renders (JSON)")
    ig_sub.add_parser("media", help="list your recent Reels to link against")
    p_link = ig_sub.add_parser("link", help="link a rendered clip to a posted Reel (JSON)")
    p_link.add_argument("job_id")
    p_link.add_argument("clip", type=int)
    p_link.add_argument("media_id")
    p_link.add_argument("--source", default="manual", choices=["manual", "match_confirmed"])
    p_unlink = ig_sub.add_parser("unlink", help="remove a clip↔Reel link (JSON)")
    p_unlink.add_argument("media_id")
    p_reject = ig_sub.add_parser("reject", help="'not this' — never suggest this pair again (JSON)")
    p_reject.add_argument("media_id")
    p_reject.add_argument("job_id")
    p_reject.add_argument("clip", type=int)
    ig_sub.add_parser("pull", help="fetch metrics for every linked clip")
    p_report = ig_sub.add_parser("report", help="score-vs-outcome calibration report")
    p_report.add_argument("--metric", default="views")
    p_ig.set_defaults(fn=cmd_ig)

    p_audio = sub.add_parser("audio", help="local music/SFX library")
    audio_sub = p_audio.add_subparsers(dest="audio_cmd", required=True)

    p_a_import = audio_sub.add_parser("import", help="import local files or folders")
    p_a_import.add_argument("paths", nargs="+")
    p_a_import.add_argument("--kind", choices=["auto", "music", "sfx"], default="auto")

    p_a_list = audio_sub.add_parser("list", help="list library items")
    p_a_list.add_argument("--kind", choices=["music", "sfx"], default=None)
    p_a_list.add_argument("--query", default=None)
    p_a_list.add_argument("--json", action="store_true")

    p_a_remove = audio_sub.add_parser("remove", help="remove a library item")
    p_a_remove.add_argument("item_id")

    p_a_tag = audio_sub.add_parser("tag", help="set an item's tags")
    p_a_tag.add_argument("item_id")
    p_a_tag.add_argument("tags", nargs="+")

    p_a_search = audio_sub.add_parser("search", help="search CC-licensed online sources")
    p_a_search.add_argument("query")
    p_a_search.add_argument("--source", choices=["freesound", "jamendo", "all"], default="all")
    p_a_search.add_argument("--kind", choices=["music", "sfx"], default="music")
    p_a_search.add_argument("--max-duration", type=float, default=None)
    p_a_search.add_argument("--allow-attribution", action="store_true")
    p_a_search.add_argument("--json", action="store_true")

    p_a_fetch = audio_sub.add_parser("fetch", help="download one known online item by id")
    p_a_fetch.add_argument("source", choices=["freesound", "jamendo"])
    p_a_fetch.add_argument("source_id")

    p_audio.set_defaults(fn=cmd_audio)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
