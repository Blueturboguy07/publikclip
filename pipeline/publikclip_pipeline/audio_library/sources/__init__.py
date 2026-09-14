"""Shared shape for online CC-licensed audio sources.

Every source module (freesound.py, jamendo.py) exposes:

    search(query, kind, max_duration, allow_attribution) -> list[Result]
    download(result) -> library.Item
    get(source_id, kind) -> Result | None      # for `audio fetch`

Licence policy lives here so both sources enforce it identically: CC0 is
always allowed, CC-BY only when the caller opts in, NC/ND are NEVER
returned by search() or resolved by get() — there is no flag that widens
past that line."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Result:
    """One online search hit — not yet downloaded."""

    source: str            # "freesound" | "jamendo"
    source_id: str
    name: str
    duration: float
    tags: list[str]
    kind: str               # "music" | "sfx" — echoes the caller's search kind
    licence: str             # SPDX-like, e.g. "CC0-1.0", "CC-BY-4.0"
    attribution: str
    download_url: str
    page_url: str
    rating: float = 0.0      # for starter-pack ranking; 0.0 when a source has no rating concept
    downloads: int = 0


class MissingKeyError(Exception):
    """A source needs an API key/client id that isn't in secrets.json yet."""


# Creative Commons licence URLs (both http/https, any of the CC domains the
# two APIs use) mapped to an SPDX-like id. NC and ND variants map to None —
# callers MUST drop a None-mapped result rather than fetch it.
_CC_URL = re.compile(
    r"creativecommons\.org/(?:licenses/(?P<kind>[a-z-]+)|publicdomain/(?P<pd>zero))/(?P<version>[\d.]+)",
    re.IGNORECASE,
)


def parse_cc_licence(value: str) -> str | None:
    """SPDX-like id for a Creative Commons licence URL or name, or None for
    anything that isn't CC0/CC-BY (NC and ND clauses are always rejected)."""
    if not value:
        return None
    text = value.strip().lower()
    if "creative commons 0" in text or "cc0" in text or "publicdomain/zero" in text:
        return "CC0-1.0"
    m = _CC_URL.search(text)
    if m:
        version = m.group("version") or "4.0"
        if m.group("pd"):
            return "CC0-1.0"
        kind = m.group("kind") or ""
        if kind == "by":
            return f"CC-BY-{version}"
        return None  # by-nc, by-nd, by-sa, by-nc-sa, by-nc-nd, by-nd-sa, ...
    if text in ("attribution", "by"):
        return "CC-BY-4.0"
    return None


def is_allowed(licence: str | None, allow_attribution: bool) -> bool:
    """Whether search()/get() may keep a result with this SPDX-like licence.
    CC0 is always allowed; CC-BY (any version) only when the caller opted
    in. A None licence (parse_cc_licence already rejected NC/ND, or the
    source's licence string was unrecognized) is never allowed."""
    if licence is None:
        return False
    if licence == "CC0-1.0":
        return True
    return allow_attribution and licence.startswith("CC-BY-")


def key_error(name: str, env_or_secret_key: str, signup_url: str) -> MissingKeyError:
    return MissingKeyError(
        f"{name} needs a free API key. Sign up at {signup_url}, then save it "
        f"(secrets.json key \"{env_or_secret_key}\")."
    )
