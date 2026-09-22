"""LLM backends: publik API (default), Gemini (your own key) and Ollama
(local fallback).

One interface: generate_json(prompt, schema, images) → dict, with disk
caching keyed on (backend, model, prompt, schema) so re-runs never re-spend
— the M2 gate requires cache hits on identical inputs.

publik API and your own Gemini key speak the same wire format (Gemini's
generateContent); they differ in where the call goes, whose key rides the
x-goog-api-key header, and who is billed. That difference is an Endpoint.

Key resolution, publik mode: PUBLIK_API_KEY env var (+ PUBLIK_API_BASE_URL),
then PUBLIKCLIP_HOME/secrets.json {"publik": {...}} (written by the app's
onboarding), then the shared per-app file publik's convention names.
Gemini mode: PUBLIKCLIP_GEMINI_API_KEY env var, then secrets.json
{"gemini_api_key": "..."}. Ollama needs no key — just a running daemon.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .. import config

# The rolling alias, deliberately: Google retires pinned models for NEW api
# keys while still advertising them in ListModels (learned live — 404 "no
# longer available to new users" on gemini-2.5-flash with a fresh key).
GEMINI_MODEL = "gemini-flash-latest"
GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"
# publik API: alias names only — the gateway owns which model sits behind
# them; never send a gemini-* slug on a publik call. The stored base URL is
# the gateway root (what POST /installs returns); the Gemini dialect lives
# under /gemini on it.
PUBLIK_BASE_URL = "https://publikhq.com/api/v1"
PUBLIK_DIALECT_PATH = "/gemini"
PUBLIK_VISION_MODEL = "publik-vision"
PUBLIK_IMAGE_MODEL = "publik-image"
GENERATE_PATH = "/v1beta/models/{model}:generateContent"
OLLAMA_URL = "http://localhost:11434"
LLM_TIMEOUT = 120.0

# x-publik-* response header → field in publik-status.json (the UI's balance
# line). Integer-micros fields are parsed; everything else is kept verbatim.
PUBLIK_HEADER_FIELDS = {
    "x-publik-balance": "balance_micros",
    "x-publik-charge-micros": "last_charge_micros",
    "x-publik-request-id": "last_request_id",
    "x-publik-claim-state": "claim_state",
    "x-publik-starter-remaining": "starter_remaining_micros",
    "x-publik-week-used": "week_used_micros",
    "x-publik-week-budget": "week_budget_micros",
    "x-publik-week-resets-at": "week_resets_at",
}


class LlmError(Exception):
    """User-actionable LLM failure (bad key, daemon down, model missing)."""


class PublikStopError(LlmError):
    """publik API said stop: out of balance (402) or this computer's key no
    longer works (401/403). Every further call would fail the same way, so
    callers that normally swallow optional-evidence failures re-raise it."""


@dataclass(frozen=True)
class Endpoint:
    """Where a generateContent call goes. The wire format is Gemini's either
    way; `provider` says whose key is on the header and who is billed."""

    provider: str  # 'publik' | 'gemini'
    base_url: str  # everything before /v1beta/...
    key: str
    model: str
    image_model: str

    def url(self, model: str | None = None) -> str:
        return self.base_url.rstrip("/") + GENERATE_PATH.format(model=model or self.model)


def _secrets() -> dict:
    path = config.home_dir() / "secrets.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def gemini_api_key() -> str | None:
    key = os.environ.get("PUBLIKCLIP_GEMINI_API_KEY")
    if key:
        return key
    return _secrets().get("gemini_api_key") or None


def publik_credential() -> dict | None:
    """The publik API credential block, or None. Env first (a terminal run
    needs no GUI), then this app's own store, then the shared per-app file."""
    key = os.environ.get("PUBLIK_API_KEY")
    if key:
        return {"key": key, "base_url": os.environ.get("PUBLIK_API_BASE_URL") or PUBLIK_BASE_URL}
    block = _secrets().get("publik")
    if isinstance(block, dict) and block.get("key"):
        return block
    shared = config.publik_shared_file()
    if shared.exists():
        try:
            data = json.loads(shared.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        if isinstance(data, dict) and data.get("key"):
            return data
    return None


def publik_dialect_base(base_url: str | None) -> str:
    """Gateway root → the Gemini dialect's base. Tolerates a base that already
    names the dialect (…/api/v1/gemini) so neither spelling double-appends."""
    base = (base_url or PUBLIK_BASE_URL).rstrip("/")
    return base if base.endswith(PUBLIK_DIALECT_PATH) else base + PUBLIK_DIALECT_PATH


def resolve_endpoint(llm_mode: str) -> Endpoint:
    if llm_mode == "publik":
        cred = publik_credential()
        if not cred:
            raise LlmError(
                "publik API isn't set up on this computer. Finish onboarding, "
                "or switch to your own Gemini key or Ollama in Settings."
            )
        models = cred.get("models") if isinstance(cred.get("models"), dict) else {}
        return Endpoint(
            "publik",
            publik_dialect_base(cred.get("base_url")),
            str(cred["key"]).strip(),
            models.get("vision") or PUBLIK_VISION_MODEL,
            models.get("image") or PUBLIK_IMAGE_MODEL,
        )
    key = gemini_api_key()
    if not key:
        raise LlmError(
            "No Gemini API key found. Add one in Settings (or set "
            "PUBLIKCLIP_GEMINI_API_KEY), or switch to publik API or Ollama mode."
        )
    return Endpoint(
        "gemini",
        os.environ.get("PUBLIKCLIP_GEMINI_BASE_URL") or GEMINI_BASE_URL,
        key.strip(),
        GEMINI_MODEL,
        GEMINI_IMAGE_MODEL,
    )


def _read_publik_status() -> dict:
    try:
        data = json.loads(config.publik_status_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _record_publik_status(res: httpx.Response, extra: dict | None = None) -> None:
    """Balance line + 402/401 state for the UI, from the response the call
    just made. Merged over the previous file so a header this response did
    not carry (starter-remaining disappears at 0) keeps its last value only
    when that is honest. Best effort: a status file that can't be written must
    never fail a scoring run."""
    status: dict[str, Any] = _read_publik_status()
    status.update({"updated_at": time.time(), "needs_credit": False, "disconnected": False})
    status.pop("message", None)
    headers = getattr(res, "headers", None) or {}
    if "x-publik-balance" in headers and "x-publik-starter-remaining" not in headers:
        status["starter_remaining_micros"] = 0
    for header, field in PUBLIK_HEADER_FIELDS.items():
        value = headers.get(header)
        if value is None:
            continue
        if field.endswith("_micros") and str(value).lstrip("-").isdigit():
            status[field] = int(value)
        else:
            status[field] = value
    if extra:
        status.update(extra)
    try:
        path = config.publik_status_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status))
    except OSError:
        pass


