"""Drive ingest -> asr -> diarize through the real CLI resume path in the app's locked uv env.

ORACLE_CANNED_ASR=1 swaps the real asr stage for a canned checkpoint (control run: diarize without speechbrain
having been imported by the asr stage). The verdict is parsed from the sidecar's JSONL, never regex-matched.
"""
from __future__ import annotations
import io, json, os, sys, traceback
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

# the asr stage imports whisperx in the same sidecar process before diarize runs
try:
    import whisperx  # noqa: F401
    print("whisperx imported", file=sys.stderr)
except BaseException:
    traceback.print_exc()

from publikclip_pipeline import cli, config
from publikclip_pipeline.asr.stage import AsrStage
from publikclip_pipeline.diarize.stage import DiarizeStage
from publikclip_pipeline.ingest.stage import IngestStage
from publikclip_pipeline.jobs import queue

canned = os.environ.get("ORACLE_CANNED_ASR") == "1"
cli._stages = lambda: [IngestStage(), AsrStage(), DiarizeStage()]
media = Path(os.environ["ORACLE_FIXTURE_MEDIA"])
job = queue.create_job("file", str(media), json.dumps(config.Settings().to_json()))
ctx = queue.StageContext(job=job, settings=config.Settings(), progress=lambda s, f, m: None)
queue.write_checkpoint(job, "ingest", IngestStage.schema_version, IngestStage().run(ctx))
if canned:
    segs = [{"start": 0.0, "end": 1.2, "text": "test", "words": [{"word": "test", "start": 0.0, "end": 1.2, "score": 0.9}]},
            {"start": 1.2, "end": 1.9, "text": "audio", "words": [{"word": "audio", "start": 1.2, "end": 1.9, "score": 0.9}]}]
    queue.write_checkpoint(job, "asr", AsrStage.schema_version, {"language": "en", "model": "large-v3-turbo", "compute_type": "int8",
        "segments": segs, "word_count": 2, "benchmark": {"audio_sec": 2.0, "transcribe_sec": 0.1, "align_sec": 0.1, "realtime_factor": 20.0}})
out, err = io.StringIO(), io.StringIO()
tb = None
try:
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["--jsonl", "resume", job.id])
except SystemExit as e:
    code = e.code
except BaseException:
    tb = traceback.format_exc(); code = 1

result = None
for line in out.getvalue().splitlines():
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if isinstance(ev, dict) and ev.get("event") == "result":
        result = ev
blob = " ".join([out.getvalue(), err.getvalue(), tb or ""])
if result is not None and result.get("ok") is True:
    verdict = "absent"
elif "Lazy import of LazyModule" in blob:
    verdict = "present"
else:
    verdict = "other"  # includes HF 403 / rate limit / network: the oracle reports exit 2
Path(os.environ["ORACLE_OUT"]).write_text(json.dumps({
    "verdict": verdict, "canned_asr": canned, "exit_code": code, "result": result,
    "stderr_tail": err.getvalue()[-1500:], "tb": tb}), encoding="utf-8")
