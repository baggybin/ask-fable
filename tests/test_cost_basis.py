"""Billed spend and subscription list prices are different currencies.

A council can mix an OAuth-backed Anthropic oracle (flat plan — the SDK reports
a LIST price, marginal cost zero) with a per-token gateway (real money). Summing
the two produced a `cost_usd` that was neither real spend nor list price, which
is worse than reporting nothing: it reads as an authoritative bill. These pin the
two apart. No model is ever called.
"""

from __future__ import annotations

import pytest

import ask_fable.trace_runtime as trace_runtime
from ask_fable.provider_telemetry import ProviderTelemetry, ProviderUsage, usage_if_available


def _usage(cost, basis, **kw):
    return ProviderUsage(cost_usd=cost, cost_basis=basis, **kw)


# --- the per-provider marker ----------------------------------------------


def test_basis_rides_alongside_a_cost():
    block = _usage(0.5, "billed", input_tokens=10).to_event_dict()
    assert block["cost_usd"] == 0.5 and block["cost_basis"] == "billed"


def test_basis_alone_does_not_invent_usage():
    """`cost_basis` is a label, not data — it must not make an empty usage look
    populated, or every costless provider would start reporting a usage block."""
    assert usage_if_available(ProviderUsage(cost_basis="billed")) is None
    assert ProviderUsage(cost_basis="billed").to_event_dict() == {}


def test_basis_is_dropped_when_there_is_no_cost():
    block = ProviderUsage(input_tokens=10, cost_basis="billed").to_event_dict()
    assert "cost_basis" not in block and block["input_tokens"] == 10


# --- the aggregate ---------------------------------------------------------


def _run_with(*usages):
    """Drive a real council trace and read back the aggregated usage block."""
    with trace_runtime.tool_trace("ask_council", {}) as trace:
        for u in usages:
            trace_runtime.record_provider(
                ProviderTelemetry(oracle_key="x", transport="http-json", usage=u), "ok"
            )
        payload = trace.complete({"status": "ok"})
    return payload.get("usage") or {}


def test_billed_and_subscription_are_never_summed():
    """The headline bug: 4 billed gateway calls + 1 Fable synthesis reported one
    number that mixed real dollars with a flat-plan list price."""
    agg = _run_with(
        _usage(0.01, "billed"),
        _usage(0.02, "billed"),
        _usage(0.48, "subscription"),
    )
    assert agg["cost_usd"] == pytest.approx(0.03)  # what you actually pay
    assert agg["cost_usd_notional"] == pytest.approx(0.48)  # covered by the plan
    assert agg["coverage_count"] == 3


def test_an_all_subscription_run_reports_no_billed_cost():
    agg = _run_with(_usage(0.4, "subscription"), _usage(0.2, "subscription"))
    assert "cost_usd" not in agg
    assert agg["cost_usd_notional"] == pytest.approx(0.6)


def test_an_all_billed_run_is_unchanged():
    """Councils that never touch a subscription oracle must report exactly what
    they did before — those numbers were always correct."""
    agg = _run_with(_usage(0.012, "billed"), _usage(0.011, "billed"))
    assert agg["cost_usd"] == pytest.approx(0.023)
    assert "cost_usd_notional" not in agg


def test_an_unlabelled_cost_counts_as_billed():
    """Backends that report a cost without a basis are direct APIs, so treating
    an unlabelled cost as real spend is the safe default — it can overstate the
    bill, never understate it."""
    agg = _run_with(_usage(0.05, None))
    assert agg["cost_usd"] == pytest.approx(0.05)


def test_tokens_still_aggregate_across_both():
    agg = _run_with(
        _usage(0.01, "billed", input_tokens=100, output_tokens=10),
        _usage(0.40, "subscription", input_tokens=900, output_tokens=90),
    )
    assert agg["input_tokens"] == 1000 and agg["output_tokens"] == 100
