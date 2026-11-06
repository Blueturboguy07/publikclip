#!/usr/bin/env python3
"""Check the usage counts (publik telemetry v1) of a built publikclip.

usage: publik-telemetry-smoke.py <app executable> <slug> <version> <macos|windows|linux> <result.json>

It starts a local HTTP listener and runs the app three times, about 15 seconds each:
  1. DO_NOT_TRACK=1:  no request may arrive and no telemetry.json may appear;
  2. signed out:      one correct app_opened must arrive, with no Authorization header;
  3. signed in:       app_opened must carry the publik key from secrets.json (a fake key here).
The app must stay up in each run. Nothing goes to publikhq.com: the endpoint and the
publik API base URL both point at the listener. Standard library only.
Exit code 0 only when all three runs pass.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

exe, slug, version, os_name, out_path = sys.argv[1:6]
exe = os.path.abspath(exe)
FAKE_KEY = "pk_live_smoke_0000000000000000"
UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
FIELDS = {"v", "app", "install_id", "event", "app_version", "os", "arch"}
received = []


class Handler(BaseHTTPRequestHandler):
    def _any(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        headers = {name.lower(): value for name, value in self.headers.items()}
        received.append({"method": self.command, "path": self.path, "headers": headers, "body": body, "time": time.time()})
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_GET = do_PUT = do_OPTIONS = do_HEAD = _any

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # port 0 = any free port
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d/api/v1" % server.server_address[1]

if os_name == "windows":
    publik_dir = os.path.join(os.environ["LOCALAPPDATA"], "publik")
elif os_name == "macos":
    publik_dir = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "publik")
else:
    publik_dir = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"), "publik")
id_file = os.path.join(publik_dir, "telemetry.json")


def stop(process):
    if process.poll() is not None:
        return
    if os.name == "nt":  # the web view runs in child processes
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def run(name, seconds, extra_env, signed_in):
    """One launch. Returns what arrived and what was wrong."""
    app_home = tempfile.mkdtemp(prefix="publikclip-smoke-")  # PUBLIKCLIP_HOME: the app's own state
    if signed_in:
        with open(os.path.join(app_home, "secrets.json"), "w", encoding="utf-8") as handle:
            json.dump({"publik": {"key": FAKE_KEY, "base_url": base}}, handle)
    env = dict(os.environ)
    for unset in ("DO_NOT_TRACK", "PUBLIK_TELEMETRY", "PUBLIK_API_KEY", "PUBLIK_TELEMETRY_INTERVAL_SECONDS"):
        env.pop(unset, None)
    env.update({"PUBLIK_TELEMETRY_URL": base + "/telemetry", "PUBLIK_TELEMETRY_DEBUG": "1", "PUBLIKCLIP_HOME": app_home})
    env.update(extra_env)
    first, began = len(received), time.time()
    log_path = os.path.join(app_home, "stderr.log")
    with open(log_path, "wb") as log:
        process = subprocess.Popen([exe], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log)
        time.sleep(seconds)
        alive = process.poll() is None
        exit_code = process.returncode
        stop(process)
    with open(log_path, "rb") as log:
        debug_lines = [line for line in log.read().decode("utf-8", "replace").splitlines() if "publik-telemetry" in line]
    events = [r for r in received[first:] if r["path"] == "/api/v1/telemetry"]
    others = [r["method"] + " " + r["path"] for r in received[first:] if r["path"] != "/api/v1/telemetry"]
    problems = []
    if not alive:
        problems.append("the app exited within %d s (exit code %s)" % (seconds, exit_code))
    for record in events:
        record["seconds_after_start"] = round(record.pop("time") - began, 2)
        if "authorization" in record["headers"]:
            record["headers"]["authorization"] = record["headers"]["authorization"].replace(FAKE_KEY, "<the fake smoke key>")
    return {"run": name, "alive_after_seconds": seconds if alive else None, "telemetry_requests": events,
            "other_requests": others, "debug_lines": debug_lines}, problems


def check_event(record, signed_in):
    problems = []
    headers = record["headers"]
    try:
        body = json.loads(record["body"])
    except ValueError:
        return ["the body is not JSON"]
    if record["method"] != "POST":
        problems.append("method is %s" % record["method"])
    if "origin" in headers:
        problems.append("the request has an Origin header")
    if not headers.get("content-type", "").startswith("application/json"):
        problems.append("Content-Type is %r" % headers.get("content-type"))
    if len(record["body"].encode("utf-8")) > 2048:
        problems.append("the body is over 2 KB")
    if set(body) - FIELDS:
        problems.append("unknown fields: %s" % sorted(set(body) - FIELDS))
    want = {"v": 1, "app": slug, "event": "app_opened", "app_version": version, "os": os_name}
    for field, value in want.items():
        if body.get(field) != value:
            problems.append("%s is %r, want %r" % (field, body.get(field), value))
    if body.get("arch") not in ("arm64", "x64"):
        problems.append("arch is %r" % body.get("arch"))
    if not UUID4.match(str(body.get("install_id"))):
        problems.append("install_id is not a lowercase UUID v4")
    auth = headers.get("authorization")
    if signed_in and auth != "Bearer <the fake smoke key>":
        problems.append("signed in, but the Authorization header is not the publik key")
    if not signed_in and auth is not None:
        problems.append("signed out, but an Authorization header was sent")
    return problems


result = {"app": slug, "app_version": version, "os": os_name, "executable": os.path.basename(exe), "runs": [], "problems": []}
id_file_before = os.path.exists(id_file)

# 1. opt-out
report, problems = run("DO_NOT_TRACK=1", 12, {"DO_NOT_TRACK": "1"}, signed_in=False)
if report["telemetry_requests"]:
    problems.append("%d request(s) arrived with DO_NOT_TRACK=1" % len(report["telemetry_requests"]))
if not id_file_before and os.path.exists(id_file):
    problems.append("telemetry.json was created with DO_NOT_TRACK=1")
report["telemetry_json_created"] = (not id_file_before) and os.path.exists(id_file)
result["runs"].append(report)
result["problems"] += ["opt-out run: " + p for p in problems]

# 2. signed out, 3. signed in
for name, signed_in in (("signed out", False), ("signed in (fake publik key in secrets.json)", True)):
    report, problems = run(name, 15, {}, signed_in=signed_in)
    events = report["telemetry_requests"]
    if len(events) != 1:
        problems.append("%d telemetry request(s) arrived, want 1" % len(events))
    for record in events:
        problems += check_event(record, signed_in)
    result["runs"].append(report)
    result["problems"] += ["%s run: %s" % (name, p) for p in problems]

try:
    with open(id_file, encoding="utf-8") as handle:
        saved = json.load(handle).get("install_id")
except (OSError, ValueError):
    saved = None
sent = {json.loads(r["body"]).get("install_id") for run_report in result["runs"] for r in run_report["telemetry_requests"]}
if len(sent) != 1 or saved not in sent:
    result["problems"].append("install_id: telemetry.json and the events do not agree")
result["install_id_file_matches_events"] = len(sent) == 1 and saved in sent
result["pass"] = not result["problems"]

with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(result, handle, indent=1)
print(json.dumps(result, indent=1))
print("RESULT: PASS" if result["pass"] else "RESULT: FAIL")
sys.exit(0 if result["pass"] else 1)
