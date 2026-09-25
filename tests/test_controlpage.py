"""ask_fable control-page client — snapshot parsing, GPU normalization, summarize."""

from __future__ import annotations

import urllib.error

import pytest

import ask_fable.controlpage as cp

SNAP = {
    "gpu": {
        "available": True,
        "name": "NVIDIA RTX PRO 5000 Blackwell",
        "util_pct": 37,
        "vram_used_mib": 24589,
        "vram_total_mib": 48935,
        "temp_c": 43,
        "fan_pct": 30,
        "power_w": 61.5,
        "power_limit_w": 300.0,
    },
    "vram_holders": [
        {"pid": 385609, "process": "/home/user/.lmstudio/llmworker", "vram_mib": 23982}
    ],
    "services": {"lmstudio": "active", "wolf": "inactive", "ollama": "active"},
    "lmstudio": {
        "active": True,
        "engine_up": True,
        "port": 1234,
        "loaded_models": ["text-embedding-nomic-embed-text-v1.5", "qwen3.8-27b-distill-q38"],
        "models": ["a", "b", "c"],
    },
    "ollama": {"status": "idle", "installed_models": [{}, {}]},
    "litellm": {"checked": True, "is_default": True, "key_preview": "sk-local..."},
    "mountain": {"health": {"ok": True, "version": "0.13.0"}},
    "snapshot_age_s": 2.5,
}


def test_base_url_precedence(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_CONTROL_URL", raising=False)
    monkeypatch.setattr(cp.config, "get_str", lambda key: None)
    assert cp.base_url() == cp.DEFAULT_CONTROL_URL
    monkeypatch.setenv("ASK_FABLE_CONTROL_URL", "http://box:5000/")
    assert cp.base_url() == "http://box:5000"
    monkeypatch.setattr(cp.config, "get_str", lambda key: "http://config:5000")
    assert cp.base_url() == "http://config:5000"  # config file wins over env


def test_gpu_normalizes_and_computes_free():
    g = cp.gpu(SNAP)
    assert g["available"] is True
    assert g["vram_used_mib"] == 24589 and g["vram_total_mib"] == 48935
    assert g["vram_free_mib"] == 48935 - 24589
    assert g["util_pct"] == 37 and g["temp_c"] == 43
    assert g["snapshot_age_s"] == 2.5


def test_gpu_unknown_is_not_mistaken_for_room():
    for snap in ({}, {"gpu": {"available": False}}, {"gpu": {"available": True, "vram_used_mib": None}}):
        g = cp.gpu(snap)
        assert g["available"] is False
        assert g["vram_free_mib"] is None


def test_summarize_curates_the_host_view():
    out = cp.summarize(SNAP)
    assert out["services"]["active"] == ["lmstudio", "ollama"]
    assert out["services"]["inactive"] == ["wolf"]
    assert out["lmstudio"]["loaded_models"] == [
        "text-embedding-nomic-embed-text-v1.5",
        "qwen3.8-27b-distill-q38",
    ]
    assert out["lmstudio"]["models_count"] == 3
    assert out["ollama"] == {"status": "idle", "installed_count": 2}
    assert out["mountain"]["version"] == "0.13.0"
    assert out["warnings"] and "LiteLLM" in out["warnings"][0]
    assert out["vram_holders"][0]["vram_mib"] == 23982


@pytest.fixture(autouse=True)
def _clear_monitor_cache():
    cp._MONITOR_CACHE.clear()
    yield
    cp._MONITOR_CACHE.clear()


def _fix_control(monkeypatch, url: str) -> None:
    monkeypatch.setattr(cp.config, "get_str", lambda key: None)
    monkeypatch.setenv("ASK_FABLE_CONTROL_URL", url)


def test_monitors_true_for_the_same_host_literal(monkeypatch):
    _fix_control(monkeypatch, "http://192.0.2.10:5000")
    assert cp.monitors("192.0.2.10") is True


def test_monitors_compares_hosts_by_resolution(monkeypatch):
    _fix_control(monkeypatch, "http://lmstudio.example.com:5000")
    answers = {
        "lmstudio.example.com": ["192.0.2.10"],
        "192.0.2.10": ["192.0.2.10"],
        "lmstudio-host": ["203.0.113.7"],
    }
    monkeypatch.setattr(
        cp.socket,
        "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", (ip, 0)) for ip in answers[host]],
    )
    assert cp.monitors("192.0.2.10") is True  # a name and its IP are one machine
    assert cp.monitors("lmstudio-host") is False  # a different machine


def test_monitors_is_false_when_resolution_fails(monkeypatch):
    _fix_control(monkeypatch, "http://lmstudio.example.com:5000")

    def _fail(host, port):
        raise cp.socket.gaierror("no such host")

    monkeypatch.setattr(cp.socket, "getaddrinfo", _fail)
    assert cp.monitors("lmstudio-host") is False


def test_monitors_caches_briefly(monkeypatch):
    _fix_control(monkeypatch, "http://lmstudio.example.com:5000")
    calls = []

    def _resolve(host, port):
        calls.append(host)
        return [(2, 1, 6, "", ("192.0.2.10", 0))]

    monkeypatch.setattr(cp.socket, "getaddrinfo", _resolve)
    assert cp.monitors("box") is True
    assert cp.monitors("box") is True
    assert calls.count("lmstudio.example.com") == 1  # second call served from the cache


def test_status_returns_none_when_unreachable(monkeypatch):
    class _Boom:
        def open(self, req, timeout=None):
            raise urllib.error.URLError("refused")

    monkeypatch.setattr(cp, "_get_opener", lambda: _Boom())
    assert cp.status() is None
