"""Oracle driver for cluster publikclip-pipeline-exit-ingest-audio-extract.

Drives the REAL publikclip_pipeline CLI code path (cli.main -> cmd_run ->
_execute -> queue.run_stages -> IngestStage.run -> normalize.probe() ->
normalize.extract_analysis_audio()) against a real LOCAL FILE job (no
network needed — matches the "download/probe already succeeded" framing
in the cluster: for a file job there is no download step at all, and probe
genuinely succeeds), so a real ffmpeg subprocess actually runs the audio
extraction and really fails.

The input fixture (fixtures/ingest-audio-extract-fail.mp4) is a normal
short H.264+AAC, constant-frame-rate mp4 with every AUDIO SAMPLE BYTE
overwritten in place (structure, box offsets, and file length all
untouched — see fixtures/build_fixture_corrupt_audio.py). Consequences,
verified empirically (see log.md):
  - `ffprobe -show_format -show_streams` (exactly what normalize.probe()
    runs) still reports a normal video stream AND a normal audio stream
    (codec_type is read from the container's track handler, not from
    decoding sample data) with avg_frame_rate == r_frame_rate (so
    info.vfr is False and normalize_to_cfr() is never called) and
    has_audio True — probe genuinely succeeds, exactly like the
    reporter's screenshot showing probe/download already done.
  - `ffmpeg -vn -ac 1 -ar 16000 -c:a pcm_s16le` (exactly what
    normalize.extract_analysis_audio() runs) genuinely fails: every AAC
    frame fails to decode ("Decode error rate 1 exceeds maximum
    0.666667"), no audio is produced, and the process exits nonzero
    ("Output file is empty, nothing was encoded" / "Conversion failed!").
    _run() (normalize.py) turns that nonzero exit into a real, raised
    FfmpegError.

The only thing swapped out from the real code is cli._stages(): the real
function imports all 8 stages (asr/diarize/.../render), which pulls in
torch/whisperx/speechbrain/opencv/etc. This oracle patches it to return
only IngestStage(), exactly as the sibling
publikclip-pipeline-exit-ingest-ytdlp oracle does, and for the same
reason: ingest is stage 1 and this repro never gets past it either way
(FfmpegError from extract_analysis_audio() is not a queue.StageError, so
per ingest/stage.py it propagates unwrapped straight out of
IngestStage.run(); per jobs/queue.py's run_stages() it is recorded via
mark_stage/set_job_status and then RE-RAISED; per cli.py's _execute() only
`except queue.StageError` is caught, so it propagates all the way out of
cli.main() uncaught). No other function is modified: cmd_run, _execute,
run_stages, IngestStage.run, normalize.probe/extract_analysis_audio all
run unmodified.

Presence (bug there): the process exits non-zero AND stdout's last JSONL
line is a "progress" event with message "Extracting analysis audio…"
(never a final "result" event) -- i.e. the same thing the Tauri shell
sees (stderr redirected to Stdio::null() in app/src-tauri/src/main.rs)
that makes it emit {"event":"exited"} and the frontend
(app/src/App.tsx) show the generic "pipeline exited unexpectedly. Resume
the job to continue from its last checkpoint." banner.

Absence (bug fixed): stdout's last JSONL line is a "result" event (ok:true
or ok:false with a specific, non-generic message) -- i.e. _execute (or
something inside it) caught whatever ingest raised and reported it
gracefully instead of crashing.
"""

from __future__ import annotations

import io
import json
import os
import sys
import traceback
from contextlib import redirect_stdout, redirect_stderr

pipeline_dir = os.environ.get("PUBLIKCLIP_PIPELINE_DIR") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, pipeline_dir)

from publikclip_pipeline import cli  # noqa: E402
from publikclip_pipeline.ingest.stage import IngestStage  # noqa: E402

# The one intentional patch: avoid importing asr/diarize/candidates/score/
# camera/render (torch, whisperx, speechbrain, opencv, ...) since ingest is
# stage 1 and this repro never gets past it either way.
cli._stages = lambda: [IngestStage()]

fixture = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "ingest-audio-extract-fail.mp4"
)
fixture = os.path.abspath(fixture)
if not os.path.exists(fixture):
    print(json.dumps({"exit_code": 2, "stdout": "", "stderr": f"fixture not found: {fixture}", "uncaught_traceback": None}))
    sys.exit(0)  # let oracle.sh's JSON parser report ORACLE COULD NOT RUN cleanly

out = io.StringIO()
err = io.StringIO()
code = None
crash_tb = None
try:
    with redirect_stdout(out), redirect_stderr(err):
        # NOT a URL -> cli.py's cmd_run classifies this as source_type="file",
        # taking the exact branch IngestStage.run() uses for local files.
        code = cli.main(["--jsonl", "run", fixture])
except SystemExit as e:
    code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
except BaseException:  # the exact failure mode under test: an uncaught exception
    crash_tb = traceback.format_exc()
    code = 1

result = {
    "exit_code": code,
    "stdout": out.getvalue(),
    "stderr": err.getvalue(),
    "uncaught_traceback": crash_tb,
}
print(json.dumps(result))
