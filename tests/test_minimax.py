"""ask_fable MiniMax bridge — envelope parsing, CLI argv, REFUSED contract.

The ``mmx`` CLI is mocked via ``subprocess.Popen`` (own-session + group-kill
pattern, same as grok/codex); no real model is called.
"""

from __future__ import annotations

import asyncio
import json

import ask_fable.minimax as minimax
from ask_fable import cli_gate


def _run(coro):
    return asyncio.run(coro)


def _envelope(text="", thinking="", status_code=0, status_msg=""):
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    content.append({"type": "text", "text": text})
    return json.dumps(
        {
            "type": "message",
            "role": "assistant",
            "model": "MiniMax-M3",
            "content": content,
            "base_resp": {"status_code": status_code, "status_msg": status_msg},
        }
    )


from conftest import FakePopen as _FakePopen


def _patch(monkeypatch, seen=None, **cfg):
    monkeypatch.setattr(minimax.shutil, "which", lambda b: "/usr/bin/mmx")
    fake = _FakePopen(**cfg)

    def _factory(argv, **kwargs):
        if seen is not None:
            seen["argv"] = argv
            seen["kwargs"] = kwargs
        return fake

    monkeypatch.setattr(cli_gate.subprocess, "Popen", _factory)
    return fake


def test_extract_ok_and_thinking():
    text, thinking, err = minimax._extract(_envelope("The router dispatches.", "let me think"))
    assert text == "The router dispatches." and thinking == "let me think" and err is None


def test_extract_api_error():
    text, thinking, err = minimax._extract(_envelope(status_code=1024, status_msg="rate limited"))
    assert text is None and "1024" in err and "rate limited" in err


def test_extract_plaintext_fallback():
    text, thinking, err = minimax._extract("just a plain answer")
    assert text == "just a plain answer" and err is None


def test_run_ok_and_argv(monkeypatch):
    seen = {}
    fake = _patch(monkeypatch, seen,
                  stdout=_envelope("Handlers register in build_server().", "reasoning here"))
    res = _run(minimax.run("How are handlers registered?", "def build(): ..."))
    assert res.status == "ok" and "Handlers" in res.text
    assert res.thinking == "reasoning here" and res.model == "MiniMax-M3"
    argv = seen["argv"]
    assert argv[1:3] == ["text", "chat"] and "--output" in argv and "json" in argv
    # own session so a hung grandchild can be group-killed
    assert seen["kwargs"].get("start_new_session") is True
    # output cap is passed through (default raised to 65536)
    assert "--max-tokens" in argv and argv[argv.index("--max-tokens") + 1] == "65536"
    # messages travel on stdin as a JSON array, not argv
    msgs = json.loads(fake.seen_input)
    assert msgs[0]["role"] == "user" and msgs[0]["content"].startswith("QUESTION:")
    assert not any("QUESTION" in a for a in argv)
    assert "--quiet" not in argv


def test_run_refused(monkeypatch):
    _patch(monkeypatch, stdout=_envelope("REFUSED: not about code"))
    res = _run(minimax.run("what is the best language overall really"))
    assert res.status == "refused" and res.text == "not about code"


def test_run_model_override(monkeypatch):
    seen = {}
    monkeypatch.setenv("ASK_FABLE_MINIMAX_MODEL", "MiniMax-M3-highspeed")
    _patch(monkeypatch, seen, stdout=_envelope("ok"))
    res = _run(minimax.run("How does dispatch work here?"))
    assert res.model == "MiniMax-M3-highspeed"
    assert seen["argv"][seen["argv"].index("--model") + 1] == "MiniMax-M3-highspeed"


def test_run_timeout_kills_group_and_releases(monkeypatch):
    fake = _patch(monkeypatch, timeout_first=True)
    res = _run(minimax.run("Trace dispatch in this module.", timeout=1))
    assert res.status == "error" and res.kind == "timeout"
    assert fake.killed  # the group/process was hard-killed, not left holding the gate
    assert fake._calls == 2  # bounded post-kill drain happened


def test_run_nonzero(monkeypatch):
    _patch(monkeypatch, returncode=1, stderr="kaboom")
    res = _run(minimax.run("Where is the router defined here?"))
    assert res.status == "error" and res.kind == "sdk_error"
    assert "kaboom" in res.text


def test_run_quota_is_rate_limit(monkeypatch):
    err = json.dumps({
        "error": {
            "code": 4,
            "message": "Rate limit or quota exceeded. Token Plan usage limit reached.",
            "hint": "Check usage: mmx quota show",
        }
    })
    _patch(monkeypatch, returncode=4, stderr=err)
    res = _run(minimax.run("Where is the router defined here?"))
    assert res.status == "error" and res.kind == "rate_limit"
    assert "quota" in res.text.lower()
    assert "mmx quota show" in res.text


def test_run_binary_missing(monkeypatch):
    called = {"spawned": False}
    monkeypatch.setattr(minimax.shutil, "which", lambda b: None)
    monkeypatch.setattr(
        cli_gate.subprocess, "Popen", lambda *a, **k: called.__setitem__("spawned", True)
    )
    res = _run(minimax.run("How does dispatch work in this file?"))
    assert res.status == "error" and res.kind == "binary_missing"
    assert called["spawned"] is False  # never spawned
