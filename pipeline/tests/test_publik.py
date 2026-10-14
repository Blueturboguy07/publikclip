"""publik API as the default brain: credential resolution, header transport,
the 402/401 contract, the status file, no-retry, and the Ollama promise.

Every HTTP call is faked (monkeypatched httpx.post) — nothing here reaches
the real gateway, Google, or Ollama."""

import json
import re
from pathlib import Path

import httpx
import pytest

from publikclip_pipeline import cli, config
from publikclip_pipeline.edits import visuals
from publikclip_pipeline.scoring import llm as llm_mod

REPO = Path(__file__).resolve().parents[2]
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    for var in ("PUBLIK_API_KEY", "PUBLIK_API_BASE_URL", "PUBLIKCLIP_GEMINI_API_KEY", "PUBLIKCLIP_GEMINI_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    # the real per-app convention file on a dev machine must never leak in
    monkeypatch.setattr(config, "publik_shared_file", lambda: tmp_path / "shared" / "publikclip.json")
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    config.ensure_home()
    yield


class _Res:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = httpx.Headers(headers or {})

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "https://gw.test")
            raise httpx.HTTPStatusError("x", request=req, response=httpx.Response(self.status_code, request=req))


def _ok_text(data=None):
    return _Res(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(data or {"ok": True})}]}}]})


def _write_secrets(obj):
    (config.home_dir() / "secrets.json").write_text(json.dumps(obj))


def _capture(monkeypatch, responses):
    calls = []
    queue = list(responses)

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        nxt = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(llm_mod.httpx, "post", fake_post)
    monkeypatch.setattr(visuals.httpx, "post", fake_post)
    return calls


def _status():
    return json.loads(config.publik_status_path().read_text())


def _cache_files():
    return list((config.home_dir() / "llm-cache").glob("*.json"))


# --- credential resolution ------------------------------------------------------


def test_publik_mode_resolves_secrets_block():
    _write_secrets({"publik": {"key": "pk_live_a", "base_url": "https://gw.test/api/v1", "install_id": "i"}})
    ep = llm_mod.resolve_endpoint("publik")
    assert ep.provider == "publik"
    assert ep.model == "publik-vision"
    assert ep.image_model == "publik-image"
    assert ep.url() == "https://gw.test/api/v1/gemini/v1beta/models/publik-vision:generateContent"


def test_base_url_that_already_names_the_dialect_is_not_doubled():
    _write_secrets({"publik": {"key": "pk_live_a", "base_url": "https://gw.test/api/v1/gemini/"}})
    assert llm_mod.resolve_endpoint("publik").url() == (
        "https://gw.test/api/v1/gemini/v1beta/models/publik-vision:generateContent"
    )


def test_env_wins_over_secrets(monkeypatch):
    _write_secrets({"publik": {"key": "pk_live_disk", "base_url": "https://disk.test/api/v1"}})
    monkeypatch.setenv("PUBLIK_API_KEY", "pk_live_env")
    monkeypatch.setenv("PUBLIK_API_BASE_URL", "http://localhost:3000/api/v1")
    ep = llm_mod.resolve_endpoint("publik")
    assert ep.key == "pk_live_env"
    assert ep.url().startswith("http://localhost:3000/api/v1/gemini/v1beta/models/publik-vision")
    monkeypatch.delenv("PUBLIK_API_BASE_URL")
    ep = llm_mod.resolve_endpoint("publik")
    assert ep.url().startswith("https://publikhq.com/api/v1/gemini/v1beta/models/")


def test_shared_file_is_last_resort(tmp_path):
    shared = config.publik_shared_file()
    shared.parent.mkdir(parents=True)
    shared.write_text(json.dumps({"key": "pk_live_shared", "base_url": "https://shared.test/api/v1"}))
    ep = llm_mod.resolve_endpoint("publik")
    assert ep.key == "pk_live_shared"
    # this app's own store wins over the shared file
    _write_secrets({"publik": {"key": "pk_live_own"}})
    assert llm_mod.resolve_endpoint("publik").key == "pk_live_own"


