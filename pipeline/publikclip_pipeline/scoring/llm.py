"""LLM backends: auto (production ladder), Bedrock, our gateway, OpenRouter,
and Ollama (local fallback).

One interface: generate_json(prompt, schema, images) → dict, with disk
caching keyed on (backend, model, prompt, schema, images) so re-runs never
re-spend — the M2 gate requires cache hits on identical inputs.

Production ladder (llm_mode "auto", the default):
1. bedrock      — AWS founder credits, boto3 default chain (local: `aws login`)
2. gateway      — our RHOBEAR agent gateway (OpenAI-compatible, 127.0.0.1:8780)
3. openrouter   — BYO key, last-resort remote
4. (never) ollama — explicitly selected only

Key resolution per backend:
- bedrock: boto3 default credential chain (no key arguments, matches the
  RHOBEAR gateway seam). Model from PUBLIKCLIP_BEDROCK_MODEL (default
  amazon.nova-pro-v1:0), region from PUBLIKCLIP_BEDROCK_REGION or AWS_REGION.
- gateway: PUBLIKCLIP_GATEWAY_URL (default http://127.0.0.1:8780/v1), key
  from RHOBEAR_GATEWAY_KEY env or secrets.json "rhobear_gateway_key", model
  from PUBLIKCLIP_GATEWAY_MODEL (default claude-cli/opus-4-8 — subscription
  route through the router).
- openrouter: PUBLIKCLIP_OPENROUTER_API_KEY env var, then
  PUBLIKCLIP_HOME/secrets.json {"openrouter_api_key": "..."}. Model from
  PUBLIKCLIP_OPENROUTER_MODEL (default z-ai/glm-4.5v — vision + strong JSON).
- ollama: no key — just a running daemon.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx

from .. import config

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = os.environ.get("PUBLIKCLIP_OPENROUTER_MODEL", "z-ai/glm-4.5v")
BEDROCK_MODEL = os.environ.get("PUBLIKCLIP_BEDROCK_MODEL", "amazon.nova-pro-v1:0")
GATEWAY_URL = os.environ.get("PUBLIKCLIP_GATEWAY_URL", "http://127.0.0.1:8780/v1")
GATEWAY_MODEL = os.environ.get("PUBLIKCLIP_GATEWAY_MODEL", "claude-cli/opus-4-8")
OLLAMA_URL = "http://localhost:11434"
LLM_TIMEOUT = 120.0


class LlmError(Exception):
    """User-actionable LLM failure (bad key, daemon down, model missing)."""


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


def _required_keys(schema: dict) -> list[str]:
    return [k for k in schema.get("required", []) if isinstance(k, str)]


def _validated(data: Any, schema: dict) -> dict:
    """Reject LLM responses that miss schema-required keys.

    Some models echo the schema definition back instead of answering with
    values (observed with GLM-4.5V on image calls). A schema-shaped dict has
    "properties"/"required"/"type" and none of the required fields — treat
    it as a failed call, never as a score.
    """
    if not isinstance(data, dict):
        raise LlmError("LLM response is not a JSON object")
    required = _required_keys(schema)
    if required and not all(k in data for k in required):
        raise LlmError(f"LLM response missing required keys: {sorted(set(required) - set(data))}")
    if "properties" in data and "type" in data and not required:
        raise LlmError("LLM echoed the schema instead of answering")
    return data


class OllamaClient:
    backend = "ollama"
    supports_vision = False

    def __init__(self, model: str | None = None):
        try:
            res = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5.0)
            res.raise_for_status()
        except httpx.HTTPError as err:
            raise LlmError(
                "Ollama isn't running. Start it (`ollama serve`) or switch to openrouter/bedrock mode."
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


def _secrets_value(key_name: str) -> str | None:
    """Read a named secret from PUBLIKCLIP_HOME/secrets.json."""
    secrets_path = config.home_dir() / "secrets.json"
    if secrets_path.exists():
        try:
            return json.loads(secrets_path.read_text()).get(key_name)
        except (json.JSONDecodeError, OSError):
            return None
    return None


class OpenRouterClient:
    """OpenAI-compatible chat completions via OpenRouter (BYO key).

    Vision-capable by default: images are embedded as data URLs, and the
    selected model is expected to accept them (z-ai/glm-4.5v does). JSON is
    requested via response_format json_object plus schema-in-prompt.
    """

    backend = "openrouter"
    supports_vision = True

    def __init__(self, model: str | None = None):
        self.model = model or OPENROUTER_MODEL
        key = os.environ.get("PUBLIKCLIP_OPENROUTER_API_KEY") or _secrets_value("openrouter_api_key")
        if not key:
            raise LlmError(
                "No OpenRouter API key found. Add one in Settings (or set "
                "PUBLIKCLIP_OPENROUTER_API_KEY), or switch to Bedrock/Ollama mode."
            )
        self._key = key

    def generate_json(
        self, prompt: str, schema: dict, images: list[bytes] | None = None
    ) -> dict:
        images = images or []
        cache_file = _cache_dir() / f"{_cache_key(self.backend, self.model, prompt, schema, images)}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text())

        content: list[dict[str, Any]] = []
        text = f"{prompt}\n\nEmit ONLY valid JSON matching this schema: {json.dumps(schema, sort_keys=True)}"
        content.append({"type": "text", "text": text})
        for img in images:
            import base64

            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(img).decode()}"},
            })
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 2000,
        }
        last_err: Exception | None = None
        retried_with_anti_echo = False
        for attempt in range(3):
            try:
                body = dict(body)
                if retried_with_anti_echo:
                    body["messages"] = [{
                        "role": "user",
                        "content": [{
                            "type": "text",
                            "text": text + " IMPORTANT: answer with the values only — never repeat the schema definition.",
                        }] + content[1:],
                    }]
                res = httpx.post(
                    OPENROUTER_URL,
                    headers={"Authorization": f"Bearer {self._key}"},
                    json=body,
                    timeout=LLM_TIMEOUT,
                )
                if res.status_code in (401, 403):
                    raise LlmError("OpenRouter rejected the API key. Check it in Settings.")
                if res.status_code == 402:
                    raise LlmError("OpenRouter has no credits left on this key — top up at openrouter.ai.")
                if res.status_code == 429:
                    import time

                    retry_after = res.headers.get("retry-after")
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else 4 * (attempt + 1)
                    time.sleep(wait)
                    continue
                res.raise_for_status()
                payload = res.json()
                # OpenRouter reports account-level problems inside a 200 body.
                if payload.get("error"):
                    raise LlmError(f"OpenRouter: {payload['error']}")
                text = payload["choices"][0]["message"]["content"]
                data = json.loads(_strip_fences(text))
                try:
                    data = _validated(data, schema)
                except LlmError:
                    if not retried_with_anti_echo:
                        retried_with_anti_echo = True
                        continue
                    raise
                cache_file.write_text(json.dumps(data))
                return data
            except LlmError:
                raise
            except (httpx.HTTPError, KeyError, json.JSONDecodeError, IndexError) as err:
                last_err = err
        raise LlmError(f"OpenRouter call failed after retries: {last_err}")


class BedrockClient:
    """Amazon Bedrock via the AWS SDK default credential chain.

    No key arguments on purpose: local resolves `aws login`; the VPS/CI
    resolves an attached role. Default model amazon.nova-pro-v1:0 is a
    text+vision model inside AWS founder credits; override with
    PUBLIKCLIP_BEDROCK_MODEL (e.g. a registry-approved GLM id).
    """

    backend = "bedrock"
    supports_vision = True

    def __init__(self, model: str | None = None):
        self.model = model or BEDROCK_MODEL
        try:
            import boto3  # noqa: PLC0415 — lazy so Ollama-only installs never need AWS SDK
        except ImportError as err:
            raise LlmError(
                "boto3 is not installed — add it to the pipeline environment to use "
                "Bedrock (or switch to openrouter/ollama mode)."
            ) from err
        self._boto3 = boto3

    def generate_json(
        self, prompt: str, schema: dict, images: list[bytes] | None = None
    ) -> dict:
        images = images or []
        cache_file = _cache_dir() / f"{_cache_key(self.backend, self.model, prompt, schema, images)}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text())

        content: list[dict[str, Any]] = [{"text": prompt}]
        for img in images:
            content.append({"image": {"format": "jpeg", "source": {"bytes": img}}})
        messages = [{"role": "user", "content": content}]
        try:
            client = self._boto3.client(
                "bedrock-runtime",
                region_name=os.environ.get("PUBLIKCLIP_BEDROCK_REGION") or os.environ.get("AWS_REGION", "us-east-1"),
            )
            resp = client.converse(
                modelId=self.model,
                messages=messages,
                inferenceConfig={"temperature": 0.2, "maxTokens": 2000},
            )
            text = resp["output"]["message"]["content"][0]["text"]
            data = _validated(json.loads(_strip_fences(text)), schema)
        except LlmError:
            raise
        except Exception as err:  # noqa: BLE001 — surface SDK/auth/parse failures as one actionable error
            raise LlmError(
                f"Bedrock call failed ({self.model}): {err}. Run `aws login` locally "
                "or set an attached role on the host."
            ) from err
        cache_file.write_text(json.dumps(data))
        return data


def make_client(llm_mode: str):
    if llm_mode == "gemini":  # jobs created before the provider swap
        llm_mode = "openrouter"
    if llm_mode == "auto":
        return AutoClient()
    if llm_mode == "ollama":
        return OllamaClient()
    if llm_mode == "openrouter":
        return OpenRouterClient()
    if llm_mode == "gateway":
        return GatewayClient()
    if llm_mode == "bedrock":
        return BedrockClient()
    raise LlmError(
        f"Unknown llm_mode {llm_mode!r} — use auto, bedrock, gateway, openrouter, or ollama."
    )


class GatewayClient:
    """Our RHOBEAR agent gateway — OpenAI-compatible chat completions.

    This is the "our agents" production line: the router normalizes slugs
    like claude-cli/opus-4-8 onto the Claude subscription and Bedrock routes.
    """

    backend = "gateway"
    supports_vision = True

    def __init__(self, model: str | None = None):
        self.model = model or GATEWAY_MODEL
        self.base_url = GATEWAY_URL.rstrip("/")
        key = os.environ.get("RHOBEAR_GATEWAY_KEY") or _secrets_value("rhobear_gateway_key")
        if not key:
            raise LlmError(
                "No RHOBEAR_GATEWAY_KEY found — the gateway needs its key (set the env var "
                "or add \"rhobear_gateway_key\" to secrets.json)."
            )
        self._key = key

    def generate_json(
        self, prompt: str, schema: dict, images: list[bytes] | None = None
    ) -> dict:
        images = images or []
        cache_file = _cache_dir() / f"{_cache_key(self.backend, self.model, prompt, schema, images)}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text())

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            import base64

            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(img).decode()}"},
            })
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 2000,
        }
        try:
            res = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._key}"},
                json=body,
                timeout=LLM_TIMEOUT,
            )
            if res.status_code in (401, 403):
                raise LlmError("RHOBEAR gateway rejected the key.")
            res.raise_for_status()
            payload = res.json()
            if payload.get("error"):
                raise LlmError(f"RHOBEAR gateway: {payload['error']}")
            text = payload["choices"][0]["message"]["content"]
            data = _validated(json.loads(_strip_fences(text)), schema)
        except LlmError:
            raise
        except (httpx.HTTPError, KeyError, json.JSONDecodeError, IndexError, TypeError) as err:
            raise LlmError(f"RHOBEAR gateway call failed: {err}") from err
        cache_file.write_text(json.dumps(data))
        return data


class AutoClient:
    """Production ladder: bedrock → gateway → openrouter, per call.

    Backends are constructed once; a failing call falls through to the next
    backend and the working one keeps serving for the rest of the job.
    """

    backend = "auto"
    supports_vision = True

    def __init__(self):
        self._clients: list = []
        self._errors: list[str] = []
        self._active: str | None = None
        for factory in (BedrockClient, GatewayClient, OpenRouterClient):
            try:
                self._clients.append(factory())
            except LlmError as err:
                self._errors.append(str(err))
        if not self._clients:
            raise LlmError("no LLM backends available: " + "; ".join(self._errors))

    @property
    def model(self) -> str:
        """Audit label: which backend actually served (or the ladder)."""
        if self._active:
            return f"auto→{self._active}"
        return "auto(bedrock→gateway→openrouter)"

    def generate_json(
        self, prompt: str, schema: dict, images: list[bytes] | None = None
    ) -> dict:
        errors = list(self._errors)
        for client in self._clients:
            try:
                result = client.generate_json(prompt, schema, images)
                self._active = client.backend
                return result
            except LlmError as err:
                errors.append(f"{client.backend}: {err}")
        raise LlmError("all LLM backends failed: " + "; ".join(errors))
