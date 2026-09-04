"""configure_tracing — runtime toggle of trace mode + live reasoning streaming.

The config file is auto-isolated per test by conftest, so each test starts with
an empty config (no overrides).
"""

from __future__ import annotations

import pytest

import ask_fable.config as cfg
import ask_fable.server as server


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")


def test_enable_full_and_streaming(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_TRACE_MODE", raising=False)
    monkeypatch.delenv("ASK_FABLE_STREAM_REASONING", raising=False)
    out = server._handle_configure_tracing({"trace_mode": "full", "stream_reasoning": True})
    assert out["status"] == "ok"
    assert out["trace_mode"] == "full" and out["stream_reasoning"] is True
    # takes effect immediately — the live readers see the new values, no restart
    assert cfg.setting("ASK_FABLE_TRACE_MODE") == "full"
    assert server._flag("ASK_FABLE_STREAM_REASONING") is True


def test_config_overrides_env(monkeypatch):
    # env says one thing; an in-session toggle must win (config precedence)
    monkeypatch.setenv("ASK_FABLE_TRACE_MODE", "safe")
    monkeypatch.setenv("ASK_FABLE_STREAM_REASONING", "1")
    assert server._flag("ASK_FABLE_STREAM_REASONING") is True
    server._handle_configure_tracing({"trace_mode": "full", "stream_reasoning": False})
    assert cfg.setting("ASK_FABLE_TRACE_MODE") == "full"        # overrides env "safe"
    assert server._flag("ASK_FABLE_STREAM_REASONING") is False  # overrides env "1"


def test_partial_update_merges():
    server._handle_configure_tracing({"trace_mode": "full"})
    out = server._handle_configure_tracing({"stream_reasoning": False})
    assert out["trace_mode"] == "full"          # earlier setting preserved
    assert out["stream_reasoning"] is False


def test_rejects_bad_input():
    assert server._handle_configure_tracing({})["kind"] == "bad_args"
    assert server._handle_configure_tracing({"trace_mode": "loud"})["kind"] == "bad_args"


def test_registered_as_a_tool():
    assert "configure_tracing" in server._TOOL_SCHEMAS
