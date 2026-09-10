"""Unit tests for hub agent_id resolution (env + parent fallbacks)."""

from __future__ import annotations

from types import SimpleNamespace

import ask_fable.server as server


class _NoClientServer:
    """Server stub whose request_context has no usable clientInfo."""

    class _Session:
        client_params = None

    class _Ctx:
        session = None

    def __init__(self):
        self._Ctx.session = self._Session()
        self.request_context = self._Ctx()


class _NamedClientServer:
    def __init__(self, name: str):
        info = SimpleNamespace(name=name)
        params = SimpleNamespace(clientInfo=info)
        session = SimpleNamespace(client_params=params)
        ctx = SimpleNamespace(session=session)
        self.request_context = ctx


def _clear_hints(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_AGENT_ID", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.delenv("AI_AGENT", raising=False)
    monkeypatch.delenv("GROK_AGENT", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_CI", raising=False)
    monkeypatch.delenv("OPENCODE_XATS_BASE_URL", raising=False)
    monkeypatch.setattr(server, "_agent_id_from_parent", lambda: None)
    server._unknown_agent_warned = True  # quiet stderr in tests


def test_agent_id_explicit_env_wins(monkeypatch):
    _clear_hints(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_AGENT_ID", "backend")
    assert server._agent_id(_NamedClientServer("claude-code")) == "backend"


def test_agent_id_client_info_name(monkeypatch):
    _clear_hints(monkeypatch)
    assert server._agent_id(_NamedClientServer("grok-shell-ask_fable")) == "grok-shell-ask_fable"


def test_agent_id_empty_client_info_falls_through_to_env(monkeypatch):
    _clear_hints(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert server._agent_id(_NamedClientServer("")) == "claude-code"
    assert server._agent_id(_NamedClientServer("unknown")) == "claude-code"


def test_agent_id_env_grok(monkeypatch):
    _clear_hints(monkeypatch)
    monkeypatch.setenv("GROK_AGENT", "1")
    assert server._agent_id(_NoClientServer()) == "grok"


def test_agent_id_env_codex_thread(monkeypatch):
    _clear_hints(monkeypatch)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-abc")
    assert server._agent_id(_NoClientServer()) == "codex"


def test_agent_id_parent_fallback(monkeypatch):
    _clear_hints(monkeypatch)
    monkeypatch.setattr(server, "_agent_id_from_parent", lambda: "opencode")
    assert server._agent_id(_NoClientServer()) == "opencode"


def test_agent_id_unknown_when_nothing_matches(monkeypatch):
    _clear_hints(monkeypatch)
    assert server._agent_id(_NoClientServer()) == "unknown"
