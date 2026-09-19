"""Oracle driver for cluster publikclip-pipeline-exit-ingest-ytdlp.

Drives the REAL publikclip_pipeline CLI code path (cli.main -> cmd_run ->
_execute -> queue.run_stages -> IngestStage.run -> ingest.ytdlp.fetch_meta)
against a real (invalid) YouTube URL, so a real yt-dlp subprocess runs and
really fails. The only thing swapped out is cli._stages(): the real function
imports all 8 stages (asr/diarize/.../render), which pulls in torch/whisperx/
speechbrain/etc. This oracle patches it to return only IngestStage(), because
ingest is stage 1 and (per the bug we're checking for) either fails before
any later stage would ever run anyway, or -- if the bug is ABSENT -- returns
cleanly and the CLI's own summary is still built the same way from
`results`. No other function is modified: cmd_run, _execute, run_stages,
IngestStage.run, ytdlp.fetch_meta/_run/_with_self_update_retry all run
unmodified.

Presence (bug there): the process exits non-zero AND stdout's last JSONL
line is a "progress" event (message "Updating yt-dlp..." or "Downloading
video..."), never a final "result" event -- i.e. the same thing the Tauri
shell sees that makes it emit {"event":"exited"} and show the generic
"pipeline exited unexpectedly" banner.

Absence (bug fixed): stdout's last JSONL line is a "result" event (ok:true
or ok:false with a specific, non-generic message) -- i.e. _execute caught
whatever ingest raised and reported it gracefully instead of crashing.
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

url = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=00000not0real"

out = io.StringIO()
err = io.StringIO()
code = None
crash_tb = None
try:
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["--jsonl", "run", url])
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