def _error_message(res: httpx.Response) -> str:
    try:
        return str(res.json()["error"]["message"])
    except Exception:  # noqa: BLE001
        return "request failed"


def _publik_error(res: httpx.Response) -> PublikStopError:
    """401/402/403 from the gateway → the status file + one actionable line.
    A 402 shows the gateway's own message and exactly one link: top_up_url."""
    try:
        err = res.json()["error"]
        if not isinstance(err, dict):
            err = {}
    except Exception:  # noqa: BLE001
        err = {}
    message = str(err.get("message") or "").strip()
    if res.status_code == 402:
        top_up = err.get("top_up_url")
        _record_publik_status(
            res,
            {
                "needs_credit": True,
                "top_up_url": top_up,
                "claim_state": err.get("claim_state"),
                "message": message or None,
            },
        )
        text = message or "publik API needs more balance for this request."
        if not text.lower().startswith("publik api"):
            text = f"publik API: {text}"
        link = f" {top_up}" if top_up else ""
        return PublikStopError(f"{text}{link} — or switch to your own Gemini key in Settings.")
    # 401 invalid_api_key / key_revoked, 403 install_revoked: the key on
    # this computer no longer works; the app re-provisions from Settings.
    _record_publik_status(
        res,
        {
            "disconnected": True,
            "reprovision": res.status_code == 401 or bool(err.get("reprovision")),
            "error_type": err.get("type"),
        },
    )
    return PublikStopError(
        "publik API is disconnected on this computer. Reconnect it in Settings, "
        "or use your own Gemini key."
    )


def _cache_dir() -> Path:
    path = config.home_dir() / "llm-cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_key(backend: str, model: str, prompt: str, schema: dict, images: list[bytes]) -> str:
    h = hashlib.sha256()
    h.update(backend.encode())
    h.update(model.encode())
    h.update(prompt.encode())
    h.update(json.dumps(schema, sort_keys=True).encode())
    for img in images:
        h.update(hashlib.sha256(img).digest())
    return h.hexdigest()[:32]


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


