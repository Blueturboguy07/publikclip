"""LLM backends: publik API (default), Gemini (your own key) and Ollama
(local fallback).

One interface: generate_json(prompt, schema, images) → dict, with disk
caching keyed on (backend, model, prompt, schema) so re-runs never re-spend
— the M2 gate requires cache hits on identical inputs.

Key resolution, publik mode: PUBLIK_API_KEY env var, then
PUBLIKCLIP_HOME/secrets.json {"publik": {...}} (written by onboarding), then
the shared per-app file publik's convention names. Gemini mode:
PUBLIKCLIP_GEMINI_API_KEY env var, then secrets.json {"gemini_api_key"}.
Ollama needs no key — just a running daemon.

The wire format is Gemini's in both cloud modes. `provider` is what says
whose key rides on the header and who gets billed: publik serves the same
`:generateContent` shape under its own alias model names, so the only things
that change between the two are the base URL, the model name and the error
handling.
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
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"
GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image"

# publik: alias names only. The gateway owns which Gemini model sits behind
# each alias and 404s anything else, which is what keeps the proxy from being
# "an API Client that functions substantially the same as the APIs" under
# Google's terms. Never send a gemini-* slug on a publik call.
PUBLIK_BASE_URL = "https://publikhq.com/api/v1/gemini"
PUBLIK_VISION_MODEL = "publik-vision"
PUBLIK_IMAGE_MODEL = "publik-image"

GENERATE_PATH = "/v1beta/models/{model}:generateContent"
OLLAMA_URL = "http://localhost:11434"
LLM_TIMEOUT = 120.0

# The x-publik-* headers every metered answer carries, and the status-file
# field each one becomes.
PUBLIK_HEADER_FIELDS = {
    "x-publik-balance": "balance_micros",
    "x-publik-week-used": "week_used_micros",
    "x-publik-week-budget": "week_budget_micros",
    "x-publik-week-resets-at": "week_resets_at",
    "x-publik-claim-state": "claim_state",
    "x-publik-starter-remaining": "starter_remaining_micros",
}


class LlmError(Exception):
    """User-actionable LLM failure (bad key, daemon down, model missing)."""


@dataclass(frozen=True)
class Endpoint:
    """Where a generateContent call goes. The wire format is Gemini's either
    way; `provider` says whose key is on the header and who is billed."""

    provider: str  # 'publik' | 'gemini'
    base_url: str
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
    return _secrets().get("gemini_api_key")


def publik_credential() -> dict | None:
    """{"key", "base_url", "models"?} or None. Env first so a developer can
    point one run at a local gateway without touching the file the app wrote."""
    key = os.environ.get("PUBLIK_API_KEY")
    if key:
        return {
            "key": key,
            "base_url": os.environ.get("PUBLIK_API_BASE_URL") or PUBLIK_BASE_URL,
        }
    block = _secrets().get("publik")
    if isinstance(block, dict) and block.get("key"):
        return block
    shared = config.publik_shared_file()
    try:
        if shared.exists():
            data = json.loads(shared.read_text())
            if isinstance(data, dict) and data.get("key"):
                return data
    except (json.JSONDecodeError, OSError):
        return None
    return None


def resolve_endpoint(llm_mode: str) -> Endpoint:
    if llm_mode == "publik":
        cred = publik_credential()
        if not cred:
            raise LlmError(
                "publik API isn't set up on this computer. Finish onboarding, "
                "or switch to your own Gemini key or Ollama in Settings."
            )
        models = cred.get("models") or {}
        return Endpoint(
            "publik",
            cred.get("base_url") or PUBLIK_BASE_URL,
            cred["key"],
            models.get("vision", PUBLIK_VISION_MODEL),
            models.get("image", PUBLIK_IMAGE_MODEL),
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
        key,
        GEMINI_MODEL,
        GEMINI_IMAGE_MODEL,
    )


def _record_publik_status(res: httpx.Response, extra: dict | None = None) -> None:
    """Balance line + 402 state for the UI, from the response the call just
    made. Best effort: a status file that can't be written must never fail a
    scoring run."""
    status: dict[str, Any] = {
        "updated_at": time.time(),
        "needs_credit": False,
        "disconnected": False,
    }
    for header, field in PUBLIK_HEADER_FIELDS.items():
        value = res.headers.get(header)
        if value is None:
            continue
        status[field] = int(value) if field.endswith("_micros") and value.isdigit() else value
    if extra:
        status.update(extra)
    try:
        config.publik_status_path().write_text(json.dumps(status))
    except OSError:
        pass


def _publik_error(res: httpx.Response) -> LlmError:
    """The gateway's refusals, turned into one sentence a person can act on.

    The 402 rule (publik's contract): render the message and EXACTLY ONE
    link, top_up_url — the claim page while this computer is anonymous, the
    add-credit page once it is linked. Two links is how a person ends up on
    the wrong one.
    """
    try:
        err = res.json()["error"]
    except Exception:  # noqa: BLE001
        err = {}
    if not isinstance(err, dict):
        err = {}
    claim_state = err.get("claim_state")
    if res.status_code == 402:
        link = err.get("top_up_url") or err.get("claim_url") or err.get("add_credit_url")
        _record_publik_status(
            res,
            {
                "needs_credit": True,
                "claim_url": err.get("claim_url"),
                "add_credit_url": err.get("add_credit_url"),
                "top_up_url": link,
                "claim_state": claim_state,
            },
        )
        lead = err.get("message") or (
            "Not enough publik credit for this request. The model behind this app is "
            "billed per use by its provider; publik passes that on at the provider's "
            "list price with no markup, and nothing is charged behind your back."
        )
        return LlmError(f"{lead} {link}" if link else lead)
    if res.status_code in (401, 403):
        _record_publik_status(
            res, {"disconnected": True, "reprovision": bool(err.get("reprovision"))}
        )
        return LlmError(
            "publik API is disconnected on this computer. Reconnect in Settings, "
            "or use your own Gemini key."
        )
    return LlmError(f"publik API {res.status_code}: {err.get('message', 'request failed')}")


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
    # Deliberately still "gemini": the T2 vision pass and the confidence label
    # gate on this string (scoring/stage.py), and both are true of publik mode,
    # which serves the same model class over the same wire format.
    backend = "gemini"

    def __init__(self, endpoint: Endpoint):
        self.endpoint = endpoint
        self.model = endpoint.model
        self.provider = endpoint.provider

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
        # Google documents both transports and warns against the query one:
        # "To include an API key with a REST API call, use the x-goog-api-key
        # HTTP header" … the key parameter "includes your API key in the URL,
        # exposing your key to theft through URL scans". Header, then, for
        # both providers.
        headers = {"x-goog-api-key": self.endpoint.key}
        if self.provider == "publik":
            # A retried call is the same call. The gateway settles real usage
            # even after the client hangs up, so publik mode makes exactly one
            # attempt and hands the idempotency key over in case the gateway
            # can dedupe a network-level retry it never saw us make.
            headers["x-publik-idempotency-key"] = key
        attempts = 1 if self.provider == "publik" else 3
        last_err: Exception | None = None
        for attempt in range(attempts):
            try:
                res = httpx.post(
                    self.endpoint.url(),
                    headers=headers,
                    json=body,
                    timeout=LLM_TIMEOUT,
                )
                if self.provider == "publik":
                    if res.status_code in (401, 402, 403):
                        raise _publik_error(res)
                    _record_publik_status(res)
                elif res.status_code in (401, 403):
                    raise LlmError("Gemini rejected the API key. Check it in Settings.")
                if res.status_code == 429:
                    # Surface the API's own words — a quota backoff and a
                    # billing stop look identical as bare 429s but need
                    # opposite user actions. publik's envelope is also
                    # {"error": {"message": …}}, so this reads either way.
                    try:
                        detail = res.json()["error"]["message"]
                    except Exception:  # noqa: BLE001
                        detail = "rate limited"
                    who = "publik API" if self.provider == "publik" else "Gemini"
                    last_err = LlmError(f"{who} 429: {detail}")
                    if self.provider == "publik" or "credit" in detail.lower() or "billing" in detail.lower():
                        raise last_err
                    time.sleep(4 * (attempt + 1))
                    continue
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
        who = "publik API" if self.provider == "publik" else "Gemini"
        raise LlmError(f"{who} call failed: {last_err}")


class OllamaClient:
    backend = "ollama"

    def __init__(self, model: str | None = None):
        try:
            res = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5.0)
            res.raise_for_status()
        except httpx.HTTPError as err:
            raise LlmError(
                "Ollama isn't running. Start it (`ollama serve`) or switch to publik API "
                "or your own Gemini key."
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