def test_publik_mode_without_credential_is_actionable():
    with pytest.raises(llm_mod.LlmError) as exc:
        llm_mod.resolve_endpoint("publik")
    assert "publik API isn't set up" in str(exc.value)
    assert "Settings" in str(exc.value)


def test_disconnected_block_is_not_a_credential():
    # Disconnect keeps only install_id; that must read as "not set up".
    _write_secrets({"publik": {"install_id": "abc"}})
    with pytest.raises(llm_mod.LlmError):
        llm_mod.resolve_endpoint("publik")


def test_gemini_mode_untouched(monkeypatch):
    _write_secrets({"publik": {"key": "pk_live_a"}})
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    ep = llm_mod.resolve_endpoint("gemini")
    assert ep.provider == "gemini"
    assert ep.key == "AIzaTEST"
    assert ep.model == "gemini-flash-latest"
    assert ep.url() == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
    )
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_BASE_URL", "http://proxy.test")
    assert llm_mod.resolve_endpoint("gemini").url().startswith("http://proxy.test/v1beta/models/gemini-flash-latest")


def test_default_mode_is_publik():
    assert config.Settings().llm_mode == "publik"
    assert config.Settings.from_json({}).llm_mode == "publik"
    # a job snapshotted on gemini keeps gemini
    assert config.Settings.from_json({"llm_mode": "gemini"}).llm_mode == "gemini"


# --- transport ------------------------------------------------------------------


def test_header_transport_no_query_param(monkeypatch):
    _write_secrets({"publik": {"key": "pk_live_a", "base_url": "https://gw.test/api/v1"}})
    calls = _capture(monkeypatch, [_ok_text()])
    assert llm_mod.make_client("publik").generate_json("p", SCHEMA) == {"ok": True}
    call = calls[0]
    assert call["url"].endswith("/gemini/v1beta/models/publik-vision:generateContent")
    assert call["headers"]["x-goog-api-key"] == "pk_live_a"
    assert re.fullmatch(r"[0-9a-f]{32}", call["headers"]["x-publik-idempotency-key"])
    assert "params" not in call
    # no field the gateway refuses
    for refused in ("systemInstruction", "tools", "toolConfig", "cachedContent", "safetySettings"):
        assert refused not in call["json"]

    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    calls = _capture(monkeypatch, [_ok_text()])
    llm_mod.make_client("gemini").generate_json("p", SCHEMA)
    assert calls[0]["headers"] == {"x-goog-api-key": "AIzaTEST"}
    assert "params" not in calls[0]
    assert "generativelanguage.googleapis.com" in calls[0]["url"]


def test_status_file_from_headers(monkeypatch):
    _write_secrets({"publik": {"key": "pk_live_a"}})
    res = _ok_text()
    res.headers = httpx.Headers({
        "x-publik-balance": "180000",
        "x-publik-charge-micros": "1234",
        "x-publik-request-id": "req_1",
        "x-publik-claim-state": "anonymous",
        "x-publik-starter-remaining": "180000",
    })
    _capture(monkeypatch, [res])
    llm_mod.make_client("publik").generate_json("p", SCHEMA)
    st = _status()
    assert st["balance_micros"] == 180000
    assert st["last_charge_micros"] == 1234
    assert st["starter_remaining_micros"] == 180000
    assert st["last_request_id"] == "req_1"
    assert st["claim_state"] == "anonymous"
    assert st["needs_credit"] is False and st["disconnected"] is False


# --- the 402 / 401 contract -----------------------------------------------------


def _402(state):
    top_up = "https://publikhq.com/claim/HK7F-2QWD" if state == "anonymous" else "https://publikhq.com/dashboard/api/add"
    return _Res(
        402,
        {"error": {
            "type": "insufficient_credit",
            "message": "Not enough publik balance for this request.",
            "top_up_url": top_up,
            "claim_state": state,
            "claim_url": "https://publikhq.com/claim/HK7F-2QWD",
            "add_credit_url": "https://publikhq.com/dashboard/api/add",
        }},
        {"x-publik-balance": "0", "x-publik-claim-state": state},
    ), top_up


