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
    DROPPED_CONNECTION_ERRORS,
    RETRYABLE_HTTP_STATUSES,
    OracleResult,
    TransientRetryTimeout,
    call_with_transient_retry,
    compose,
    error_status,
    extract_http_error,
    http_error_detail,
    is_provider_refusal,
    max_tokens,
    parse_anthropic_body,
    shape_stopped,
    timeout_default,
    websearch_max_turns,
)
from .prompts import ORACLE_SYSTEM_PROMPT
from .provider_telemetry import (
    ProviderTelemetry,
    ProviderUsage,
    ToolEvent,
    token_count,
    usage_if_available,
)


def websearch_max_uses() -> int:
    """Search budget for one server-side web-search turn.

    Reuses ``ASK_FABLE_WEBSEARCH_MAX_TURNS`` (default 20) — for a server-side tool the
    unit is searches, not agent turns, but it is the same knob an operator already
    tunes, and mapping it here avoids a second setting that means almost the same
    thing. The API charges per search, so this is also the cost ceiling."""
    return websearch_max_turns()

# Built-in provider defaults. The API key is ALWAYS from env (no default) — a
# provider with no key configured is simply unavailable.
_DEFAULTS = {
    "glm": {"base_url": "https://api.z.ai/api/anthropic", "model": "glm-5.2"},
    # deepseek-flash = DeepSeek-V4.1-Flash (1M ctx, thinking). The older
    # deepseek-v4-pro retires 2026-09-14 (routed to V4.1-Flash anyway, and DeepSeek
    # says Flash now outperforms it). A V4.1-Pro is signalled but unreleased — move
    # the default here, or set ASK_FABLE_DEEPSEEK_MODEL, once it ships.
    "deepseek": {"base_url": "https://api.deepseek.com/anthropic", "model": "deepseek-flash"},
}


# Server-side web search (no client tool loop — Anthropic runs the searches).
#
# The BASIC tool version, deliberately. Later versions are not drop-in upgrades:
# `web_search_20260209`+ default `allowed_callers` to code execution and return a
# 400 on a model without programmatic tool calling unless you also set
# `allowed_callers: ["direct"]`, and `web_search_20260318` adds response-inclusion
# control. Basic is accepted by every tier and searches directly (verified against
# the live tool docs, 2026-09-20 — and it is what the installed Claude Code 2.1.278
# uses too). Moving up is a two-line change plus the `allowed_callers` field.
WEB_SEARCH_TOOL = "web_search_20250305"

# The API pauses a long server-side search turn (~10 internal iterations) with
# `stop_reason: "pause_turn"`; resuming means re-sending the paused turn. Capped so
# a pathological prompt cannot loop forever.
_MAX_CONTINUATIONS = 4

# Result URLs kept on the answer's `sources` — the answer itself is the payload, this
# is provenance.
_MAX_SOURCES = 25


@dataclass(frozen=True, slots=True)
class _SearchReport:
    """What the server-side searches did this turn.

    Success and failure arrive in the SAME 200 response, distinguished only by the
    shape of a result block's `content`: a list of `web_search_result` blocks when
    the search ran (an empty list means it matched nothing — still a success), or a
    single error object when it did not."""

    cited: list[str]  # URLs the answer cites
    results: list[str]  # URLs the searches returned
    errors: list[str]  # error codes, one per failed search
    statuses: list[str]  # per-search: "ok" or the error code

    @property
    def searches(self) -> int:
        return len(self.statuses)

    @property
    def ok_searches(self) -> int:
        return sum(1 for status in self.statuses if status == "ok")


def _search_report(blocks: list) -> _SearchReport:
    """Collect the search facts from a turn's content blocks (see `_SearchReport`)."""
    cited: list[str] = []
    results: list[str] = []
    errors: list[str] = []
    statuses: list[str] = []
    for block in blocks:
        for citation in block.get("citations") or []:
            url = citation.get("url") if isinstance(citation, dict) else None
            if isinstance(url, str) and url:
                cited.append(url)
        if block.get("type") != "web_search_tool_result":
            continue
        content = block.get("content")
        if isinstance(content, list):
            statuses.append("ok")
            results.extend(
                item["url"]
                for item in content
                if isinstance(item, dict) and isinstance(item.get("url"), str)
            )
        else:
            code = str(
                (content or {}).get("error_code")
                if isinstance(content, dict)
                else "unavailable"
            )
            errors.append(code)
            statuses.append(code)
    return _SearchReport(
        cited=_dedupe(cited), results=_dedupe(results), errors=errors, statuses=statuses
    )


