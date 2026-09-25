"""Alibaba Cloud (Qwen) reasoning models via the token-plan MaaS gateway.

The gateway speaks the **Anthropic Messages API** at ``/apps/anthropic/v1/messages``
— and it returns real ``thinking`` blocks — so a turn reuses :mod:`anthropic_http`
(the same client glm/deepseek ride) with a per-call :class:`~anthropic_http.ProviderConfig`
carrying the selected model. Only the CATALOG is fetched over the gateway's separate
OpenAI-compatible surface (``/compatible-mode/v1/models``), because the Anthropic app
exposes no model list.

One key fronts several labs (Qwen, DeepSeek, GLM, plus audio/image models); this
provider is for the **reasoning LLMs**, so :func:`is_reasoning` drops the audio/TTS/
image ids from the catalog. Models are selected per call as ``ali:<model>`` tokens
(e.g. ``ali:qwen3.8-max``), like ``atlas:``/``openrouter:``.

Auth: ``ASK_FABLE_ALI_API_KEY`` — a token-plan MaaS key for *this* gateway. (No
generic ``DASHSCOPE_API_KEY`` fallback: a classic DashScope key targets a different
Anthropic-compatible host, so it would 401 here; point ``ASK_FABLE_ALI_BASE_URL`` at
that host if you want to use one.) Billed per token — the result carries
``cost_basis="billed"`` so it is never confused with the flat-plan OAuth oracles.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request

from . import anthropic_http, config
from .openai_compat import USER_AGENT as _USER_AGENT
from .oracle_common import OracleResult

DEFAULT_BASE_URL = "https://token-plan.maas.qwencloudapi.com"
_ANTHROPIC_PATH = "/apps/anthropic"  # Anthropic Messages API surface (the reasoning path)
_CATALOG_PATH = "/compatible-mode/v1/models"  # OpenAI-compatible catalog (list only)
DEFAULT_MODEL = "qwen3.8-max"

# Ids carrying any of these are not text-reasoning LLMs (audio / TTS / realtime /
# image / video / vision / embedding), so `ask_ali` and `list_ali_models` drop them.
# A best-effort id-substring filter — the catalog carries no modality field to key
# on — and only advisory: `ask_ali` runs any explicit `model`, and `list_ali_models
# all=true` shows the full list, so a false drop is always recoverable.
_NON_REASONING = (
    "audio", "tts", "realtime", "image", "wan", "video", "-vl", "vl-", "embedding", "rerank",
)


def base_url() -> str:
    raw = (os.environ.get("ASK_FABLE_ALI_BASE_URL") or "").strip() or DEFAULT_BASE_URL
    return raw.rstrip("/")


def anthropic_base() -> str:
    """Base URL for the Anthropic Messages surface (`anthropic_http` appends
    ``/v1/messages``)."""
    return base_url() + _ANTHROPIC_PATH


def api_key() -> str | None:
    """The gateway API key from ``ASK_FABLE_ALI_API_KEY``, or None. No generic
    DashScope fallback: that key targets a different endpoint (see module doc)."""
    return (os.environ.get("ASK_FABLE_ALI_API_KEY") or "").strip() or None


def configured() -> bool:
    """True when a reasoning call can be made — i.e. an API key is present."""
    return api_key() is not None


def default_model() -> str:
    """Model used by ``ask_ali`` when none is passed. Precedence: config file
    (``ali_model``) → ``ASK_FABLE_ALI_MODEL`` → :data:`DEFAULT_MODEL`."""
    return (
        config.get_str("ali_model")
        or (os.environ.get("ASK_FABLE_ALI_MODEL") or "").strip()
        or DEFAULT_MODEL
    )


def is_reasoning(model_id: str) -> bool:
    """True unless the id names an audio/TTS/image model (see :data:`_NON_REASONING`)."""
    m = model_id.lower()
    return not any(s in m for s in _NON_REASONING)


def catalog() -> dict:
    """The gateway's model list, split into reasoning LLMs and everything else.

    Fetched from the OpenAI-compatible ``/compatible-mode/v1/models`` endpoint (the
    Anthropic app has no list). Free — a catalog read is not a reasoning call.
    Returns ``{"cloud_ok", "models" (reasoning ids), "all" (every id), "error"}``."""
    key = api_key()
    headers = {"User-Agent": _USER_AGENT}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(base_url() + _CATALOG_PATH, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 — fixed https gateway
            data = json.loads(resp.read().decode("utf-8"))
    # HTTPException: urllib doesn't wrap what read() raises (IncompleteRead on a cut body).
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ValueError, OSError) as exc:
        return {"cloud_ok": False, "models": [], "all": [], "error": str(exc)}
    # A 200 can still carry an error envelope ({"error": …}) or a non-object body;
    # either way there is no usable list, so fail closed rather than report "no
    # models" as success or let `.get` raise on a list/None body.
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        detail = data.get("error") if isinstance(data, dict) else None
        return {
            "cloud_ok": False,
            "models": [],
            "all": [],
            "error": str(detail) if detail else "unexpected catalog response",
        }
    ids = [
        m["id"]
        for m in data["data"]
        if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"].strip()
    ]
    return {
        "cloud_ok": True,
        "models": [i for i in ids if is_reasoning(i)],
        "all": ids,
        "error": None,
    }


async def run(
    model: str,
    question: str,
    context: str = "",
    *,
    system_prompt: str | None = None,
    timeout: float | None = None,
) -> OracleResult:
    """One reasoning turn on an Alibaba/Qwen model over the Anthropic Messages
    surface. Never raises for expected failures (delegates to ``anthropic_http``)."""
    key = api_key()
    if not key:
        return OracleResult(
            "error",
            kind="not_configured",
            model=model,
            text="ali not configured (set ASK_FABLE_ALI_API_KEY)",
        )
    cfg = anthropic_http.ProviderConfig(
        key="ali:" + model,
        label=model,
        base_url=anthropic_base(),
        model=model,
        api_key=key,
    )
    result = await anthropic_http.run(
        cfg, question, context, timeout=timeout, system_prompt=system_prompt
    )
    # Real per-token spend on the token plan — never confuse it with the flat-plan
    # OAuth oracles (same reasoning as fable's http transport).
    result.meta.setdefault("cost_basis", "billed")
    return result