@pytest.mark.parametrize("state", ["anonymous", "claimed"])
def test_publik_402_surfaces_exactly_one_link(monkeypatch, state):
    _write_secrets({"publik": {"key": "pk_live_a"}})
    res, top_up = _402(state)
    calls = _capture(monkeypatch, [res])
    with pytest.raises(llm_mod.PublikStopError) as exc:
        llm_mod.make_client("publik").generate_json("p", SCHEMA)
    text = str(exc.value)
    assert "Not enough publik balance" in text
    assert re.findall(r"https?://\S+", text) == [top_up]
    assert len(calls) == 1
    st = _status()
    assert st["needs_credit"] is True
    assert st["top_up_url"] == top_up
    assert st["claim_state"] == state
    assert st["balance_micros"] == 0
    assert _cache_files() == []


def test_success_after_402_clears_needs_credit(monkeypatch):
    _write_secrets({"publik": {"key": "pk_live_a"}})
    res, _ = _402("anonymous")
    _capture(monkeypatch, [res])
    with pytest.raises(llm_mod.LlmError):
        llm_mod.make_client("publik").generate_json("p", SCHEMA)
    _capture(monkeypatch, [_ok_text()])
    llm_mod.make_client("publik").generate_json("p", SCHEMA)
    assert _status()["needs_credit"] is False


def test_publik_401_marks_disconnected_for_reprovision(monkeypatch):
    _write_secrets({"publik": {"key": "pk_live_a"}})
    _capture(monkeypatch, [_Res(401, {"error": {"type": "invalid_api_key", "message": "Invalid API key."}})])
    with pytest.raises(llm_mod.PublikStopError) as exc:
        llm_mod.make_client("publik").generate_json("p", SCHEMA)
    assert "Reconnect" in str(exc.value)
    st = _status()
    assert st["disconnected"] is True
    assert st["reprovision"] is True
    assert st["error_type"] == "invalid_api_key"


def test_publik_403_marks_disconnected(monkeypatch):
    _write_secrets({"publik": {"key": "pk_live_a"}})
    _capture(monkeypatch, [_Res(403, {"error": {"type": "install_revoked", "message": "x", "reprovision": False}})])
    with pytest.raises(llm_mod.PublikStopError):
        llm_mod.make_client("publik").generate_json("p", SCHEMA)
    st = _status()
    assert st["disconnected"] is True
    assert st["reprovision"] is False


def test_publik_other_error_surfaces_gateway_message(monkeypatch):
    _write_secrets({"publik": {"key": "pk_live_a"}})
    calls = _capture(monkeypatch, [_Res(502, {"error": {"type": "upstream_error", "message": "model busy"}})])
    with pytest.raises(llm_mod.LlmError) as exc:
        llm_mod.make_client("publik").generate_json("p", SCHEMA)
    assert "publik API 502: model busy" in str(exc.value)
    assert len(calls) == 1


# --- retries --------------------------------------------------------------------


def test_publik_does_not_retry(monkeypatch):
    _write_secrets({"publik": {"key": "pk_live_a"}})
    calls = _capture(monkeypatch, [httpx.ReadTimeout("slow")])
    with pytest.raises(llm_mod.LlmError) as exc:
        llm_mod.make_client("publik").generate_json("p", SCHEMA)
    assert len(calls) == 1
    assert "publik API" in str(exc.value)


def test_gemini_mode_still_retries_three_times(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    calls = _capture(monkeypatch, [httpx.ReadTimeout("slow")])
    with pytest.raises(llm_mod.LlmError):
        llm_mod.make_client("gemini").generate_json("p", SCHEMA)
    assert len(calls) == 3


def test_cache_isolated_between_providers(monkeypatch):
    _write_secrets({"publik": {"key": "pk_live_a"}, "gemini_api_key": "AIzaTEST"})
    calls = _capture(monkeypatch, [_ok_text()])
    llm_mod.make_client("publik").generate_json("same prompt", SCHEMA)
    llm_mod.make_client("gemini").generate_json("same prompt", SCHEMA)
    assert len(calls) == 2
    assert len(_cache_files()) == 2
    # and identical inputs never re-spend
    llm_mod.make_client("publik").generate_json("same prompt", SCHEMA)
    assert len(calls) == 2


# --- image generation -----------------------------------------------------------


def _image_res():
    import base64

    return _Res(200, {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(b"PNG").decode()}}]}}]})


