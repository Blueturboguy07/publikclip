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
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from .. import config

# The rolling alias, deliberately: Google retires pinned models for NEW api
# keys while still advertising them in ListModels (learned live — 404 "no
# longer available to new users" on gemini-2.5-flash with a fresh key).
GEMINI_MODEL = "gemini-flash-latest"
# Tried in order when Google keeps answering the model above with a 5xx
# ("the model is overloaded"), a 404 (retired for this key) or a quota 429.
# Free-tier keys are shed from the flagship flash alias first, and a
# brand-new key stayed shed for three days straight (Windows report, Oct 7
# 2026: "Gemini call failed after retries: Server error '503'") while the
# lite aliases answered the same key in about a second. Each alias has its
# own quota, so a lite model is a different queue, not the same one again.
GEMINI_FALLBACK_MODELS = (
    "gemini-flash-lite-latest",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
)
# Seconds to wait before the 2nd and 3rd try on the same model. The old loop
# retried instantly, so an overloaded model took three hits inside a second
# and the job failed before Google could have let the next request through.
GEMINI_RETRY_WAITS = (3.0, 8.0)
# Free-tier quotas (AI Studio, Oct 13 2026): the flash alias allows 5 requests
# a minute and 20 a day, the lite models 10 a minute and 500 a day. Quotas are
# per Google project; the daily ones reset at midnight Pacific. A 429 says
# which quota it hit (QuotaFailure.violations[].quotaId ends in ...PerMinute...
# or ...PerDay...) and how long to wait (RetryInfo.retryDelay, "15s"). Oct 11
# 2026, Windows report with a free key: Google's standard quota text says
# "check your plan and billing details", the old code read "billing" as a
# billing stop, so JUDGE died on the first per-minute hit; each restart spent
# more of the flash alias's 20 a day until it answered "retry in 11 hours",
# while the lite models sat unused with 500 a day each.
QUOTA_WAIT_CAP = 90.0        # longest single wait for a per-minute quota
QUOTA_WAITS_PER_CALL = 4     # per-minute waits on one model before trying the next
QUOTA_DEFAULT_WAIT = 15.0    # when the 429 does not say how long
QUOTA_DEFAULT_RPM = 5        # free-tier flash, when the 429 does not name the limit
QUOTA_DAY_THRESHOLD = 600.0  # an unlabeled 429 asking for a longer wait is a daily quota
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


class QuotaExhaustedError(LlmError):
    """Own-key Gemini: today's free requests are used up on every model the
    ladder has. Nothing answers until Google's daily reset; the job resumes
    from its cache then."""


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


def _error_message(res: httpx.Response, default: str = "request failed") -> str:
    try:
        return str(res.json()["error"]["message"])
    except Exception:  # noqa: BLE001
        return default


