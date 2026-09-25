"""bugfix-lab Windows-CI driver for cluster publikclip-pipeline-exit-speakers-model.

Same mechanism as bugfix-lab/work/publikclip-pipeline-exit-speakers-model/
oracle_drive.py (mac-local oracle) -- this is the Windows-runner copy so the
GitHub Actions workflow can exercise the real, unmodified
diarize/stage.py -> models/registry.py -> diarize/campplus.py torch.load()
path on an actual windows-latest runner, not just locally. See that file's
docstring for the full mechanism writeup; this file only drives the
"poison" case (a cached-but-corrupted CAM++ checkpoint) since the local
oracle already proved the "valid" negative control on Sep 21 2026.

Prints one JSON line to stdout: {"exit_code", "stdout", "stderr", "job_id"}.
"""

from __future__ import annotations

import io
import json
import os
import sys
import traceback
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

pipeline_dir = os.environ.get("PUBLIKCLIP_PIPELINE_DIR") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, pipeline_dir)

from publikclip_pipeline import cli, config  # noqa: E402
from publikclip_pipeline.asr.stage import AsrStage  # noqa: E402
from publikclip_pipeline.diarize.stage import DiarizeStage  # noqa: E402
from publikclip_pipeline.ingest.stage import IngestStage  # noqa: E402
from publikclip_pipeline.jobs import queue  # noqa: E402
from publikclip_pipeline.models import specs  # noqa: E402
from publikclip_pipeline.models.registry import model_path  # noqa: E402

cli._stages = lambda: [IngestStage(), AsrStage(), DiarizeStage()]

FIXTURE_MEDIA = Path(os.environ["ORACLE_FIXTURE_MEDIA"])

job = queue.create_job("file", str(FIXTURE_MEDIA), json.dumps(config.Settings().to_json()))
ingest_ctx = queue.StageContext(job=job, settings=config.Settings(), progress=lambda s, f, m: None)
ingest_data = IngestStage().run(ingest_ctx)
queue.write_checkpoint(job, "ingest", IngestStage.schema_version, ingest_data)

asr_data = {
    "language": "en",
    "model": "large-v3-turbo",
    "compute_type": "int8",
    "segments": [
        {"start": 0.0, "end": 1.2, "text": "test", "words": [
            {"word": "test", "start": 0.0, "end": 1.2, "score": 0.9},
        ]},
        {"start": 1.2, "end": 1.9, "text": "audio", "words": [
            {"word": "audio", "start": 1.2, "end": 1.9, "score": 0.9},
        ]},
    ],
    "word_count": 2,
    "benchmark": {"audio_sec": 2.0, "transcribe_sec": 0.1, "align_sec": 0.1, "realtime_factor": 20.0},
}
queue.write_checkpoint(job, "asr", AsrStage.schema_version, asr_data)

dest = model_path(specs.CAMPPLUS)
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_bytes(b"NOT_A_REAL_CHECKPOINT_TRUNCATED_DOWNLOAD_" * 64)

out = io.StringIO()
err = io.StringIO()
code = None
crash_tb = None
try:
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["--jsonl", "resume", job.id])
except SystemExit as e:
    code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
except BaseException:
    crash_tb = traceback.format_exc()
    code = 1

result = {
    "exit_code": code,
    "stdout": out.getvalue(),
    "stderr": err.getvalue(),
    "uncaught_traceback": crash_tb,
    "job_id": job.id,
}
print(json.dumps(result))