class GeminiClient:
    """Gemini's generateContent wire format, against either publik API or
    Google with the user's own key. `backend` stays "gemini" for both: the
    T2 vision pass and the confidence label key on it (scoring/stage.py), and
    both are true for publik. `provider` says who is billed."""

    backend = "gemini"

    def __init__(self, endpoint: Endpoint | None = None):
        self.endpoint = endpoint or resolve_endpoint("gemini")
        self.model = self.endpoint.model
        self.provider = self.endpoint.provider

    def generate_json(
        self, prompt: str, schema: dict, images: list[bytes] | None = None
    ) -> dict:
        images = images or []
        key = _cache_key(self.backend, self.model, prompt, schema, images)
        cache_file = _cache_dir() / f"{key}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text())

        parts: list[dict[str, Any]] = [{"text": prompt}]
        for img in images:
            import base64

            parts.append(
                {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(img).decode()}}
            )
        body = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "temperature": 0.2,
            },
        }
        # Header transport, never ?key= — a key in the URL leaks through
        # logs and URL scans (Google's own guidance).
        headers = {"x-goog-api-key": self.endpoint.key}
        publik = self.provider == "publik"
        if publik:
            # A repeated call is the same call: the gateway can dedupe on it.
            headers["x-publik-idempotency-key"] = key
        # publik API settles real usage even after a client gives up, so a
        # timed-out publik call is final — retrying could pay for it twice.
        attempts = 1 if publik else 3
        who = "publik API" if publik else "Gemini"
        last_err: Exception | None = None
        for attempt in range(attempts):
            try:
                res = httpx.post(
                    self.endpoint.url(),
                    headers=headers,
                    json=body,
                    timeout=LLM_TIMEOUT,
                )
                if publik:
                    if res.status_code in (401, 402, 403):
                        raise _publik_error(res)
                    _record_publik_status(res)
                elif res.status_code in (401, 403):
                    raise LlmError("Gemini rejected the API key. Check it in Settings.")
                if res.status_code == 429:
                    # Surface the API's own words — a quota backoff and a
                    # "credits depleted" billing stop look identical as bare
                    # 429s but need opposite user actions. publik's error
                    # envelope carries error.message too.
                    try:
                        detail = res.json()["error"]["message"]
                    except Exception:  # noqa: BLE001
                        detail = "rate limited"
                    last_err = LlmError(f"{who} 429: {detail}")
                    if publik or "credit" in detail.lower() or "billing" in detail.lower():
                        raise last_err
                    time.sleep(4 * (attempt + 1))
                    continue
                if publik and res.status_code >= 400:
                    raise LlmError(f"publik API {res.status_code}: {_error_message(res)}")
                res.raise_for_status()
                payload = res.json()
                text = payload["candidates"][0]["content"]["parts"][0]["text"]
                data = json.loads(_strip_fences(text))
                cache_file.write_text(json.dumps(data))
                return data
            except LlmError:
                raise
            except (httpx.HTTPError, KeyError, json.JSONDecodeError, IndexError) as err:
                last_err = err
        if attempts == 1:
            raise LlmError(f"{who} call failed: {last_err}")
        raise LlmError(f"{who} call failed after retries: {last_err}")


class OllamaClient:
    backend = "ollama"

    def __init__(self, model: str | None = None):
        try:
            res = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5.0)
            res.raise_for_status()
        except httpx.HTTPError as err:
            raise LlmError(
                "Ollama isn't running. Start it (`ollama serve`) or switch to publik API or your own Gemini key."
            ) from err
        models = [m["name"] for m in res.json().get("models", [])]
        if not models:
            raise LlmError("Ollama has no models. Pull one, e.g. `ollama pull llama3.1:8b`.")
        self.model = model if model in models else _pick_ollama_model(models)

    def generate_json(
        self, prompt: str, schema: dict, images: list[bytes] | None = None
    ) -> dict:
        if images:
            # Text-only fallback: the caller records visual as signals_missing.
            images = []
        cache_file = _cache_dir() / f"{_cache_key(self.backend, self.model, prompt, schema, [])}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text())
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0.1},
        }
        try:
            res = httpx.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=600.0)
            res.raise_for_status()
            data = json.loads(_strip_fences(res.json()["message"]["content"]))
        except (httpx.HTTPError, KeyError, json.JSONDecodeError) as err:
            raise LlmError(f"Ollama call failed: {err}") from err
        cache_file.write_text(json.dumps(data))
        return data


def _pick_ollama_model(models: list[str]) -> str:
    """Prefer capable general models, and among them the LARGEST — list
    order once handed us qwen2.5:3b while 7b sat right there."""
    import re

    def size_of(name: str) -> float:
        m = re.search(r"(\d+(?:\.\d+)?)b", name.lower())
        return float(m.group(1)) if m else 0.0

    candidates = [
        name
        for prefix in ("llama3.1", "llama3", "qwen2.5", "qwen3", "mistral", "gemma2", "gemma3")
        for name in models
        if name.startswith(prefix)
    ]
    if candidates:
        return max(candidates, key=size_of)
    return models[0]


def make_client(llm_mode: str):
    if llm_mode == "ollama":
        return OllamaClient()
    return GeminiClient(resolve_endpoint(llm_mode))