def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    return [url for url in urls if not (url in seen or seen.add(url))]


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
    system_prompt: str | None = None,
    web_search: bool = False,
) -> OracleResult:
    """Run one turn against an Anthropic-compatible endpoint. Never raises for
    expected failures (network, HTTP, timeout, bad key).

    ``system_prompt`` overrides the default oracle scope contract — glm/deepseek
    omit it, but the Fable-over-HTTP transport passes ``FABLE_SYSTEM_PROMPT`` (or a
    caller's own prompt), and answering under the wrong contract would be a silent
    change of oracle rather than a transport fallback.

    ``web_search`` declares the API's **server-side** web-search tool: the searches
    run on Anthropic's infrastructure, so this needs no client tool loop. See
    :data:`WEB_SEARCH_TOOL` for why the basic tool version is the one used."""
    timeout = timeout if timeout is not None else timeout_default()
    messages: list[dict] = [{"role": "user", "content": compose(question, context)}]

    def _request() -> urllib.request.Request:
        body: dict = {
            "model": cfg.model,
            "max_tokens": max_tokens(),
            "system": system_prompt or ORACLE_SYSTEM_PROMPT,
            "messages": messages,
        }
        if web_search:
            body["tools"] = [{"type": WEB_SEARCH_TOOL, "name": "web_search",
                              "max_uses": websearch_max_uses()}]
        return urllib.request.Request(
            f"{cfg.base_url}/v1/messages",
            data=json.dumps(body).encode("utf-8"),
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
            with urllib.request.urlopen(_request(), timeout=timeout) as resp:
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
        except DROPPED_CONNECTION_ERRORS as e:
            transport["dropped"] = True
            return None, "", extract_http_error(e)

    started = time.perf_counter()

    async def _turn() -> tuple[tuple[str | None, str, str | None], int]:
        """One request, with the shared bounded retry on transient statuses."""
        return await call_with_transient_retry(
            _call,
            timeout=timeout,
            # One bounded retry on transient statuses (429 / 5xx) or a dropped
            # connection — a single hiccup shouldn't hard-fail a council panelist.
            retryable=lambda r: bool(r[2]) and (
                transport.get("http_status") in RETRYABLE_HTTP_STATUSES
                or bool(transport.get("dropped"))
            ),
        )

    # Content blocks and usage accumulate ACROSS turns: a resumed search turn
    # reports only its own slice, so reading the last response alone would lose the
    # searches (and their cost) already spent on this answer.
    all_blocks: list = []
    usage_total: dict = {}

    def _absorb() -> None:
        blocks = body_meta.get("content")
        if isinstance(blocks, list):
            all_blocks.extend(b for b in blocks if isinstance(b, dict))
        raw = body_meta.get("usage")
        if not isinstance(raw, dict):
            return
        for name, value in raw.items():
            if isinstance(value, int):
                usage_total[name] = usage_total.get(name, 0) + value
        searches = (raw.get("server_tool_use") or {}).get("web_search_requests")
        if isinstance(searches, int):
            usage_total["web_search_requests"] = (
                usage_total.get("web_search_requests", 0) + searches
            )

    try:
        (text, thinking, err), retry_count = await _turn()
        _absorb()
        # The API runs its own server-side search loop and pauses a long turn
        # (~10 internal iterations) with `stop_reason: "pause_turn"`. The resume is
        # a re-send of the paused assistant turn, unchanged and with NO extra user
        # message — the API detects the trailing server_tool_use and continues.
        # Capped: a pathological prompt must not loop forever.
        continuations = 0
        while (
            not err and web_search and body_meta.get("stop_reason") == "pause_turn"
            and continuations < _MAX_CONTINUATIONS
        ):
            continuations += 1
            messages.append({"role": "assistant", "content": body_meta.get("content") or []})
            (more_text, more_thinking, err), more_retries = await _turn()
            _absorb()
            retry_count += more_retries
            text = f"{text or ''}{more_text or ''}" or None
            thinking = "\n".join(t for t in (thinking, more_thinking) if t)
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
            error_status(kind),
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
    report = _search_report(all_blocks)
    if web_search and report.searches and not report.ok_searches:
        # Every search this turn errored (the API reports that INSIDE a 200, as a
        # `web_search_tool_result` whose content is an error object rather than a
        # list of results). Answering "ok" here would hand back a research answer
        # with no research behind it — the one thing ask_websearch must never do.
        code = report.errors[0]
        kind = "rate_limit" if code == "too_many_requests" else "sdk_error"
        return OracleResult(
            "error",
            kind=kind,
            text=(
                f"{cfg.label} web search failed ({', '.join(report.errors)}); "
                f"{report.searches} search(es) attempted, none returned results"
            ),
            model=cfg.label,
            telemetry=ProviderTelemetry(
                oracle_key=cfg.key, requested_model=cfg.model, actual_model=cfg.model,
                transport="anthropic-http",
                provider_request_id=transport.get("request_id") or body_meta.get("id"),
                stop_reason=body_meta.get("stop_reason"),
                retry_count=retry_count,
                wall_duration_ms=(time.perf_counter() - started) * 1000,
                reasoning_available=bool(thinking), usage_available=False, tools_available=True,
            ),
        )

    raw_usage = usage_total
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
        if raw_usage
        else None
    )
    actual_model = body_meta.get("model") or cfg.model
    stop_reason = body_meta.get("stop_reason")
    telemetry = ProviderTelemetry(
        oracle_key=cfg.key,
        requested_model=cfg.model,
        actual_model=actual_model,
        transport="anthropic-http",
        provider_request_id=transport.get("request_id") or body_meta.get("id"),
        stop_reason=stop_reason,
        http_status=200,
        retry_count=retry_count,
        wall_duration_ms=(time.perf_counter() - started) * 1000,
        reasoning_available=bool(thinking),
        usage_available=usage is not None,
        tools_available=web_search,
        tool_events=tuple(
            ToolEvent(name="web_search", status=status) for status in report.statuses
        ),
        usage=usage,
    )
    provider_reason = is_provider_refusal(stop_reason)
    if provider_reason is not None:
        # The API's own safeguard declined, inside a 200: before any output (empty
        # content, which `shape` would call an sdk_error and feed the breaker) or
        # MID-STREAM (partial text that would pass as a complete answer and be
        # cached for an hour). Either way it is a refusal, reported exactly as the
        # SDK path reports one (fable.py) — and the partial text is discarded.
        return OracleResult(
            "refused",
            kind="provider_refusal",
            text=provider_reason,
            model=actual_model,
            telemetry=telemetry,
        )
    shaped = shape_stopped(text, stop_reason, label=cfg.label, thinking=thinking)
    shaped.model = actual_model
    shaped.thinking = thinking
    shaped.telemetry = telemetry
    if web_search:
        # Caller-visible search facts. `sources` prefers the URLs the answer actually
        # CITES (each text block carries a `web_search_result_location` citation),
        # falling back to the result URLs it was shown; the count is what the API
        # bills for ($10/1,000 searches — the one cost component knowable exactly
        # here, since tokens have no price table on this path).
        backing = report.cited or report.results
        if backing:
            shaped.meta["sources"] = backing[:_MAX_SOURCES]
        # The API's own count is authoritative — it is what the $10/1,000 rate is
        # charged against — so prefer it over counting the result blocks we saw.
        billed = usage_total.get("web_search_requests")
        if isinstance(billed, int) and billed:
            shaped.meta["web_search_requests"] = billed
        elif report.searches:
            shaped.meta["web_search_requests"] = report.searches
        if report.errors:
            shaped.meta["search_errors"] = report.errors
    return shaped
