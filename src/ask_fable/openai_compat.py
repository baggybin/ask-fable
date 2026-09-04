"""Shared plumbing for OpenAI-compatible chat gateways (Atlas Cloud, OpenRouter).

Both providers speak the same `POST /chat/completions` dialect and both publish a
free, unauthenticated model catalog, so the *transport* half of talking to them is
identical: the same error envelope, the same `choices[0].message.content`, the same
finish-reason lookup, the same effort ladder over `max_tokens`/timeout.

What is NOT here is anything provider-specific — base URLs, API keys, catalog
shape, model ranking, or the request body a given gateway accepts. Those diverge
sharply (Atlas ranks a flat catalog with hand-tuned keyword signals and has to
probe whether `reasoning_effort` is accepted at all; OpenRouter publishes
`supported_parameters` and a per-model `reasoning.supported_efforts`, so it can
know up front), and forcing them through one abstraction would mean a base class
with per-provider feature flags on day one.

This module exists so the subtle parts — error-envelope parsing, the transient
retry contract, the WAF User-Agent — have exactly one definition. Duplicating
them is how two copies silently drift apart, with each provider's tests happily
green against its own stale copy.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# Some provider edges (Cloudflare-style WAF) return HTTP 403 to the default
# ``Python-urllib/x.y`` User-Agent, so every request sends a real one.
USER_AGENT = "ask_fable/1.0 (+https://github.com/baggybin/ask-fable)"

# Effort presets: the menu dimension. The honest levers on an OpenAI-compatible
# API are output budget (max_tokens), wall-clock timeout, and a system nudge —
# temperature stays flat/low so oracle answers are deterministic-ish. ``deep``
# also asks for maximum reasoning effort; how that is negotiated is the
# provider's business, not this module's.
EFFORT_PRESETS: dict[str, dict] = {
    "quick": {
        "max_tokens": 1024,
        "timeout": 120.0,
        "nudge": "Answer concisely.",
    },
    "standard": {
        "max_tokens": 4096,
        "timeout": 240.0,
        "nudge": None,
    },
    "deep": {
        "max_tokens": 16384,
        "timeout": 600.0,
        "nudge": "Reason thoroughly, step by step, then give a concrete recommendation.",
        "reasoning_effort": "high",
    },
}
# Prefer maximum reasoning budget by default (operators can still pass effort=quick/standard).
DEFAULT_EFFORT = "deep"


def effort_menu() -> list[dict]:
    """The three effort choices, shaped for an agent's selection menu."""
    return [
        {
            "value": "quick",
            "label": f"Quick — short answer, ~{EFFORT_PRESETS['quick']['max_tokens']} tokens",
            "max_tokens": EFFORT_PRESETS["quick"]["max_tokens"],
        },
        {
            "value": "standard",
            "label": f"Standard — ~{EFFORT_PRESETS['standard']['max_tokens']} tokens",
            "max_tokens": EFFORT_PRESETS["standard"]["max_tokens"],
        },
        {
            "value": "deep",
            "label": (
                f"Deep — ~{EFFORT_PRESETS['deep']['max_tokens']} tokens, max reasoning (default)"
            ),
            "max_tokens": EFFORT_PRESETS["deep"]["max_tokens"],
        },
    ]


def get_json(url: str, headers: dict, timeout: float, user_agent: str = USER_AGENT) -> dict | None:
    """GET one JSON document, or None on any failure (never raises)."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, UnicodeDecodeError, TimeoutError):
        return None


def parse_chat_response(body: bytes) -> tuple[str | None, dict, str | None]:
    """Return (text, parsed_obj, error) from an OpenAI ``/chat/completions`` body."""
    try:
        obj = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return None, {}, f"unparseable response: {exc}"
    if isinstance(obj.get("error"), dict):  # OpenAI-style error envelope
        e = obj["error"]
        return None, {}, f"{e.get('type', 'error')}: {e.get('message', obj)}"
    choices = obj.get("choices")
    if not isinstance(choices, list) or not choices:
        return None, {}, "unrecognized response shape (no choices[])"
    msg = choices[0].get("message") or {}
    text = (msg.get("content") or "").strip()
    return text, obj if isinstance(obj, dict) else {}, None


def finish_reason(metadata: dict) -> str | None:
    """``choices[0].finish_reason`` from a parsed response body, or None."""
    choices = metadata.get("choices") if isinstance(metadata, dict) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        fr = choices[0].get("finish_reason")
        return str(fr) if fr else None
    return None


def max_tokens_cap() -> int:
    """The operator's global output ceiling (``ASK_FABLE_MAX_TOKENS``)."""
    try:
        return int(os.environ.get("ASK_FABLE_MAX_TOKENS") or 65536)
    except (TypeError, ValueError):
        return 65536