def _parse_answer(res: httpx.Response) -> dict:
    """The JSON object the model wrote, out of a generateContent answer."""
    payload = res.json()
    text = payload["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(_strip_fences(text))


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


@dataclass(frozen=True)
class QuotaHit:
    """What a Gemini 429 said: which quota (per minute, per day, or unknown),
    how long Google asked us to wait, and the limit it named."""

    scope: str  # "minute" | "day" | "unknown"
    retry_after: float | None
    limit: int | None
    message: str

    def describe(self) -> str:
        if self.scope == "day":
            limit = f" ({self.limit} a day)" if self.limit else ""
            when = _in_about(self.retry_after) if self.retry_after else "at midnight Pacific time"
            return f"today's free requests used{limit}; resets {when}"
        if self.scope == "minute":
            limit = f" ({self.limit} a minute)" if self.limit else ""
            return f"rate limited{limit}"
        return f"429 {self.message}"


_DURATION = re.compile(
    r"^\s*(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+(?:\.\d+)?)m)?\s*(?:(\d+(?:\.\d+)?)s?)?\s*$"
)


def _seconds(value: Any) -> float | None:
    """Google's two spellings of a wait: the structured retryDelay ('15s',
    '39599.4s') and the message's own ('20h20m4.321946213s', seen in an
    Oct 11 2026 screenshot). A bare number is seconds."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    m = _DURATION.match(text)
    if not m or not any(m.groups()):
        return None
    hours, minutes, seconds = (float(g) if g else 0.0 for g in m.groups())
    return hours * 3600 + minutes * 60 + seconds


def _in_about(seconds: float) -> str:
    if seconds >= 5400:
        return f"in about {round(seconds / 3600)} h"
    if seconds >= 90:
        return f"in about {round(seconds / 60)} min"
    return f"in about {round(seconds)} s"


def _quota_hit(res: httpx.Response) -> QuotaHit:
    """Read a 429 the way Google writes it: the structured details first
    (quotaId, quotaValue, retryDelay), the message's own words as the
    fallback. Never trusts the word "billing": the standard quota text
    contains it."""
    try:
        err = res.json().get("error") or {}
        if not isinstance(err, dict):
            err = {}
    except Exception:  # noqa: BLE001
        err = {}
    message = str(err.get("message") or "rate limited").strip()
    scope, retry_after = "unknown", None
    day_limit = minute_limit = None
    details = err.get("details") if isinstance(err.get("details"), list) else []
    for detail in details:
        if not isinstance(detail, dict):
            continue
        kind = str(detail.get("@type", ""))
        if kind.endswith("RetryInfo"):
            retry_after = _seconds(detail.get("retryDelay"))
        elif kind.endswith("QuotaFailure"):
            for violation in detail.get("violations") or []:
                if not isinstance(violation, dict):
                    continue
                quota_id = str(violation.get("quotaId", "")).lower()
                value = str(violation.get("quotaValue", ""))
                number = int(value) if value.isdigit() else None
                if "perday" in quota_id:
                    scope = "day"
                    day_limit = number if number is not None else day_limit
                elif "perminute" in quota_id:
                    if scope != "day":
                        scope = "minute"
                    minute_limit = number if number is not None else minute_limit
    low = message.lower()
    if retry_after is None:
        m = re.search(r"retry in ([\dhms.\s]+?)[.\s]*(?:$|\n)", low)
        if m:
            retry_after = _seconds(m.group(1).strip().rstrip("."))
    if scope == "unknown":
        if "per day" in low or "perday" in low or "daily" in low:
            scope = "day"
        elif "per minute" in low or "perminute" in low:
            scope = "minute"
        elif retry_after is not None:
            scope = "day" if retry_after > QUOTA_DAY_THRESHOLD else "minute"
    limit = day_limit if scope == "day" else minute_limit
    if limit is None:
        m = re.search(r"limit:\s*(\d+)", low)
        if m:
            limit = int(m.group(1))
    return QuotaHit(scope, retry_after, limit, message)


class GeminiClient:
    """Gemini's generateContent wire format, against either publik API or
    Google with the user's own key. `backend` stays "gemini" for both: the
    T2 vision pass and the confidence label key on it (scoring/stage.py), and
    both are true for publik. `provider` says who is billed."""

    backend = "gemini"

    def __init__(self, endpoint: Endpoint | None = None, notify: Callable[[str], None] | None = None):
        self.endpoint = endpoint or resolve_endpoint("gemini")
        # The model calls go to right now. Own-key mode moves it down
        # GEMINI_FALLBACK_MODELS when Google keeps refusing, and it stays
        # moved for the rest of the run so moment 2 of 35 doesn't pay the
        # same three-strikes wait moment 1 did.
        self.model = self.endpoint.model
        self.provider = self.endpoint.provider
        # One line per model switch, for the progress panel; None = silent.
        self.notify = notify
        self._models_left: list[str] = (
            [] if self.provider == "publik" else [m for m in GEMINI_FALLBACK_MODELS if m != self.model]
        )
        # Own-key pacing: once Google names a model's per-minute limit, calls
        # to that model are spaced to it for the rest of the run instead of
        # tripping the limit every few calls.
        self._interval: dict[str, float] = {}
        self._last_call_at = 0.0
        # Models whose daily quota this run used up: skipped without a call.
        self._day_out: dict[str, QuotaHit] = {}

    def generate_json(
        self, prompt: str, schema: dict, images: list[bytes] | None = None
    ) -> dict:
        images = images or []
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
        if self.provider == "publik":
            return self._publik_call(body, headers, prompt, schema, images)
        return self._gemini_call(body, headers, prompt, schema, images)

    def _publik_call(
        self, body: dict, headers: dict, prompt: str, schema: dict, images: list[bytes]
    ) -> dict:
        key = _cache_key(self.backend, self.model, prompt, schema, images)
        cache_file = _cache_dir() / f"{key}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text())
        # A repeated call is the same call: the gateway can dedupe on it.
        headers = {**headers, "x-publik-idempotency-key": key}
        # publik API settles real usage even after a client gives up, so a
        # timed-out publik call is final — one try, never a retry, and never
        # another model: the gateway owns which model sits behind the alias.
        try:
            res = httpx.post(self.endpoint.url(), headers=headers, json=body, timeout=LLM_TIMEOUT)
            if res.status_code in (401, 402, 403):
                raise _publik_error(res)
            _record_publik_status(res)
            if res.status_code == 429:
                # The gateway's own words: a quota backoff and a "credits
                # depleted" billing stop look identical as bare 429s but
                # need opposite user actions.
                raise LlmError(f"publik API 429: {_error_message(res, 'rate limited')}")
            if res.status_code >= 400:
                raise LlmError(f"publik API {res.status_code}: {_error_message(res)}")
            data = _parse_answer(res)
        except LlmError:
            raise
        except (httpx.HTTPError, KeyError, json.JSONDecodeError, IndexError) as err:
            raise LlmError(f"publik API call failed: {err}") from err
        cache_file.write_text(json.dumps(data))
        return data

    def _gemini_call(
        self, body: dict, headers: dict, prompt: str, schema: dict, images: list[bytes]
    ) -> dict:
        """Own-key mode: the current model first, then each fallback, each
        with up to three tries. A model is given up on only for what that
        model did (5xx, daily quota, 404); a dead network fails here, since
        the next model would time out just the same."""
        ladder = [self.model, *self._models_left]
        # Any model's cached answer is the answer: a resumed job that moved
        # down the ladder yesterday must not spend today's quota re-asking.
        for model in dict.fromkeys([*ladder, GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]):
            cached = _cache_dir() / f"{_cache_key(self.backend, model, prompt, schema, images)}.json"
            if cached.exists():
                return json.loads(cached.read_text())
        models = [m for m in ladder if m not in self._day_out]
        failures: list[str] = []
        for idx, model in enumerate(models):
            verdict, payload = self._try_model(model, body, headers)
            if verdict == "ok":
                key = _cache_key(self.backend, model, prompt, schema, images)
                (_cache_dir() / f"{key}.json").write_text(json.dumps(payload))
                self.model = model
                self._models_left = models[idx + 1 :]
                return payload
            failures.append(f"{model}: {payload}")
            if idx + 1 < len(models) and self.notify:
                self.notify(f"Gemini {model} answered {payload}; trying {models[idx + 1]}")
        if not models or all(m in self._day_out for m in models):
            used = "; ".join(
                f"{m}: {hit.limit} a day" if hit.limit else m for m, hit in self._day_out.items()
            )
            soonest = min((h.retry_after for h in self._day_out.values() if h.retry_after), default=None)
            when = f", {_in_about(soonest)}" if soonest else ""
            raise QuotaExhaustedError(
                "Your Gemini key has used today's free requests on every model publikclip "
                f"can use ({used}). Google resets daily quotas at midnight Pacific time{when}. "
                "Resume the job then: the moments already scored are saved and are not sent "
                "again. Or switch the brain to publik API in Settings."
            )
        raise LlmError(
            "Gemini could not answer on any model (" + "; ".join(failures) + "). "
            "Google turns free keys away first when its servers are busy. Wait a few "
            "minutes and run again, or switch the brain to publik API in Settings."
        )

    def _pace(self, model: str) -> None:
        """Hold the next call until the model's known per-minute limit allows it."""
        interval = self._interval.get(model, 0.0)
        if interval:
            wait = self._last_call_at + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        self._last_call_at = time.monotonic()

    def _slow_down(self, model: str, hit: QuotaHit, wait: float) -> None:
        """A per-minute 429: space this model's calls to the limit Google named
        (5 a minute on a free flash key) and say so under JUDGE."""
        rpm = hit.limit if hit.limit and 0 < hit.limit <= 120 else QUOTA_DEFAULT_RPM
        interval = 60.0 / rpm + 0.5
        first = model not in self._interval
        self._interval[model] = max(self._interval.get(model, 0.0), interval)
        if self.notify:
            pacing = f", then one call every {interval:.0f} s" if first else ""
            self.notify(
                f"Gemini {model}: your key allows {rpm} requests a minute; "
                f"waiting {wait:.0f} s{pacing}"
            )

    def _try_model(self, model: str, body: dict, headers: dict) -> tuple[str, Any]:
        """Up to three tries on one model. ("ok", data) on success; ("next",
        reason) when this model is the problem and the next one should be
        tried; raises LlmError when trying another model cannot help.

        A per-minute 429 is not a strike: the model is fine, we were early.
        Wait what Google asked for and ask again. A per-day 429 is final for
        the day: straight to the next model, which has its own quota."""
        url = self.endpoint.url(model)
        last_err: Exception | None = None
        strikes = 0
        waits = 0
        while strikes < 3:
            if strikes:
                time.sleep(GEMINI_RETRY_WAITS[strikes - 1])
            self._pace(model)
            try:
                res = httpx.post(url, headers=headers, json=body, timeout=LLM_TIMEOUT)
            except httpx.HTTPError as err:
                last_err = err  # the network, not the model
                strikes += 1
                continue
            status = res.status_code
            if status in (401, 403):
                raise LlmError("Gemini rejected the API key. Check it in Settings.")
            if status == 404:
                # Retired for this key ("no longer available to new users");
                # it will say so again, so straight to the next model.
                return "next", f"404 {_error_message(res, 'model not found')}"
            if status == 429:
                hit = _quota_hit(res)
                if hit.scope == "day":
                    self._day_out[model] = hit
                    return "next", hit.describe()
                if waits >= QUOTA_WAITS_PER_CALL:
                    return "next", f"still {hit.describe()} after {waits} waits"
                waits += 1
                asked = hit.retry_after if hit.retry_after is not None else QUOTA_DEFAULT_WAIT
                wait = min(asked, QUOTA_WAIT_CAP) + 1.0
                self._slow_down(model, hit, wait)
                time.sleep(wait)
                continue
            if status >= 500:
                last_err = LlmError(f"{status} {_error_message(res, 'server error')}")
                strikes += 1
                continue
            try:
                res.raise_for_status()
                data = _parse_answer(res)
            except (httpx.HTTPError, KeyError, json.JSONDecodeError, IndexError) as err:
                last_err = err
                strikes += 1
                continue
            return "ok", data
        if isinstance(last_err, LlmError):
            # three 5xx answers in a row: this model's problem
            return "next", str(last_err)
        raise LlmError(f"Gemini call failed after retries: {last_err}")

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


def make_client(llm_mode: str, notify: Callable[[str], None] | None = None):
    if llm_mode == "ollama":
        return OllamaClient()
    return GeminiClient(resolve_endpoint(llm_mode), notify=notify)
