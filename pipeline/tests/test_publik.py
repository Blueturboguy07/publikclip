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
