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


def test_disk_writers_honor_config_trace_mode_over_env(monkeypatch):
    """F1 (bug hunt 2026-09-09): the bundle writer and answer-saver read trace_mode
    from config-over-env like ToolTrace.mode does — so configure_tracing actually
    governs disk writes. Before, they read os.environ directly: a runtime 'safe'
    toggle still wrote bundles, and a config 'full' over an unset env wrote none."""
    import ask_fable.outputs as outputs
    import ask_fable.trace_bundle as trace_bundle

    # env DEFAULTS to full; an in-session toggle to safe must stop the disk writers
    monkeypatch.setenv("ASK_FABLE_TRACE_MODE", "full")
    monkeypatch.delenv("ASK_FABLE_SAVE", raising=False)
    server._handle_configure_tracing({"trace_mode": "safe"})
    assert trace_bundle.write("tid", {"x": 1}) is None  # was writing despite "safe"
    assert outputs._enabled() is False  # was saving despite "safe"

    # reverse: config 'full' with env unset must ENABLE the saver (was inert)
    monkeypatch.delenv("ASK_FABLE_TRACE_MODE", raising=False)
    server._handle_configure_tracing({"trace_mode": "full"})
    assert outputs._enabled() is True
