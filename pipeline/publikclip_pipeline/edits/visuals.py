"""Visual-overlay planning + fetching.

Planner: one LLM call over the clip's transcript proposes overlays — the
explicit things being talked about (objects, places, amounts, situations).
Deliberately generous (the user wants overdone-and-removable); every item
lands on the timeline where it can be deleted, moved, resized.

Fetchers: Pexels (free BYO key, real photos, license-clean) and OpenRouter
image generation (the funded key's image models — on-topic for absurd or
specific scenes stock can't match). Images cache into
<job_dir>/overlays/ and are referenced by path in the edit state.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

from .. import config
from ..scoring import llm as llm_mod
from .timeline import Overlay

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "visuals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string", "description": "the exact spoken words this illustrates"},
                    "query": {"type": "string", "description": "short image search / generation prompt"},
                    "start_word_index": {"type": "integer"},
                    "duration_sec": {"type": "number", "description": "1.5-3.0 typical"},
                    "kind": {"type": "string", "enum": ["object", "place", "person", "number", "situation"]},
                },
                "required": ["phrase", "query", "start_word_index", "duration_sec", "kind"],
            },
        }
    },
    "required": ["visuals"],
}


def plan_prompt(numbered_words: str) -> str:
    return (
        "You are planning illustrative pop-over images for a short vertical "
        "clip. Below is the transcript with word indices. Propose a visual "
        "for EVERY explicit thing mentioned — objects, animals, places, "
        "amounts of money, named people, concrete situations. Be generous; "
        "the editor deletes what they don't want. Do not propose visuals "
        "for abstract talk with nothing depictable.\n\n"
        f"{numbered_words}\n\n"
        "query: a concrete image description, e.g. 'possum close up', "
        "'stack of hundred dollar bills', 'las vegas strip at night'. "
        "start_word_index: where the phrase begins. duration_sec: 1.5-3."
    )


def plan_overlays(words: list[dict], llm_mode: str) -> list[dict]:
    """words carry OUTPUT-timeline times. Returns raw visual plans."""
    numbered = " ".join(f"[{i}]{w['word']}" for i, w in enumerate(words))
    client = llm_mod.make_client(llm_mode)
    result = client.generate_json(plan_prompt(numbered), PLAN_SCHEMA)
    plans = []
    for v in result.get("visuals", []):
        idx = max(0, min(int(v["start_word_index"]), len(words) - 1))
        start = words[idx]["start"]
        plans.append(
            {
                "phrase": v["phrase"],
                "query": v["query"],
                "start": round(start, 2),
                "end": round(start + max(1.0, min(4.0, float(v["duration_sec"]))), 2),
                "kind": v["kind"],
            }
        )
    return plans


def _overlay_dir(job_dir: Path) -> Path:
    d = job_dir / "overlays"
    d.mkdir(exist_ok=True)
    return d


def pexels_key() -> str | None:
    secrets_path = config.home_dir() / "secrets.json"
    if secrets_path.exists():
        try:
            return json.loads(secrets_path.read_text()).get("pexels_api_key")
        except (json.JSONDecodeError, OSError):
            return None
    return None


def fetch_pexels(query: str, job_dir: Path) -> str | None:
    key = pexels_key()
    if not key:
        return None
    dest = _overlay_dir(job_dir) / f"px_{hashlib.sha256(query.encode()).hexdigest()[:12]}.jpg"
    if dest.exists():
        return str(dest)
    try:
        res = httpx.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": 1, "orientation": "landscape"},
            headers={"Authorization": key},
            timeout=20.0,
        )
        res.raise_for_status()
        photos = res.json().get("photos", [])
        if not photos:
            return None
        img = httpx.get(photos[0]["src"]["large"], timeout=30.0)
        img.raise_for_status()
        dest.write_bytes(img.content)
        return str(dest)
    except httpx.HTTPError:
        return None


OPENROUTER_IMAGE_MODEL = os.environ.get("PUBLIKCLIP_IMAGE_MODEL", "black-forest-labs/flux-1.1-pro")


def fetch_ai_image(query: str, job_dir: Path) -> str | None:
    """AI-generated overlay via OpenRouter image models."""
    key = os.environ.get("PUBLIKCLIP_OPENROUTER_API_KEY") or llm_mod._secrets_value("openrouter_api_key")
    if not key:
        return None
    dest = _overlay_dir(job_dir) / f"ai_{hashlib.sha256(query.encode()).hexdigest()[:12]}.png"
    if dest.exists():
        return str(dest)
    try:
        res = httpx.post(
            llm_mod.OPENROUTER_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": OPENROUTER_IMAGE_MODEL,
                "messages": [{"role": "user", "content": f"A clean, punchy illustrative photo for a video overlay: {query}. No text in the image."}],
                "modalities": ["image"],
            },
            timeout=120.0,
        )
        res.raise_for_status()
        payload = res.json()
        import base64

        # OpenRouter returns image content blocks: [{"type": "image", "image_url": {"url": "data:image/...;base64,..."}}]
        for part in payload["choices"][0]["message"].get("content", []):
            if isinstance(part, dict) and part.get("type") == "image":
                url = (part.get("image_url") or {}).get("url", "")
                if url.startswith("data:image/"):
                    dest.write_bytes(base64.b64decode(url.split(",", 1)[1]))
                    return str(dest)
        # Some models report a plain base64 list instead.
        for b64 in payload["choices"][0]["message"].get("images", []):
            if isinstance(b64, str) and b64:
                dest.write_bytes(base64.b64decode(b64))
                return str(dest)
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return None
    return None


def fetch_image(query: str, job_dir: Path, prefer: str = "pexels") -> tuple[str | None, str]:
    """(path, source). Tries the preferred source, falls back to the other."""
    order = ["pexels", "ai"] if prefer != "ai" else ["ai", "pexels"]
    for source in order:
        path = fetch_pexels(query, job_dir) if source == "pexels" else fetch_ai_image(query, job_dir)
        if path:
            return path, source
    return None, "none"


def suggest(job_dir: Path, words: list[dict], llm_mode: str, prefer: str = "pexels") -> list[Overlay]:
    """Plan + fetch, returning ready Overlay items (static by default —
    animation strictly opt-in per the design decision)."""
    plans = plan_overlays(words, llm_mode)
    out: list[Overlay] = []
    for i, plan in enumerate(plans):
        path, source = fetch_image(plan["query"], job_dir, prefer)
        if not path:
            continue
        out.append(
            Overlay(
                id=f"ov{i}_{hashlib.sha256(plan['query'].encode()).hexdigest()[:6]}",
                query=plan["query"],
                source=source,
                image_path=path,
                start=plan["start"],
                end=plan["end"],
                animation="none",
                phrase=plan["phrase"],
            )
        )
    return out
