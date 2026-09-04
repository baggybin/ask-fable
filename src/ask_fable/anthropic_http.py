"""Reach any Anthropic-Messages-compatible endpoint for one reasoning turn.

Extra council oracles (GLM via Z.ai, DeepSeek, or any other provider that speaks
the Anthropic ``/v1/messages`` shape) go through here. Unlike Fable (OAuth) and
MiniMax (`mmx` CLI), these need an API key + base URL — supplied entirely by env,
never hardcoded — so the server still sets no secrets of its own; it only reads
what the operator configured.

Each configured provider is one ``ProviderConfig`` (label, env-var prefix, base
URL, model, key). The call is a plain POST with stdlib ``urllib`` (no new
dependency), parsing content blocks (text + optional thinking) and honoring the
same ``REFUSED:`` scope contract as Fable/MiniMax.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .oracle_common import (
    RETRYABLE_HTTP_STATUSES,
    OracleResult,
    TransientRetryTimeout,
    call_with_transient_retry,
    compose,
    extract_http_error,
    http_error_detail,
    max_tokens,
    parse_anthropic_body,
    shape,
    timeout_default,
)
from .prompts import ORACLE_SYSTEM_PROMPT
from .provider_telemetry import ProviderTelemetry, ProviderUsage, token_count, usage_if_available

# Built-in provider defaults. The API key is ALWAYS from env (no default) — a
# provider with no key configured is simply unavailable.
_DEFAULTS = {
    "glm": {"base_url": "https://api.z.ai/api/anthropic", "model": "glm-5.2"},
    "deepseek": {"base_url": "https://api.deepseek.com/anthropic", "model": "deepseek-v4-pro"},
}


@dataclass
class ProviderConfig:
    key: str  # oracle key, e.g. "glm"
    label: str  # display/model label
    base_url: str
    model: str
    api_key: str


def _env(prefix: str, name: str) -> str | None:
    return os.environ.get(f"ASK_FABLE_{prefix}_{name}") or None


def config_for(key: str) -> ProviderConfig | None:
    """Resolve a provider from env, or None if no API key is configured for it."""
    prefix = key.upper()
    api_key = _env(prefix, "API_KEY")
    if not api_key:
        return None
    d = _DEFAULTS.get(key, {})
    base_url = _env(prefix, "BASE_URL") or d.get("base_url")
    model = _env(prefix, "MODEL") or d.get("model")
    if not base_url or not model:
        return None
    return ProviderConfig(
        key=key, label=model, base_url=base_url.rstrip("/"), model=model, api_key=api_key
    )


async def run(
    cfg: ProviderConfig,
    question: str,
    context: str = "",
    *,
    timeout: float | None = None,
) -> OracleResult:
    """Run one turn against an Anthropic-compatible endpoint. Never raises for
    expected failures (network, HTTP, timeout, bad key)."""
    timeout = timeout if timeout is not None else timeout_default()
    payload = json.dumps(
        {
            "model": cfg.model,
            "max_tokens": max_tokens(),
            "system": ORACLE_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": compose(question, context)}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{cfg.base_url}/v1/messages",
        data=payload,
        method="POST",
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": cfg.api_key,
            "authorization": f"Bearer {cfg.api_key}",  # some gateways accept only this
        },
    )

    # Transport facts (HTTP status, request-id header) live apart from the
    # parsed response body: merging the body into the same dict would let a
    # 200 response whose JSON happens to contain an "http_status" key forge a
    # retryable status, and both dicts are cleared per attempt so a retry
    # never inherits the previous attempt's state (e.g. a 429's request-id
    # attached to a later network failure).
    transport: dict = {}
    body_meta: dict = {}

    def _call() -> tuple[str | None, str, str | None]:
        transport.clear()
        body_meta.clear()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        body_meta.update(parsed)
                except (ValueError, UnicodeDecodeError):
                    pass
                headers = getattr(resp, "headers", None)
                if headers is not None:
                    transport["request_id"] = headers.get("request-id") or headers.get(
                        "x-request-id"
                    )
                return parse_anthropic_body(body)
        except urllib.error.HTTPError as e:
            transport["http_status"] = e.code
            if e.headers is not None:
                transport["request_id"] = e.headers.get("request-id") or e.headers.get(
                    "x-request-id"
                )
            return None, "", extract_http_error(e)
        except urllib.error.URLError as e:
            return None, "", extract_http_error(e)

    started = time.perf_counter()
    try:
        (text, thinking, err), retry_count = await call_with_transient_retry(
            _call,
            timeout=timeout,
            # One bounded retry on transient statuses (429 / 5xx) — a single
            # rate-limit hiccup shouldn't hard-fail a council panelist.
            retryable=lambda r: (
                bool(r[2]) and transport.get("http_status") in RETRYABLE_HTTP_STATUSES
            ),
        )
    except TransientRetryTimeout as e:
        return OracleResult(
            "error",
            kind="timeout",
            text=f"{cfg.label} timed out after {e.elapsed_s:.0f}s",
            model=cfg.label,
            telemetry=ProviderTelemetry(
                oracle_key=cfg.key,
                requested_model=cfg.model,
                actual_model=cfg.model,
                transport="http-json",
                retry_count=e.retry_count,
                wall_duration_ms=(time.perf_counter() - started) * 1000,
                reasoning_available=False,
                usage_available=False,
                tools_available=False,
            ),
        )
    if err:
        kind, text = http_error_detail(
            label=cfg.label,
            error=err,
            http_status=transport.get("http_status"),
        )
        return OracleResult(
            "error",
            kind=kind,
            text=text,
            model=cfg.label,
            telemetry=ProviderTelemetry(
                oracle_key=cfg.key,
                requested_model=cfg.model,
                actual_model=cfg.model,
                transport="http-json",
                provider_request_id=transport.get("request_id"),
                http_status=transport.get("http_status"),
                retry_count=retry_count,
                wall_duration_ms=(time.perf_counter() - started) * 1000,
                reasoning_available=False,
                usage_available=False,
                tools_available=False,
            ),
        )
    shaped = shape(text)
    shaped.model = cfg.label
    shaped.thinking = thinking
    raw_usage = body_meta.get("usage") or {}
    usage = (
        usage_if_available(
            ProviderUsage(
                input_tokens=token_count(raw_usage.get("input_tokens")),
                output_tokens=token_count(raw_usage.get("output_tokens")),
                cache_read_input_tokens=token_count(raw_usage.get("cache_read_input_tokens")),
                cache_creation_input_tokens=token_count(
                    raw_usage.get("cache_creation_input_tokens")
                ),
            )
        )
        if isinstance(raw_usage, dict) and raw_usage
        else None
    )
    actual_model = body_meta.get("model") or cfg.model
    shaped.model = actual_model
    shaped.telemetry = ProviderTelemetry(
        oracle_key=cfg.key,
        requested_model=cfg.model,
        actual_model=actual_model,
        transport="anthropic-http",
        provider_request_id=transport.get("request_id") or body_meta.get("id"),
        stop_reason=body_meta.get("stop_reason"),
        http_status=200,
        retry_count=retry_count,
        wall_duration_ms=(time.perf_counter() - started) * 1000,
        reasoning_available=bool(thinking),
        usage_available=usage is not None,
        tools_available=False,
        usage=usage,
    )
    return shaped