def test_ollama_mode_never_spends_publik(monkeypatch, tmp_path):
    _write_secrets({"publik": {"key": "pk_live_a"}})
    calls = _capture(monkeypatch, [_image_res()])
    assert visuals.fetch_gemini("possum", tmp_path, "ollama") is None
    assert calls == []
    _write_secrets({"publik": {"key": "pk_live_a"}, "gemini_api_key": "AIzaTEST"})
    path = visuals.fetch_gemini("possum", tmp_path, "ollama")
    assert path and Path(path).read_bytes() == b"PNG"
    assert "generativelanguage.googleapis.com" in calls[0]["url"]
    assert calls[0]["headers"] == {"x-goog-api-key": "AIzaTEST"}


def test_image_alias(monkeypatch, tmp_path):
    _write_secrets({"publik": {"key": "pk_live_a", "base_url": "https://gw.test/api/v1"}})
    calls = _capture(monkeypatch, [_image_res()])
    path = visuals.fetch_gemini("possum", tmp_path, "publik")
    assert path and Path(path).read_bytes() == b"PNG"
    assert calls[0]["url"] == "https://gw.test/api/v1/gemini/v1beta/models/publik-image:generateContent"
    assert calls[0]["headers"] == {"x-goog-api-key": "pk_live_a"}
    assert "params" not in calls[0]


def test_image_402_surfaces(monkeypatch, tmp_path):
    _write_secrets({"publik": {"key": "pk_live_a"}})
    res, top_up = _402("anonymous")
    _capture(monkeypatch, [res])
    with pytest.raises(llm_mod.PublikStopError) as exc:
        visuals.fetch_gemini("possum", tmp_path, "publik")
    assert top_up in str(exc.value)


# --- CLI + scoring stage --------------------------------------------------------


def test_cli_accepts_publik(monkeypatch):
    seen = {}

    def fake_execute(job, jsonl):
        seen["settings"] = json.loads(job.settings_json)
        return 0

    monkeypatch.setattr(cli, "_execute", fake_execute)
    assert cli.main(["run", "x.mp4", "--llm", "publik"]) == 0
    assert seen["settings"]["llm_mode"] == "publik"
    # and with no flag the default is publik too
    assert cli.main(["run", "x.mp4"]) == 0
    assert seen["settings"]["llm_mode"] == "publik"


