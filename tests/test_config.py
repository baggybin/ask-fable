"""ask_fable config store + its interaction with ollama model precedence."""

from __future__ import annotations

import ask_fable.config as cfg
import ask_fable.ollama as ol


def test_missing_config_reads_empty():
    assert cfg.load() == {}
    assert cfg.get_str("ollama_model") is None
    assert cfg.get_list("ollama_council") is None


def test_save_merges_and_persists():
    p1 = cfg.save({"ollama_council": ["a:cloud", "b:cloud"]})
    assert p1 is not None
    p2 = cfg.save({"ollama_model": "gpt-oss:120b-cloud"})
    assert p2 == p1  # same file
    loaded = cfg.load()
    assert loaded["ollama_council"] == ["a:cloud", "b:cloud"]
    assert loaded["ollama_model"] == "gpt-oss:120b-cloud"


def test_save_none_removes_key():
    cfg.save({"ollama_model": "x:cloud"})
    assert cfg.get_str("ollama_model") == "x:cloud"
    cfg.save({"ollama_model": None})
    assert cfg.get_str("ollama_model") is None


def test_get_list_accepts_string_form():
    cfg.save({"ollama_council": "a:cloud, b:cloud  c:cloud"})
    assert cfg.get_list("ollama_council") == ["a:cloud", "b:cloud", "c:cloud"]


def test_setting_prefers_config_over_env(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_TRACE_MODE", "safe")
    assert cfg.setting("ASK_FABLE_TRACE_MODE") == "safe"   # env when no config override
    cfg.save({"ASK_FABLE_TRACE_MODE": "full"})
    assert cfg.setting("ASK_FABLE_TRACE_MODE") == "full"   # config overrides env
    monkeypatch.delenv("ASK_FABLE_TRACE_MODE", raising=False)
    assert cfg.setting("ASK_FABLE_MISSING") is None        # neither set → None


def test_unparseable_file_reads_empty(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("ASK_FABLE_CONFIG_FILE", str(bad))
    assert cfg.load() == {}


def test_council_precedence_config_over_env(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_OLLAMA_COUNCIL", "envmodel:cloud")
    # env wins over the built-in default...
    assert ol.council_models() == ["envmodel:cloud"]
    # ...but the config file wins over env.
    cfg.save({"ollama_council": ["ollama:cfg-a:cloud", "cfg-b:cloud"]})
    assert ol.council_models() == ["cfg-a:cloud", "cfg-b:cloud"]  # prefix stripped


def test_council_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_OLLAMA_COUNCIL", raising=False)
    assert ol.council_models() == list(ol.DEFAULT_OLLAMA_COUNCIL)
    assert "minimax-m3:cloud" in ol.DEFAULT_OLLAMA_COUNCIL
    assert "glm-5.2:cloud" in ol.DEFAULT_OLLAMA_COUNCIL
    assert "nemotron-3-ultra:cloud" in ol.DEFAULT_OLLAMA_COUNCIL


def test_default_model_precedence(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_OLLAMA_MODEL", "env:cloud")
    assert ol.default_model() == "env:cloud"
    cfg.save({"ollama_model": "cfg:cloud"})
    assert ol.default_model() == "cfg:cloud"


def test_cloud_id_normalization():
    assert ol.cloud_id("minimax-m3") == "minimax-m3:cloud"      # untagged -> :cloud
    assert ol.cloud_id("gpt-oss:120b") == "gpt-oss:120b-cloud"  # tagged  -> -cloud
    assert ol.cloud_id("glm-5.2:cloud") == "glm-5.2:cloud"      # already suffixed
    assert ol.cloud_id("gpt-oss:120b-cloud") == "gpt-oss:120b-cloud"
    assert ol.cloud_id("  ") == ""
