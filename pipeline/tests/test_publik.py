"""publik API as the default scoring brain.

What these tests hold, in one line each: the credential is found in the right
order and never invented; the key rides the header and not the URL; a publik
call names an alias and never a gemini-* slug; the own-key and Ollama paths
are exactly what they were; a 402 produces one actionable sentence with one
link and no cache entry; and the balance line the UI reads comes from the
response's own headers.

No network: every test monkeypatches the module's httpx.post, which is the
suite's own convention (tests/test_insights_sync.py), and the autouse home
fixture is what makes them hermetic on Windows too (tests/test_queue.py).
"""

import json

import httpx
import pytest

from publikclip_pipeline import config
from publikclip_pipeline.edits import visuals
from publikclip_pipeline.scoring import llm as llm_mod


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_HOME", str(tmp_path / "home"))
    for var in ("PUBLIK_API_KEY", "PUBLIK_API_BASE_URL", "PUBLIKCLIP_GEMINI_API_KEY", "PUBLIKCLIP_GEMINI_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    # The shared convention file is a real path on the developer's machine;
    # point it somewhere that does not exist unless a test creates it.
    monkeypatch.setattr(config, "publik_shared_file", lambda: tmp_path / "shared" / "publikclip.json")
    config.ensure_home()
    yield


def write_secrets(**fields) -> None:
    (config.home_dir() / "secrets.json").write_text(json.dumps(fields))


def read_status() -> dict:
    return json.loads(config.publik_status_path().read_text())


class _Res:
    """Enough of httpx.Response for the paths under test."""

    def __init__(self, status: int, body: dict, headers: dict | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]


def ok_payload(text: str = '{"verdict": "funny"}') -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


PUBLIK_HEADERS = {
    "x-publik-balance": "180000",
    "x-publik-week-used": "70000",
    "x-publik-week-budget": "none",
    "x-publik-week-resets-at": "2026-09-25T17:04:11Z",
    "x-publik-claim-state": "anonymous",
    "x-publik-starter-remaining": "180000",
    "x-publik-charge-micros": "2550",
}


def capture(monkeypatch, response):
    """Swaps httpx.post for one that records its kwargs and answers `response`
    (or calls it, for a sequence)."""
    calls: list[dict] = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return response(len(calls) - 1) if callable(response) else response

    monkeypatch.setattr(llm_mod.httpx, "post", fake_post)
    return calls


# ---------------------------------------------------------------- resolution


def test_publik_mode_resolves_secrets_block():
    write_secrets(publik={"key": "pk_live_a", "base_url": "https://gw.test/gemini"})
    endpoint = llm_mod.resolve_endpoint("publik")
    assert endpoint.provider == "publik"
    assert endpoint.key == "pk_live_a"
    assert endpoint.model == "publik-vision"
    assert endpoint.image_model == "publik-image"
    assert endpoint.url() == "https://gw.test/gemini/v1beta/models/publik-vision:generateContent"


def test_env_wins_over_secrets(monkeypatch):
    write_secrets(publik={"key": "from-disk", "base_url": "https://gw.test/gemini"})
    monkeypatch.setenv("PUBLIK_API_KEY", "from-env")
    monkeypatch.setenv("PUBLIK_API_BASE_URL", "http://localhost:3000/api/v1/gemini")
    endpoint = llm_mod.resolve_endpoint("publik")
    assert endpoint.key == "from-env"
    assert endpoint.base_url == "http://localhost:3000/api/v1/gemini"
    # The env key alone falls back to the shipped base URL rather than to the
    # file's, so a half-set env never sends a dev key at production.
    monkeypatch.delenv("PUBLIK_API_BASE_URL")
    assert llm_mod.resolve_endpoint("publik").base_url == llm_mod.PUBLIK_BASE_URL


def test_shared_file_is_last_resort(tmp_path):
    shared = config.publik_shared_file()
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text(json.dumps({"key": "pk_live_shared"}))
    assert llm_mod.resolve_endpoint("publik").key == "pk_live_shared"
    # An app-owned block always wins: the convention never overrides the store
    # the app itself wrote.
    write_secrets(publik={"key": "pk_live_own"})
    assert llm_mod.resolve_endpoint("publik").key == "pk_live_own"


def test_publik_mode_without_credential_is_actionable():
    with pytest.raises(llm_mod.LlmError) as err:
        llm_mod.resolve_endpoint("publik")
    assert "publik API isn't set up" in str(err.value)
    assert "Settings" in str(err.value)


def test_gemini_mode_untouched(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTest")
    endpoint = llm_mod.resolve_endpoint("gemini")
    assert endpoint.provider == "gemini"
    assert endpoint.key == "AIzaTest"
    assert endpoint.model == "gemini-flash-latest"
    assert endpoint.url().startswith("https://generativelanguage.googleapis.com/")
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_BASE_URL", "https://proxy.test")
    assert llm_mod.resolve_endpoint("gemini").url() == "https://proxy.test/v1beta/models/gemini-flash-latest:generateContent"
    # The env key still beats the file, as it always did.
    write_secrets(gemini_api_key="AIzaFromDisk")
    assert llm_mod.resolve_endpoint("gemini").key == "AIzaTest"
    monkeypatch.delenv("PUBLIKCLIP_GEMINI_API_KEY")
    assert llm_mod.resolve_endpoint("gemini").key == "AIzaFromDisk"


def test_publik_and_gemini_credentials_coexist():
    """The publik key never clobbers a key the user pasted, and vice versa."""
    write_secrets(gemini_api_key="AIzaMine", pexels_api_key="px", publik={"key": "pk_live_a"})
    assert llm_mod.resolve_endpoint("publik").key == "pk_live_a"
    assert llm_mod.resolve_endpoint("gemini").key == "AIzaMine"
    assert visuals.pexels_key() == "px"


# ------------------------------------------------------------------ the wire


def test_header_transport_no_query_param(monkeypatch):
    write_secrets(publik={"key": "pk_live_a", "base_url": "https://gw.test/gemini"})
    calls = capture(monkeypatch, _Res(200, ok_payload(), PUBLIK_HEADERS))
    llm_mod.make_client("publik").generate_json("p", {"type": "object"})
    sent = calls[0]
    # Google's own guidance, and the reason the gateway never has to log a URL.
    assert sent["headers"]["x-goog-api-key"] == "pk_live_a"
    assert "params" not in sent
    assert "pk_live_a" not in sent["url"]
    assert sent["url"].endswith("/v1beta/models/publik-vision:generateContent")
    assert sent["headers"]["x-publik-idempotency-key"]


def test_gemini_mode_also_uses_the_header(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTest")
    calls = capture(monkeypatch, _Res(200, ok_payload()))
    llm_mod.make_client("gemini").generate_json("p", {"type": "object"})
    assert calls[0]["headers"] == {"x-goog-api-key": "AIzaTest"}
    assert "params" not in calls[0]
    assert calls[0]["url"].endswith("/v1beta/models/gemini-flash-latest:generateContent")


def test_publik_never_names_a_google_model(monkeypatch):
    write_secrets(publik={"key": "pk_live_a"})
    calls = capture(monkeypatch, _Res(200, ok_payload(), PUBLIK_HEADERS))
    llm_mod.make_client("publik").generate_json("p", {"type": "object"})
    visuals.fetch_gemini("a fox", config.home_dir(), "publik")
    for sent in calls:
        assert "gemini-" not in sent["url"]
    assert calls[0]["url"].endswith("publik-vision:generateContent")
    assert calls[1]["url"].endswith("publik-image:generateContent")


def test_status_file_from_headers(monkeypatch):
    write_secrets(publik={"key": "pk_live_a"})
    capture(monkeypatch, _Res(200, ok_payload(), PUBLIK_HEADERS))
    llm_mod.make_client("publik").generate_json("p", {"type": "object"})
    status = read_status()
    assert status["balance_micros"] == 180_000
    assert status["starter_remaining_micros"] == 180_000
    assert status["week_budget_micros"] == "none"
    assert status["claim_state"] == "anonymous"
    assert status["needs_credit"] is False
    assert status["disconnected"] is False


def test_status_file_failure_never_fails_a_run(monkeypatch):
    write_secrets(publik={"key": "pk_live_a"})
    capture(monkeypatch, _Res(200, ok_payload(), PUBLIK_HEADERS))
    monkeypatch.setattr(config, "publik_status_path", lambda: config.home_dir() / "nope" / "s.json")
    assert llm_mod.make_client("publik").generate_json("p", {"type": "object"}) == {"verdict": "funny"}


# -------------------------------------------------------------- the refusals


def _insufficient(claim_state: str) -> dict:
    anonymous = claim_state == "anonymous"
    return {
        "error": {
            "type": "insufficient_credit",
            "message": (
                "Not enough publik credit for this request. The model behind this app is "
                "billed per use by its provider; publik passes that on and nothing is "
                "charged behind your back. "
                + ("Link this computer and pick a plan at the link below, or use your own key."
                   if anonymous
                   else "Add a plan or a pack at the link below, or use your own key.")
            ),
            "available_micros": 1240,
            "required_micros": 41000,
            "claim_state": claim_state,
            "top_up_url": "https://publikhq.com/claim/HK7F-2QWD" if anonymous else "https://publikhq.com/dashboard/api/add",
            "claim_url": "https://publikhq.com/claim/HK7F-2QWD" if anonymous else None,
            "add_credit_url": "https://publikhq.com/dashboard/api/add",
            "plans_url": "https://publikhq.com/developers#plans",
        }
    }


def test_publik_402_surfaces_exactly_one_link(monkeypatch):
    write_secrets(publik={"key": "pk_live_a"})
    capture(monkeypatch, _Res(402, _insufficient("anonymous"), PUBLIK_HEADERS))
    with pytest.raises(llm_mod.LlmError) as err:
        llm_mod.make_client("publik").generate_json("p", {"type": "object"})
    message = str(err.value)
    assert "Not enough publik credit for this request." in message
    assert "nothing is charged behind your back" in message
    assert "https://publikhq.com/claim/HK7F-2QWD" in message
    # Exactly one link: two is how a person ends up on the wrong one.
    assert message.count("https://") == 1
    status = read_status()
    assert status["needs_credit"] is True
    assert status["top_up_url"] == "https://publikhq.com/claim/HK7F-2QWD"
    assert status["claim_state"] == "anonymous"
    # Nothing was answered, so nothing may be cached as an answer.
    assert list((config.home_dir() / "llm-cache").glob("*.json")) == []


def test_publik_402_claimed_uses_add_credit_url(monkeypatch):
    write_secrets(publik={"key": "pk_live_a"})
    capture(monkeypatch, _Res(402, _insufficient("claimed"), PUBLIK_HEADERS))
    with pytest.raises(llm_mod.LlmError) as err:
        llm_mod.make_client("publik").generate_json("p", {"type": "object"})
    assert "https://publikhq.com/dashboard/api/add" in str(err.value)
    assert str(err.value).count("https://") == 1
    assert read_status()["claim_state"] == "claimed"


def test_publik_403_marks_disconnected(monkeypatch):
    write_secrets(publik={"key": "pk_live_a"})
    body = {"error": {"type": "key_revoked", "message": "revoked", "reprovision": True}}
    capture(monkeypatch, _Res(403, body, PUBLIK_HEADERS))
    with pytest.raises(llm_mod.LlmError) as err:
        llm_mod.make_client("publik").generate_json("p", {"type": "object"})
    assert "disconnected" in str(err.value)
    assert read_status()["disconnected"] is True
    assert read_status()["reprovision"] is True


def test_publik_401_is_also_disconnected(monkeypatch):
    write_secrets(publik={"key": "pk_live_a"})
    capture(monkeypatch, _Res(401, {"error": {"type": "invalid_api_key", "message": "no"}}, {}))
    with pytest.raises(llm_mod.LlmError):
        llm_mod.make_client("publik").generate_json("p", {"type": "object"})
    assert read_status()["disconnected"] is True


def test_publik_does_not_retry(monkeypatch):
    """The gateway settles real usage even after the caller hangs up, so the
    three-attempt loop would pay for the same scoring call three times."""
    write_secrets(publik={"key": "pk_live_a"})
    calls: list[str] = []

    def fake_post(url, **kwargs):
        calls.append(url)
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(llm_mod.httpx, "post", fake_post)
    with pytest.raises(llm_mod.LlmError) as err:
        llm_mod.make_client("publik").generate_json("p", {"type": "object"})
    assert len(calls) == 1
    assert "publik API call failed" in str(err.value)


def test_gemini_mode_still_retries_three_times(monkeypatch):
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaTest")
    calls: list[str] = []

    def fake_post(url, **kwargs):
        calls.append(url)
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(llm_mod.httpx, "post", fake_post)
    with pytest.raises(llm_mod.LlmError):
        llm_mod.make_client("gemini").generate_json("p", {"type": "object"})
    assert len(calls) == 3


def test_publik_429_does_not_sleep_and_retry(monkeypatch):
    write_secrets(publik={"key": "pk_live_a"})
    body = {"error": {"type": "rate_limit_exceeded", "message": "This key is limited to 60 requests per minute."}}
    calls = capture(monkeypatch, _Res(429, body, {}))
    with pytest.raises(llm_mod.LlmError) as err:
        llm_mod.make_client("publik").generate_json("p", {"type": "object"})
    assert len(calls) == 1
    # The gateway's own words reach the user: a minute limit and a spent daily
    # cap are both 429s and need different actions.
    assert "60 requests per minute" in str(err.value)


# ----------------------------------------------------------------- the cache


def test_cache_isolated_between_providers(monkeypatch):
    write_secrets(publik={"key": "pk_live_a"}, gemini_api_key="AIzaMine")
    capture(monkeypatch, _Res(200, ok_payload(), PUBLIK_HEADERS))
    llm_mod.make_client("publik").generate_json("p", {"type": "object"})
    llm_mod.make_client("gemini").generate_json("p", {"type": "object"})
    # The cache key includes the model, and publik-vision != gemini-flash-latest.
    assert len(list((config.home_dir() / "llm-cache").glob("*.json"))) == 2


def test_cache_hit_never_re_spends(monkeypatch):
    write_secrets(publik={"key": "pk_live_a"})
    calls = capture(monkeypatch, _Res(200, ok_payload(), PUBLIK_HEADERS))
    client = llm_mod.make_client("publik")
    assert client.generate_json("p", {"type": "object"}) == {"verdict": "funny"}
    assert client.generate_json("p", {"type": "object"}) == {"verdict": "funny"}
    assert len(calls) == 1


# ---------------------------------------------------------------- the images


def test_image_alias(monkeypatch):
    write_secrets(publik={"key": "pk_live_a", "base_url": "https://gw.test/gemini"})
    png = {"candidates": [{"content": {"parts": [{"inlineData": {"data": "AAAA"}}]}}]}
    calls = capture(monkeypatch, _Res(200, png, PUBLIK_HEADERS))
    monkeypatch.setattr(visuals, "httpx", llm_mod.httpx)
    path = visuals.fetch_gemini("a fox", config.home_dir(), "publik")
    assert path and path.endswith(".png")
    assert calls[0]["url"] == "https://gw.test/gemini/v1beta/models/publik-image:generateContent"
    assert calls[0]["headers"]["x-goog-api-key"] == "pk_live_a"


def test_ollama_mode_never_spends_publik(monkeypatch):
    """"Zero cost, fully offline" has to stay true of the overlay button too."""
    write_secrets(publik={"key": "pk_live_a"})
    calls = capture(monkeypatch, _Res(200, {}, {}))
    monkeypatch.setattr(visuals, "httpx", llm_mod.httpx)
    assert visuals.fetch_gemini("a fox", config.home_dir(), "ollama") is None
    assert calls == []
    # With the user's own key, an ollama job may still generate one.
    monkeypatch.setenv("PUBLIKCLIP_GEMINI_API_KEY", "AIzaMine")
    png = {"candidates": [{"content": {"parts": [{"inlineData": {"data": "AAAA"}}]}}]}
    calls2 = capture(monkeypatch, _Res(200, png, {}))
    monkeypatch.setattr(visuals, "httpx", llm_mod.httpx)
    assert visuals.fetch_gemini("a fox", config.home_dir(), "ollama")
    assert "generativelanguage.googleapis.com" in calls2[0]["url"]


def test_image_402_surfaces_rather_than_returning_none(monkeypatch):
    write_secrets(publik={"key": "pk_live_a"})
    capture(monkeypatch, _Res(402, _insufficient("anonymous"), PUBLIK_HEADERS))
    monkeypatch.setattr(visuals, "httpx", llm_mod.httpx)
    with pytest.raises(llm_mod.LlmError):
        visuals.fetch_gemini("a fox", config.home_dir(), "publik")
    assert read_status()["needs_credit"] is True


# --------------------------------------------------------------- the wiring


def test_default_brain_is_publik():
    assert config.Settings().llm_mode == "publik"
    # A job started before this patch keeps the brain it started with.
    assert config.Settings.from_json({"llm_mode": "gemini"}).llm_mode == "gemini"
    assert config.Settings.from_json({}).llm_mode == "publik"


def test_cli_accepts_publik():
    from publikclip_pipeline import cli

    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    if parser is None:
        import inspect

        source = inspect.getsource(cli)
        assert '"--llm", choices=["publik", "gemini", "ollama"]' in source
    else:  # pragma: no cover - only if the CLI grows a factory
        assert "publik" in parser.parse_args(["run", "x", "--llm", "publik"]).llm


def test_copy_rule():
    """publik's copy rule: the product is "publik API", money is in dollars,
    and "credits" is never a unit."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    forbidden = [
        ("OpenAI API access", re.compile(r"OpenAI\s+API\s+access", re.I)),
        ("ChatGPT credits", re.compile(r"ChatGPT\s+credits", re.I)),
        ("credits as a unit", re.compile(r"\b(?:\d[\d,.]*|publik|api|buy|add|your|free|more|remaining)\s+credits\b|\bcredits\s+(?:left|remaining|balance|pack)\b", re.I)),
    ]
    scanned = 0
    violations = []
    for path in [*root.glob("pipeline/publikclip_pipeline/**/*.py"), *root.glob("app/src/**/*.tsx"), *root.glob("app/src/**/*.ts"), root / "README.md"]:
        if not path.is_file() or path.name == "test_publik.py":
            continue
        scanned += 1
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            for name, pattern in forbidden:
                if pattern.search(line):
                    violations.append(f"{path.relative_to(root)}:{i} [{name}] {line.strip()[:90]}")
    assert scanned > 20
    assert violations == []
