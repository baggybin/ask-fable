"""Reach an Ollama Cloud model for one reasoning turn.

Dynamic council oracles named ``ollama:<model>`` (e.g. ``ollama:kimi-k2.7-code:cloud``,
``ollama:gpt-oss:120b-cloud``) go through here. Unlike the Anthropic-compatible
HTTP providers (GLM, DeepSeek), Ollama speaks its OWN native ``/api/chat`` shape:
a Bearer-only auth header, the system prompt as a leading ``system`` message, and
a response with ``message.content`` (+ optional ``message.thinking`` when the
model reasons). So this is a separate backend, not a reuse of ``anthropic_http``.

Transport — by default the **local Ollama daemon** (``http://localhost:11434``),
which proxies ``:cloud`` models through your existing ``ollama signin`` session, so
NO API key is set by the server (mirroring how Fable reuses OAuth and MiniMax
reuses the ``mmx`` CLI login). Point ``ASK_FABLE_OLLAMA_BASE_URL`` at
``https://ollama.com`` (with ``ASK_FABLE_OLLAMA_API_KEY``) to hit the cloud
directly instead. A plain stdlib ``urllib`` POST (no new dependency); the same
``REFUSED:`` scope contract as every other oracle.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .oracle_common import (
    RETRYABLE_HTTP_STATUSES,
    OracleResult,
    TransientRetryTimeout,
    call_with_transient_retry,
    compose,
    http_error_detail,
    shape,
)
from .prompts import ORACLE_SYSTEM_PROMPT  # same scope contract for every oracle
from .provider_telemetry import ProviderTelemetry, ProviderUsage, token_count, usage_if_available

DEFAULT_OLLAMA_BASE_URL = (
    "http://localhost:11434"  # local daemon proxies :cloud via `ollama signin`
)
DEFAULT_OLLAMA_MODEL = "gpt-oss:120b-cloud"
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
DEFAULT_CLOUD_HOST = "https://ollama.com"  # where the cloud catalog (`/api/tags`) lives

# Ollama Cloud models that make up the 'full' council tier when neither the config
# file (config.json → "ollama_council") nor ASK_FABLE_OLLAMA_COUNCIL is set. A
# curated spread of strong reasoners — Ollama-hosted GLM and MiniMax alongside the
# coding/reasoning specialists — deliberately kept LEAN: the huge generalists
# (mistral-large-3:675b, qwen3.5:397b) dominate a parallel council's wall-clock for
# little marginal quality, so they're left out of the default (add them per call or
# via `configure_ollama_council` if you want them). Reconfigure at runtime with
# that tool, or override via env to taste.
DEFAULT_OLLAMA_COUNCIL = (
    "minimax-m3:cloud",
    "glm-5.2:cloud",
    "nemotron-3-ultra:cloud",
    "qwen3-coder:480b-cloud",
    "kimi-k2.7-code:cloud",
    "deepseek-v4-pro:cloud",
    "gpt-oss:120b-cloud",
)


def base_url() -> str:
    raw = (os.environ.get("ASK_FABLE_OLLAMA_BASE_URL") or "").strip() or DEFAULT_OLLAMA_BASE_URL
    return raw.rstrip("/")


def api_key() -> str | None:
    """The Ollama Cloud API key, or None. Only needed for a remote (non-local) endpoint."""
    return (os.environ.get("ASK_FABLE_OLLAMA_API_KEY") or "").strip() or None


def is_local() -> bool:
    """True when the endpoint is a local Ollama daemon (no key needed — it proxies
    ``:cloud`` models through ``ollama signin``)."""
    return (urllib.parse.urlparse(base_url()).hostname or "") in _LOCAL_HOSTS


def configured() -> bool:
    """True when Ollama is reachable: a local daemon (signin assumed) or a remote
    endpoint with an API key set."""
    return is_local() or api_key() is not None


def default_model() -> str:
    """Model used by the ``ask_ollama`` single-ask tool when none is passed.

    Precedence: config file (``ollama_model``) → ``ASK_FABLE_OLLAMA_MODEL`` →
    ``DEFAULT_OLLAMA_MODEL``."""
    return (
        config.get_str("ollama_model")
        or (os.environ.get("ASK_FABLE_OLLAMA_MODEL") or "").strip()
        or DEFAULT_OLLAMA_MODEL
    )


def dedupe_models(parts: list[str]) -> list[str]:
    """Strip an optional ``ollama:`` prefix off each id and de-dupe, order-preserving."""
    out: list[str] = []
    for p in parts:
        m = p[len("ollama:") :] if p.lower().startswith("ollama:") else p
        m = m.strip()
        if m and m not in out:
            out.append(m)
    return out


def council_models() -> list[str]:
    """Ollama Cloud model ids for the 'full' council tier / a default ``ask_ollama_council``.

    Precedence, highest first: the config file (``ollama_council``, written by the
    ``configure_ollama_council`` tool) → ``ASK_FABLE_OLLAMA_COUNCIL`` (comma- or
    whitespace-separated) → ``DEFAULT_OLLAMA_COUNCIL``. Returns bare model ids —
    the ``ollama:`` prefix is added by callers."""
    from_config = config.get_list("ollama_council")
    if from_config:
        return dedupe_models(from_config)
    raw = os.environ.get("ASK_FABLE_OLLAMA_COUNCIL")
    if raw is None or not raw.strip():
        return list(DEFAULT_OLLAMA_COUNCIL)
    return dedupe_models([p.strip() for p in raw.replace(",", " ").split()])


def cloud_id(name: str) -> str:
    """Normalize a catalog model name into the id the local daemon expects.

    The signed-in daemon exposes cloud models with a ``-cloud`` suffix on the tag
    (``gpt-oss:120b`` → ``gpt-oss:120b-cloud``), or a bare ``:cloud`` tag when the
    name is untagged (``minimax-m3`` → ``minimax-m3:cloud``). Already-suffixed ids
    pass through unchanged."""
    n = name.strip()
    if not n or n.endswith(":cloud") or n.endswith("-cloud"):
        return n
    return f"{n}-cloud" if ":" in n else f"{n}:cloud"


def _get_json(url: str, timeout: float) -> dict | None:
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, UnicodeDecodeError, TimeoutError):
        return None


def catalog(timeout: float = 6.0) -> dict:
    """Discover Ollama models available for the council (best-effort, never raises).

    Returns ``{"cloud": [...], "local": [...], "cloud_ok": bool}``:
      - ``cloud``: daemon-ready ids for every model in the ollama.com catalog
        (``https://ollama.com/api/tags``), normalized via :func:`cloud_id` and
        sorted. Empty when the catalog can't be reached.
      - ``local``: ids of models already pulled into the local daemon
        (``/api/tags`` on the configured base URL) — the ones certain to run now.
      - ``cloud_ok``: whether the cloud catalog fetch succeeded.
    """
    cloud_host = (os.environ.get("ASK_FABLE_OLLAMA_CATALOG_URL") or DEFAULT_CLOUD_HOST).rstrip("/")
    cloud_raw = _get_json(f"{cloud_host}/api/tags", timeout)
    cloud: list[str] = []
    if cloud_raw:
        for m in cloud_raw.get("models") or []:
            name = str(m.get("name") or m.get("model") or "").strip()
            cid = cloud_id(name)
            if cid and cid not in cloud:
                cloud.append(cid)

    local_raw = _get_json(f"{base_url()}/api/tags", timeout)
    local: list[str] = []
    if local_raw:
        for m in local_raw.get("models") or []:
            name = str(m.get("name") or m.get("model") or "").strip()
            if name and name not in local:
                local.append(name)

    return {"cloud": sorted(cloud), "local": sorted(local), "cloud_ok": cloud_raw is not None}


def _timeout_default() -> float:
    try:
        return float(os.environ.get("ASK_FABLE_TIMEOUT") or 240.0)
    except (TypeError, ValueError):
        return 240.0


def _max_tokens() -> int:
    try:
        return int(os.environ.get("ASK_FABLE_MAX_TOKENS") or 65536)
    except (TypeError, ValueError):
        return 65536


def _parse(body: bytes) -> tuple[str | None, str, str | None]:
    """Return (text, thinking, error) from an Ollama ``/api/chat`` response body."""
    try:
        obj = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return None, "", f"unparseable response: {exc}"
    if isinstance(obj.get("error"), str):  # Ollama's error shape
        return None, "", obj["error"]
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None, "", "unrecognized response shape (no message)"
    text = (msg.get("content") or "").strip()
    thinking = (msg.get("thinking") or "").strip()
    return text, thinking, None


async def run(
    model: str,
    question: str,
    context: str = "",
    *,
    timeout: float | None = None,
) -> OracleResult:
    """Run one turn against Ollama Cloud. Never raises for expected failures
    (network, HTTP, timeout, bad key, missing config)."""
    timeout = timeout if timeout is not None else _timeout_default()
    key = api_key()
    if not configured():
        return OracleResult(
            "error",
            kind="not_configured",
            text="ollama not reachable (default is a local `ollama serve` + `ollama signin`; "
            "or set ASK_FABLE_OLLAMA_API_KEY to hit ollama.com directly)",
            model=model,
        )

    messages = [
        {"role": "system", "content": ORACLE_SYSTEM_PROMPT},
        {"role": "user", "content": compose(question, context)},
    ]

    metadata: dict = {}
    retry_count = 0
    final_http_status: int | None = None

    def _post(think: bool) -> tuple[str | None, str, str | None, int | None]:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": _max_tokens()},  # Ollama's output-token cap
        }
        if think:
            payload["think"] = True
        headers = {"content-type": "application/json"}
        if key:  # only needed for a remote endpoint; the local daemon uses `ollama signin`
            headers["authorization"] = f"Bearer {key}"
        req = urllib.request.Request(
            f"{base_url()}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                try:
                    metadata.clear()  # a retry must not inherit a prior body
                    metadata.update(json.loads(body))
                except (ValueError, UnicodeDecodeError):
                    pass
                text, thinking, err = _parse(body)
                return text, thinking, err, 200
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            return None, "", f"HTTP {e.code}: {detail or e.reason}", e.code
        except urllib.error.URLError as e:
            return None, "", f"network error: {e.reason}", None

    def _call() -> tuple[str | None, str, str | None]:
        nonlocal final_http_status, retry_count
        text, thinking, err, code = _post(think=True)
        final_http_status = code
        # Some models reject the `think` flag with a 400 — retry once without it.
        if code == 400 and "think" in (err or "").lower():
            retry_count += 1
            text, thinking, err, final_http_status = _post(think=False)
        return text, thinking, err

    started = time.perf_counter()
    try:
        (text, thinking, err), status_retries = await call_with_transient_retry(
            _call,
            timeout=timeout,
            # One bounded retry on transient statuses (429 / 5xx) — the think-flag
            # feature retry inside _call can stack on top.
            retryable=lambda r: bool(r[2]) and final_http_status in RETRYABLE_HTTP_STATUSES,
        )
        retry_count += status_retries
    except TransientRetryTimeout as e:
        return OracleResult(
            "error",
            kind="timeout",
            text=f"{model} timed out after {e.elapsed_s:.0f}s",
            model=model,
            telemetry=ProviderTelemetry(
                oracle_key="ollama",
                requested_model=model,
                actual_model=model,
                transport="http-json",
                retry_count=retry_count + e.retry_count,
                wall_duration_ms=(time.perf_counter() - started) * 1000,
                reasoning_available=False,
                usage_available=False,
                tools_available=False,
            ),
        )
    if err:
        kind, text = http_error_detail(label="Ollama", error=err, http_status=final_http_status)
        return OracleResult(
            "error",
            kind=kind,
            text=text,
            model=model,
            telemetry=ProviderTelemetry(
                oracle_key="ollama",
                requested_model=model,
                actual_model=model,
                transport="http-json",
                http_status=final_http_status,
                retry_count=retry_count,
                wall_duration_ms=(time.perf_counter() - started) * 1000,
                reasoning_available=False,
                usage_available=False,
                tools_available=False,
            ),
        )
    shaped = shape(text)
    usage = (
        usage_if_available(
            ProviderUsage(
                input_tokens=token_count(metadata.get("prompt_eval_count")),
                output_tokens=token_count(metadata.get("eval_count")),
            )
        )
        if metadata.get("prompt_eval_count") is not None or metadata.get("eval_count") is not None
        else None
    )
    telemetry = ProviderTelemetry(
        oracle_key="ollama",
        requested_model=model,
        actual_model=metadata.get("model") or model,
        transport="http-json",
        stop_reason=metadata.get("done_reason"),
        http_status=final_http_status,
        retry_count=retry_count,
        wall_duration_ms=(time.perf_counter() - started) * 1000,
        provider_duration_ms=(metadata.get("total_duration") / 1_000_000)
        if isinstance(metadata.get("total_duration"), int)
        else None,
        reasoning_available=bool(thinking),
        usage_available=usage is not None,
        tools_available=False,
        usage=usage,
    )
    return OracleResult(
        shaped.status,
        text=shaped.text,
        kind=shaped.kind,
        model=model,
        thinking=thinking,
        telemetry=telemetry,
    )
