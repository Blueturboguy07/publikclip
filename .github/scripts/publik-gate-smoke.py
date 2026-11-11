#!/usr/bin/env python3
"""Check Sign in with publik, the self-test and the usage counts of a built publikclip.

usage: publik-gate-smoke.py <app executable> <slug> <version> <macos|windows|linux> <result.json>

A fake publik on 127.0.0.1 answers POST /api/v1/installs, GET /api/v1/wallet and
POST /api/v1/telemetry. PUBLIK_ORIGIN points the app at it, so nothing goes to
publikhq.com. Five launches:
  1. self-test:  PUBLIK_SELF_TEST=1 prints "publik self-test: ok <slug> <version>" and exits 0,
                 after POST /installs and GET /wallet;
  2. signed out: the wallet says anonymous; the app provisions, shows the gate, stays up,
                 polls the wallet, and sends no usage event;
  3. opt-out:    signed in with DO_NOT_TRACK=1: no usage event, no telemetry.json;
  4. signed in:  one app_opened arrives with the install key; no new install is made;
  5. 404:        POST /telemetry answers 404; the app stays up.
"signed in" means a credential file with a fake key and the choice "publik", and a
wallet that says claimed. Standard library only. Exit code 0 only when every run passes.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

exe, slug, version, os_name, out_path = sys.argv[1:6]
exe = os.path.abspath(exe)
FAKE_KEY = "pk_live_smoke_0000000000000000"
UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
FIELDS = {"v", "app", "install_id", "event", "app_version", "os", "arch"}
received = []
mode = {"claim_state": "anonymous", "telemetry_status": 204}


class Handler(BaseHTTPRequestHandler):
    def _json(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        headers = {name.lower(): value for name, value in self.headers.items()}
        record = {"method": self.command, "path": self.path, "headers": headers, "body": body, "time": time.time()}
        received.append(record)
        return record

    def do_POST(self):
        record = self._record()
        if self.path == "/api/v1/installs":
            try:
                body = json.loads(record["body"])
            except ValueError:
                body = {}
            install_id = body.get("install_id") or str(uuid.uuid4())
            return self._json(201, {
                "key": FAKE_KEY, "install_id": install_id, "base_url": base,
                "claim_url": origin + "/claim/SMOKE", "claim_state": "anonymous",
            })
        if self.path == "/api/v1/telemetry":
            status = mode["telemetry_status"]
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        return self._json(404, {"error": {"type": "not_found", "message": "no such route"}})

    def do_GET(self):
        record = self._record()
        if self.path == "/api/v1/wallet":
            if record["headers"].get("authorization") != "Bearer " + FAKE_KEY:
                return self._json(401, {"error": {"type": "invalid_key", "message": "This key is not valid."}})
            claimed = mode["claim_state"] == "claimed"
            return self._json(200, {
                "claim_state": mode["claim_state"],
                "account": {"handle": "smoke"} if claimed else None,
                "plan": {"kind": "plan", "label": "Basic"} if claimed else {"kind": "none", "label": None},
                "balance_micros": 0,
                "claim_url": None if claimed else origin + "/claim/SMOKE",
                "top_up_url": origin + "/wallet?top_up=1",
                "linked_at": None,
            })
        return self._json(404, {"error": {"type": "not_found", "message": "no such route"}})

    do_PUT = do_OPTIONS = do_HEAD = do_GET

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # port 0 = any free port
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()
origin = "http://127.0.0.1:%d" % server.server_address[1]
base = origin + "/api/v1"

if os_name == "windows":
    publik_dir = os.path.join(os.environ["LOCALAPPDATA"], "publik")
elif os_name == "macos":
    publik_dir = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "publik")
else:
    publik_dir = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"), "publik")
id_file = os.path.join(publik_dir, "telemetry.json")
credential_file = os.path.join(publik_dir, "apps", slug + ".json")


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


def remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def run(name, seconds, extra_env, signed_in, wait_for_exit=False):
    """One launch. Returns what happened and what was wrong."""
    app_home = tempfile.mkdtemp(prefix="publikclip-smoke-")  # PUBLIKCLIP_HOME: the app's own state
    remove(credential_file)
    mode["claim_state"] = "claimed" if signed_in else "anonymous"
    if signed_in:
        os.makedirs(os.path.dirname(credential_file), exist_ok=True)
        with open(credential_file, "w", encoding="utf-8") as handle:
            json.dump({"key": FAKE_KEY, "base_url": base, "install_id": str(uuid.uuid4()),
                       "claim_url": None, "choice": "publik"}, handle)
    env = dict(os.environ)
    for unset in ("DO_NOT_TRACK", "PUBLIK_TELEMETRY", "PUBLIK_API_KEY", "PUBLIK_TELEMETRY_INTERVAL_SECONDS",
                  "PUBLIK_TELEMETRY_URL", "PUBLIK_SELF_TEST", "PUBLIK_APP_TOKEN"):
        env.pop(unset, None)
    env.update({"PUBLIK_ORIGIN": origin, "PUBLIK_TELEMETRY_DEBUG": "1", "PUBLIKCLIP_HOME": app_home})
    env.update(extra_env)
    first, began = len(received), time.time()
    out_path_ = os.path.join(app_home, "stdout.log")
    err_path = os.path.join(app_home, "stderr.log")
    with open(out_path_, "wb") as out, open(err_path, "wb") as err:
        process = subprocess.Popen([exe], env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        if wait_for_exit:
            try:
                process.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(seconds)
        alive = process.poll() is None
        exit_code = process.returncode
        stop(process)
    with open(out_path_, "rb") as out:
        stdout_lines = out.read().decode("utf-8", "replace").splitlines()
    with open(err_path, "rb") as err:
        debug_lines = [line for line in err.read().decode("utf-8", "replace").splitlines() if "publik-telemetry" in line]
    seen = received[first:]
    events = [r for r in seen if r["path"] == "/api/v1/telemetry"]
    calls = [r["method"] + " " + r["path"] for r in seen]
    for record in events:
        record["seconds_after_start"] = round(record.pop("time") - began, 2)
        if "authorization" in record["headers"]:
            record["headers"]["authorization"] = record["headers"]["authorization"].replace(FAKE_KEY, "<the fake smoke key>")
    problems = []
    if wait_for_exit:
        if alive:
            problems.append("the app did not exit within %d s" % seconds)
        elif exit_code != 0:
            problems.append("the app exited with code %s" % exit_code)
    elif not alive:
        problems.append("the app exited within %d s (exit code %s)" % (seconds, exit_code))
    report = {"run": name, "alive_after_seconds": seconds if alive else None, "exit_code": exit_code,
              "stdout": [line.replace(FAKE_KEY, "<the fake smoke key>") for line in stdout_lines][:20],
              "api_calls": calls[:40], "telemetry_requests": events, "debug_lines": debug_lines}
    return report, problems


def check_event(record):
    problems = []
    headers = record["headers"]
    try:
        body = json.loads(record["body"])
    except ValueError:
        return ["the body is not JSON"]
    if record["method"] != "POST":
        problems.append("method is %s" % record["method"])
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
    if headers.get("authorization") != "Bearer <the fake smoke key>":
        problems.append("the Authorization header is not the install key")
    return problems


result = {"app": slug, "app_version": version, "os": os_name, "executable": os.path.basename(exe), "runs": [], "problems": []}

# 1. the self-test (setup.md step 6): one line, then exit
report, problems = run("self-test", 60, {"PUBLIK_SELF_TEST": "1"}, signed_in=False, wait_for_exit=True)
want_line = "publik self-test: ok %s %s" % (slug, version)
if report["stdout"] != [want_line]:
    problems.append("stdout is %r, want [%r]" % (report["stdout"], want_line))
if [c for c in report["api_calls"] if c in ("POST /api/v1/installs", "GET /api/v1/wallet")] != ["POST /api/v1/installs", "GET /api/v1/wallet"]:
    problems.append("calls are %r, want POST /installs then GET /wallet" % report["api_calls"])
if report["telemetry_requests"]:
    problems.append("a usage event arrived during the self-test")
result["runs"].append(report)
result["problems"] += ["self-test run: " + p for p in problems]

# 2. signed out: the gate holds the app; no usage event before the sign-in
report, problems = run("signed out (anonymous wallet)", 15, {}, signed_in=False)
if report["telemetry_requests"]:
    problems.append("%d usage event(s) arrived before the sign-in" % len(report["telemetry_requests"]))
if "POST /api/v1/installs" not in report["api_calls"]:
    problems.append("the app did not provision (no POST /installs)")
if report["api_calls"].count("GET /api/v1/wallet") < 2:
    problems.append("the gate did not poll the wallet (want at least 2 GET /wallet in 15 s)")
result["runs"].append(report)
result["problems"] += ["signed out run: " + p for p in problems]

# 3. opt-out
id_file_before = os.path.exists(id_file)
remove(id_file)
report, problems = run("signed in, DO_NOT_TRACK=1", 12, {"DO_NOT_TRACK": "1"}, signed_in=True)
if report["telemetry_requests"]:
    problems.append("%d request(s) arrived with DO_NOT_TRACK=1" % len(report["telemetry_requests"]))
if os.path.exists(id_file):
    problems.append("telemetry.json was created with DO_NOT_TRACK=1")
report["telemetry_json_created"] = os.path.exists(id_file)
result["runs"].append(report)
result["problems"] += ["opt-out run: " + p for p in problems]

# 4. signed in: app_opened with the install key, no new install
report, problems = run("signed in (fake key in the credential file)", 15, {}, signed_in=True)
events = report["telemetry_requests"]
if len(events) != 1:
    problems.append("%d usage event(s) arrived, want 1" % len(events))
for record in events:
    problems += check_event(record)
if "POST /api/v1/installs" in report["api_calls"]:
    problems.append("the app provisioned again although it had a key")
result["runs"].append(report)
result["problems"] += ["signed in run: " + p for p in problems]

# 5. the usage endpoint answers 404: the app does not mind
mode["telemetry_status"] = 404
report, problems = run("signed in, POST /telemetry answers 404", 15, {}, signed_in=True)
if len(report["telemetry_requests"]) != 1:
    problems.append("%d usage event(s) arrived, want 1" % len(report["telemetry_requests"]))
result["runs"].append(report)
result["problems"] += ["404 run: " + p for p in problems]
mode["telemetry_status"] = 204

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
remove(credential_file)

with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(result, handle, indent=1)
print(json.dumps(result, indent=1))
print("RESULT: PASS" if result["pass"] else "RESULT: FAIL")
sys.exit(0 if result["pass"] else 1)