def test_scoring_stage_turns_publik_stop_into_stage_error(monkeypatch, tmp_path):
    """A 402 mid-run must end the stage with the message (so the CLI emits
    {"ok": false, "error": …} with the link), not crash the sidecar."""
    from types import SimpleNamespace

    from publikclip_pipeline.jobs.queue import StageError
    from publikclip_pipeline.scoring.stage import ScoreStage

    _write_secrets({"publik": {"key": "pk_live_a"}})
    res, top_up = _402("anonymous")
    calls = _capture(monkeypatch, [res])
    curves = tmp_path / "curves.json"
    curves.write_text(json.dumps({"arousal": [0.1] * 60, "arousal_grid_sec": 0.5}))
    words = [{"word": f"w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(50)]
    ctx = SimpleNamespace(
        prior={
            "ingest": {"heatmap": None, "probe": {"duration_sec": 30}},
            "diarize": {"segments": [{"start": 0, "end": 25, "speaker": 0, "words": words}]},
            "events": {"timeline": [], "curves_path": str(curves)},
            "candidates": {"candidates": [{"start": 0, "end": 25}]},
        },
        settings=config.Settings(),
        job_dir=tmp_path,
        emit=lambda *a, **k: None,
    )
    with pytest.raises(StageError) as exc:
        ScoreStage().run(ctx)
    assert top_up in str(exc.value)
    assert len(calls) == 1


# --- copy rule ------------------------------------------------------------------


def test_copy_rule():
    banned = re.compile(r"OpenAI API|ChatGPT credit|Gemini credit|publik credits|in credits", re.I)
    roots = [REPO / "pipeline" / "publikclip_pipeline", REPO / "app" / "src", REPO / "README.md"]
    hits = []
    for root in roots:
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.suffix in {".py", ".ts", ".tsx", ".md"}]
        for f in files:
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if banned.search(line):
                    hits.append(f"{f}:{n}: {line.strip()}")
    assert hits == []


# --- own-key fallback across Gemini models ---------------------------------------
#
# Oct 7 2026, Windows: a brand-new free key got "503 Service Unavailable" on
# gemini-flash-latest on every run for three days, and the job died at JUDGE
# moment 1/35 after three instant retries. The lite aliases answered the same
# key in a second.


def _overloaded():
    return _Res(503, {"error": {"message": "The model is overloaded. Please try again later."}})


def _retired():
    return _Res(404, {"error": {"message": "This model is no longer available to new users."}})


def _model_of(call):
    return re.search(r"/models/([^:]+):generateContent", call["url"]).group(1)


def test_gemini_503_three_times_moves_to_the_lite_model(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    calls = _capture(monkeypatch, [_overloaded(), _overloaded(), _overloaded(), _ok_text()])
    client = llm_mod.make_client("gemini")
    assert client.generate_json("p", SCHEMA) == {"ok": True}
    assert [_model_of(c) for c in calls] == [
        "gemini-flash-latest",
        "gemini-flash-latest",
        "gemini-flash-latest",
        "gemini-flash-lite-latest",
    ]
    assert client.model == "gemini-flash-lite-latest"


def test_gemini_404_skips_the_model_without_retrying(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    calls = _capture(monkeypatch, [_retired(), _ok_text()])
    assert llm_mod.make_client("gemini").generate_json("p", SCHEMA) == {"ok": True}
    assert [_model_of(c) for c in calls] == ["gemini-flash-latest", "gemini-flash-lite-latest"]


def test_gemini_switched_model_sticks_for_the_rest_of_the_run(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    _capture(monkeypatch, [_overloaded(), _overloaded(), _overloaded(), _ok_text()])
    client = llm_mod.make_client("gemini")
    client.generate_json("moment 1", SCHEMA)
    calls = _capture(monkeypatch, [_ok_text()])
    client.generate_json("moment 2", SCHEMA)
    assert [_model_of(c) for c in calls] == ["gemini-flash-lite-latest"]


def test_gemini_switch_is_reported_to_the_progress_panel(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    _capture(monkeypatch, [_overloaded(), _overloaded(), _overloaded(), _ok_text()])
    notes = []
    llm_mod.make_client("gemini", notify=notes.append).generate_json("p", SCHEMA)
    assert len(notes) == 1
    assert "gemini-flash-latest" in notes[0] and "503" in notes[0]
    assert "trying gemini-flash-lite-latest" in notes[0]


def test_gemini_every_model_overloaded_is_one_human_line(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    calls = _capture(monkeypatch, [_overloaded()])
    with pytest.raises(llm_mod.LlmError) as exc:
        llm_mod.make_client("gemini").generate_json("p", SCHEMA)
    text = str(exc.value)
    assert len(calls) == 3 * (1 + len(llm_mod.GEMINI_FALLBACK_MODELS))
    assert "gemini-flash-latest: 503 The model is overloaded" in text
    assert "publik API" in text  # the way out is named
    assert "Server error" not in text  # not httpx's wording


# --- free-tier quotas ------------------------------------------------------------
#
# Oct 11 2026, Windows, free key: 5 requests a minute and 20 a day on the
# flash alias, 10 a minute and 500 a day on the lite models (AI Studio).
# Google's 429 names the quota (quotaId …PerMinute… / …PerDay…) and the
# wait (retryDelay). The old code read "billing" in Google's standard text
# ("check your plan and billing details") as a billing stop and died.

GOOGLE_QUOTA_TEXT = (
    "You exceeded your current quota, please check your plan and billing details. "
    "For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits.\n"
    "* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, "
    "limit: {limit}, model: {model}\nPlease retry in {retry}."
)


def _quota_429(scope, limit, retry="15s", model="gemini-flash-latest", details=True):
    quota_id = {
        "minute": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
        "day": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
    }[scope]
    body = {
        "error": {
            "code": 429,
            "message": GOOGLE_QUOTA_TEXT.format(limit=limit, model=model, retry=retry),
            "status": "RESOURCE_EXHAUSTED",
        }
    }
    if details:
        body["error"]["details"] = [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                        "quotaId": quota_id,
                        "quotaDimensions": {"location": "global", "model": model},
                        "quotaValue": str(limit),
                    }
                ],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry},
        ]
    return _Res(429, body)


def _sleeps(monkeypatch):
    slept = []
    monkeypatch.setattr(llm_mod.time, "sleep", slept.append)
    return slept


def test_gemini_per_minute_429_waits_what_google_asked_and_keeps_the_model(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    slept = _sleeps(monkeypatch)
    calls = _capture(monkeypatch, [_quota_429("minute", 5, "15s"), _ok_text()])
    notes = []
    client = llm_mod.make_client("gemini", notify=notes.append)
    assert client.generate_json("p", SCHEMA) == {"ok": True}
    assert [_model_of(c) for c in calls] == ["gemini-flash-latest", "gemini-flash-latest"]
    assert client.model == "gemini-flash-latest"
    assert slept and abs(slept[0] - 16.0) < 0.01  # Google's 15 s plus one
    assert "5 requests a minute" in notes[0] and "waiting 16 s" in notes[0]


def test_gemini_standard_quota_text_is_not_a_billing_stop(monkeypatch):
    """The regression: "billing details" in Google's own wording."""
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    _sleeps(monkeypatch)
    calls = _capture(monkeypatch, [_quota_429("minute", 5, "15s", details=False), _ok_text()])
    assert llm_mod.make_client("gemini").generate_json("p", SCHEMA) == {"ok": True}
    assert len(calls) == 2 and _model_of(calls[-1]) == "gemini-flash-latest"


def test_gemini_402_is_a_billing_stop_with_one_call(monkeypatch):
    """Oct 14 2026: a key whose Google billing ran dry answered 402 and the
    client retried three times, then said only "Gemini call failed"."""
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    _sleeps(monkeypatch)
    calls = _capture(monkeypatch, [_Res(402, {"error": {"message": "Payment Required"}})])
    with pytest.raises(llm_mod.LlmError) as err:
        llm_mod.make_client("gemini").generate_json("p", SCHEMA)
    assert "402" in str(err.value) and "billing" in str(err.value) and "resume" in str(err.value)
    assert len(calls) == 1


def test_gemini_paces_the_model_after_a_per_minute_hit(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    slept = _sleeps(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(llm_mod.time, "monotonic", lambda: clock[0])
    _capture(monkeypatch, [_quota_429("minute", 5, "15s"), _ok_text()])
    client = llm_mod.make_client("gemini")
    client.generate_json("moment 1", SCHEMA)
    _capture(monkeypatch, [_ok_text()])
    clock[0] += 2.0  # two seconds later: too soon for 5 a minute
    client.generate_json("moment 2", SCHEMA)
    assert abs(slept[-1] - (60.0 / 5 + 0.5 - 2.0)) < 0.01


def test_gemini_per_day_429_moves_to_the_next_model_at_once(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    slept = _sleeps(monkeypatch)
    calls = _capture(monkeypatch, [_quota_429("day", 20, "39599.4s"), _ok_text()])
    notes = []
    client = llm_mod.make_client("gemini", notify=notes.append)
    assert client.generate_json("p", SCHEMA) == {"ok": True}
    assert [_model_of(c) for c in calls] == ["gemini-flash-latest", "gemini-flash-lite-latest"]
    assert slept == []  # no strike waits: the day is over for that model
    assert "today's free requests used (20 a day)" in notes[0]
    assert "in about 11 h" in notes[0] and "trying gemini-flash-lite-latest" in notes[0]


def test_gemini_day_out_model_is_not_asked_again_this_run(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    _sleeps(monkeypatch)
    _capture(monkeypatch, [_quota_429("day", 20, "39599s"), _ok_text()])
    client = llm_mod.make_client("gemini")
    client.generate_json("moment 1", SCHEMA)
    calls = _capture(monkeypatch, [_ok_text()])
    client.generate_json("moment 2", SCHEMA)
    assert [_model_of(c) for c in calls] == ["gemini-flash-lite-latest"]


def test_gemini_every_model_out_for_the_day_names_the_reset(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    _sleeps(monkeypatch)
    calls = _capture(monkeypatch, [_quota_429("day", 20, "39599s")])
    with pytest.raises(llm_mod.QuotaExhaustedError) as exc:
        llm_mod.make_client("gemini").generate_json("p", SCHEMA)
    assert len(calls) == 1 + len(llm_mod.GEMINI_FALLBACK_MODELS)  # one each, no retries
    text = str(exc.value)
    assert "today's free requests on every model" in text
    assert "midnight Pacific" in text and "in about 11 h" in text
    assert "Resume the job" in text and "publik API" in text


def test_gemini_day_out_everywhere_fails_fast_on_the_same_client(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    _sleeps(monkeypatch)
    _capture(monkeypatch, [_quota_429("day", 20, "39599s")])
    client = llm_mod.make_client("gemini")
    with pytest.raises(llm_mod.QuotaExhaustedError):
        client.generate_json("p", SCHEMA)
    calls = _capture(monkeypatch, [_ok_text()])
    with pytest.raises(llm_mod.QuotaExhaustedError):
        client.generate_json("q", SCHEMA)
    assert calls == []


def test_gemini_unlabeled_429_waits_then_gives_the_model_up(monkeypatch):
    """A bare 429 with no details and no wait: treated as a per-minute
    limit, waited out a few times, then the next model."""
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    slept = _sleeps(monkeypatch)
    bare = _Res(429, {"error": {"message": "Resource has been exhausted (e.g. check quota)."}})
    calls = _capture(monkeypatch, [bare, bare, bare, bare, bare, _ok_text()])
    assert llm_mod.make_client("gemini").generate_json("p", SCHEMA) == {"ok": True}
    assert [_model_of(c) for c in calls] == ["gemini-flash-latest"] * 5 + ["gemini-flash-lite-latest"]
    # four quota waits of 15 s + 1; the other sleeps are the pacing between tries
    quota_waits = [s for s in slept if abs(s - (llm_mod.QUOTA_DEFAULT_WAIT + 1)) < 0.01]
    assert len(quota_waits) == llm_mod.QUOTA_WAITS_PER_CALL
    assert all(s <= 60.0 / llm_mod.QUOTA_DEFAULT_RPM + 0.5 + 0.01 for s in slept if s not in quota_waits)


def test_gemini_long_unlabeled_wait_is_read_as_a_daily_quota(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    slept = _sleeps(monkeypatch)
    calls = _capture(monkeypatch, [_quota_429("day", 20, "72000s", details=False), _ok_text()])
    assert llm_mod.make_client("gemini").generate_json("p", SCHEMA) == {"ok": True}
    assert [_model_of(c) for c in calls] == ["gemini-flash-latest", "gemini-flash-lite-latest"]
    assert slept == []


def test_gemini_cached_answer_from_any_model_needs_no_call(monkeypatch):
    """Resume after a model switch: yesterday's lite answers are found
    before the flash alias is asked (and charged) again."""
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    _sleeps(monkeypatch)
    _capture(monkeypatch, [_quota_429("day", 20, "39599s"), _ok_text({"from": "lite"})])
    llm_mod.make_client("gemini").generate_json("moment 1", SCHEMA)
    calls = _capture(monkeypatch, [_ok_text({"from": "flash"})])
    fresh = llm_mod.make_client("gemini")  # a resumed job starts at the top of the ladder
    assert fresh.generate_json("moment 1", SCHEMA) == {"from": "lite"}
    assert calls == []


# Verbatim from the Oct 11 2026 screenshot (the app shows error.message only).
AYAAN_429 = (
    "You exceeded your current quota, please check your plan and billing details. For more "
    "information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To "
    "monitor your current usage, head to: https://ai.dev/rate-limit. * Quota exceeded for metric: "
    "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: "
    "gemini-3.8-flash\nPlease retry in 20h20m4.321946213s."
)


def test_the_screenshot_message_alone_is_read_as_a_daily_quota(monkeypatch):
    """No structured details at all: the words still say 20 a day, 20 h."""
    hit = llm_mod._quota_hit(_Res(429, {"error": {"message": AYAAN_429}}))
    assert hit.scope == "day" and hit.limit == 20
    assert abs(hit.retry_after - (20 * 3600 + 20 * 60 + 4.321946213)) < 0.001
    assert "resets in about 20 h" in hit.describe()
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    slept = _sleeps(monkeypatch)
    calls = _capture(monkeypatch, [_Res(429, {"error": {"message": AYAAN_429}}), _ok_text()])
    assert llm_mod.make_client("gemini").generate_json("p", SCHEMA) == {"ok": True}
    assert [_model_of(c) for c in calls] == ["gemini-flash-latest", "gemini-flash-lite-latest"]
    assert slept == []


def test_duration_spellings():
    assert llm_mod._seconds("15s") == 15.0
    assert llm_mod._seconds("39599.4s") == 39599.4
    assert llm_mod._seconds("20h20m4.321946213s") == 20 * 3600 + 20 * 60 + 4.321946213
    assert llm_mod._seconds("2m") == 120.0 and llm_mod._seconds("1h") == 3600.0
    assert llm_mod._seconds("30") == 30.0
    assert llm_mod._seconds("soon") is None and llm_mod._seconds("") is None


def test_quota_hit_parser_reads_details_then_words():
    day = _quota_429("day", 20, "39599.4s")
    hit = llm_mod._quota_hit(day)
    assert (hit.scope, hit.limit, hit.retry_after) == ("day", 20, 39599.4)
    minute = _quota_429("minute", 5, "15s", details=False)
    hit = llm_mod._quota_hit(minute)
    assert (hit.scope, hit.limit, hit.retry_after) == ("minute", 5, 15.0)
    both = _quota_429("minute", 5, "30s")
    both._body["error"]["details"][0]["violations"].append(
        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}
    )
    assert llm_mod._quota_hit(both).scope == "day"
    assert llm_mod._quota_hit(_Res(429, {"error": {"message": "slow down"}})).scope == "unknown"
    assert llm_mod._quota_hit(_Res(429, "not json")).scope == "unknown"


def test_gemini_network_timeout_does_not_cascade_through_models(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    calls = _capture(monkeypatch, [httpx.ReadTimeout("slow")])
    with pytest.raises(llm_mod.LlmError):
        llm_mod.make_client("gemini").generate_json("p", SCHEMA)
    assert {_model_of(c) for c in calls} == {"gemini-flash-latest"}


def test_gemini_bad_key_never_tries_another_model(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTEST")
    calls = _capture(monkeypatch, [_Res(403, {"error": {"message": "nope"}})])
    with pytest.raises(llm_mod.LlmError) as exc:
        llm_mod.make_client("gemini").generate_json("p", SCHEMA)
    assert len(calls) == 1 and "rejected the API key" in str(exc.value)


def test_publik_5xx_never_falls_back_to_a_gemini_slug(monkeypatch):
    _write_secrets({"publik": {"key": "pk_live_a"}})
    calls = _capture(monkeypatch, [_overloaded()])
    with pytest.raises(llm_mod.LlmError) as exc:
        llm_mod.make_client("publik").generate_json("p", SCHEMA)
    assert len(calls) == 1
    assert "publik API 503" in str(exc.value)
    assert "gemini" not in calls[0]["url"].split("/models/")[1]
