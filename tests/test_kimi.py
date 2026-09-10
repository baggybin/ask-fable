"""Local Kimi Code bridge — sandbox generation, model mapping, stream-json parsing."""

from __future__ import annotations

import asyncio
import tomllib

import ask_fable.kimi as kimi
import ask_fable.oracles as oracles
from ask_fable.cli_gate import CliRun
from ask_fable.oracle_common import OracleResult


def _run(coro):
    return asyncio.run(coro)


# A miniature stand-in for the operator's config: a bare key, provider/model
# tables, and an existing [thinking] table we have to displace.
SAMPLE_CONFIG = """default_model = "kimi-code/k3"

[thinking]
enabled = true
effort = "low"

[providers."managed:kimi-code"]
type = "kimi"
base_url = "https://api.kimi.com/coding/v1"

[models."kimi-code/k3"]
provider = "managed:kimi-code"
max_context_size = 1048576
display_name = "K3"
"""


def test_render_config_is_valid_toml_and_keeps_models():
    """The generated config must PARSE. A bare key appended after a [table] would
    silently become a field of that table, and a second [thinking] is a hard TOML
    error — both of which cost the CLI its model aliases."""
    out = kimi.render_config(SAMPLE_CONFIG, "max")
    d = tomllib.loads(out)  # raises on duplicate tables
    # Operator content survives.
    assert "kimi-code/k3" in d["models"]
    assert d["models"]["kimi-code/k3"]["max_context_size"] == 1048576
    assert "managed:kimi-code" in d["providers"]
    assert d["default_model"] == "kimi-code/k3"
    # Our overlay landed at TOP level, not nested inside the last table.
    assert d["default_permission_mode"] == "manual"
    assert d["permission"]["rules"] == [{"decision": "deny", "scope": "user", "pattern": "*"}]
    # Our [thinking] replaced theirs rather than duplicating it.
    assert d["thinking"] == {"enabled": True, "effort": "max"}


def test_render_config_survives_config_with_no_thinking_table():
    d = tomllib.loads(kimi.render_config('default_model = "kimi-code/k3"\n', "low"))
    assert d["thinking"]["effort"] == "low"
    assert d["default_permission_mode"] == "manual"


def test_render_config_deny_rule_is_last_table():
    """Regression guard for the ordering bug: every bare key must precede the
    first table header in the rendered output."""
    out = kimi.render_config(SAMPLE_CONFIG, "high")
    lines = [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    first_table = next(i for i, ln in enumerate(lines) if ln.startswith("["))
    bare = [ln for ln in lines[:first_table] if "=" in ln]
    assert any(ln.startswith("default_permission_mode") for ln in bare)
    assert not any("=" in ln and not ln.startswith("[") for ln in lines[first_table:]
                   if ln.startswith("default_permission_mode"))


def test_local_model_for_maps_atlas_ids():
    assert kimi.local_model_for("moonshotai/kimi-k3") == "kimi-code/k3"
    assert kimi.local_model_for("moonshotai/kimi-k2.7-code") == "kimi-code/kimi-for-coding"
    assert kimi.local_model_for("moonshotai/kimi-k2.6") == "kimi-code/kimi-for-coding"
    # Local aliases pass through untouched.
    assert kimi.local_model_for("kimi-code/k3-256k") == "kimi-code/k3-256k"
    # Unknown ids fall back to the default rather than passing a bad alias through,
    # which the CLI rejects with "Model … is not configured in config.toml".
    assert kimi.local_model_for("moonshotai/kimi-k9-nonexistent") == kimi.DEFAULT_KIMI_MODEL


def test_looks_like_kimi_model():
    assert kimi.looks_like_kimi_model("moonshotai/kimi-k2.7-code") is True
    assert kimi.looks_like_kimi_model("kimi-code/k3") is True
    assert kimi.looks_like_kimi_model("kimi-k3") is True
    assert kimi.looks_like_kimi_model("xai/grok-4.6") is False
    assert kimi.looks_like_kimi_model("") is False


def test_parse_stream_json_extracts_assistant_only():
    stdout = "\n".join([
        '{"role":"meta","type":"system.version","version":"0.39.1"}',
        '{"role":"assistant","content":"The answer."}',
        '{"role":"meta","type":"session.resume_hint","content":"To resume: kimi -r x"}',
    ])
    answer, leaked = kimi.parse_stream_json(stdout)
    assert answer == "The answer."
    assert leaked is False


def test_parse_stream_json_flags_tool_execution():
    """A role="tool" event is the CLI's own record that the sandbox failed."""
    tool = '{"role":"assistant","content":"ok"}\n{"role":"tool","content":"CHANGELOG.md"}'
    assert kimi.parse_stream_json(tool)[1] is True


def test_parse_stream_json_tolerates_benign_non_json_noise():
    """A version banner or deprecation notice must NOT be read as a sandbox
    breach — discarding a good answer over CLI chatter is the worse failure."""
    noisy = ("kimi version 0.39.1\n"
             '{"role":"assistant","content":"The answer."}\n'
             "Update available: run `kimi upgrade`\n"
             "[1,2,3]")
    answer, leaked = kimi.parse_stream_json(noisy)
    assert answer == "The answer."
    assert leaked is False


def test_run_discards_answer_when_sandbox_leaks(monkeypatch, tmp_path):
    """A leaked turn is an ERROR, not a usable answer — it may be reporting the
    caller's filesystem rather than the supplied context."""
    monkeypatch.setattr(kimi.shutil, "which", lambda b: "/usr/bin/kimi")
    monkeypatch.setattr(kimi, "build_sandbox", lambda effort: tmp_path)

    async def fake_cli(argv, **kw):
        return CliRun(0, '{"role":"assistant","content":"ans"}\n{"role":"tool","content":"ls"}', "", False)

    monkeypatch.setattr(kimi.cli_gate, "run_cli_async", fake_cli)
    r = _run(kimi.run("q"))
    assert r.status == "error" and r.kind == "sdk_error"
    assert "executed a tool" in r.text


def test_run_passes_sandbox_home_via_env(monkeypatch, tmp_path):
    """KIMI_CODE_HOME must point at the sandbox — without it the CLI uses the
    operator's real home, which is trusted and therefore NOT hermetic."""
    monkeypatch.setattr(kimi.shutil, "which", lambda b: "/usr/bin/kimi")
    monkeypatch.setattr(kimi, "build_sandbox", lambda effort: tmp_path)
    seen = {}

    async def fake_cli(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env")
        return CliRun(0, '{"role":"assistant","content":"ans"}', "", False)

    monkeypatch.setattr(kimi.cli_gate, "run_cli_async", fake_cli)
    r = _run(kimi.run("q", "ctx"))
    assert r.status == "ok" and r.text == "ans"
    assert seen["env"] == {"KIMI_CODE_HOME": str(tmp_path)}
    assert seen["argv"][seen["argv"].index("--output-format") + 1] == "stream-json"
    assert seen["argv"][seen["argv"].index("-m") + 1] == "kimi-code/k3"


def test_run_reports_not_configured_without_a_readable_home(monkeypatch):
    """If the sandbox can't be built we must NOT silently fall back to the real
    home — that would hand an agentic loop the filesystem."""
    monkeypatch.setattr(kimi.shutil, "which", lambda b: "/usr/bin/kimi")
    monkeypatch.setattr(kimi, "build_sandbox", lambda effort: None)
    r = _run(kimi.run("q"))
    assert r.status == "error" and r.kind == "not_configured"


def test_run_binary_missing(monkeypatch):
    monkeypatch.setattr(kimi.shutil, "which", lambda b: None)
    r = _run(kimi.run("q"))
    assert r.status == "error" and r.kind == "binary_missing"


def test_oracle_registration_and_label(monkeypatch):
    assert "kimi" in oracles.KNOWN
    monkeypatch.setattr(oracles.kimi, "available", lambda: True)
    assert oracles.available("kimi") is True
    monkeypatch.setattr(oracles.kimi, "available", lambda: False)
    assert oracles.available("kimi") is False
    assert oracles.label("kimi") == "kimi-code/k3"


def test_oracle_run_routes_to_kimi(monkeypatch):
    async def fake(q, c="", **kw):
        return OracleResult("ok", text="kimi ans", model="kimi-code/k3")

    monkeypatch.setattr(oracles.kimi, "run", fake)
    r = _run(oracles.run("kimi", "q"))
    assert r.key == "kimi" and r.text == "kimi ans"


def test_atlas_kimi_token_prefers_local_cli(monkeypatch):
    """atlas:moonshotai/kimi-* uses the local bridge when the binary is available —
    the operator's subscription instead of per-token Atlas billing."""
    called = {}

    async def fake_kimi(q, c="", **kw):
        called["kw"] = kw
        return OracleResult("ok", text="from local", model="kimi-code/kimi-for-coding")

    async def fake_atlas(*a, **k):
        raise AssertionError("atlas HTTP should not be called when local kimi is available")

    monkeypatch.setattr(oracles.kimi, "available", lambda: True)
    monkeypatch.setattr(oracles.kimi, "run", fake_kimi)
    monkeypatch.setattr(oracles.atlas, "run", fake_atlas)
    r = _run(oracles.run("atlas:moonshotai/kimi-k2.7-code", "q"))
    assert r.status == "ok" and r.text == "from local"
    assert r.key == "atlas:moonshotai/kimi-k2.7-code"  # attribution keeps the token
    assert called["kw"].get("model") == "moonshotai/kimi-k2.7-code"


def test_atlas_kimi_falls_back_to_http_without_local_cli(monkeypatch):
    monkeypatch.setattr(oracles.kimi, "available", lambda: False)
    monkeypatch.setattr(oracles.atlas, "configured", lambda: True)
    seen = {}

    async def fake_atlas(model, q, c="", **kw):
        seen["model"] = model
        return OracleResult("ok", text="from atlas", model=model)

    monkeypatch.setattr(oracles.atlas, "run", fake_atlas)
    r = _run(oracles.run("atlas:moonshotai/kimi-k2.7-code", "q"))
    assert r.status == "ok" and r.text == "from atlas"
    assert seen["model"] == "moonshotai/kimi-k2.7-code"


def test_effort_maps_atlas_presets_to_kimi_vocabulary():
    assert kimi._EFFORT_MAP["quick"] == "low"
    assert kimi._EFFORT_MAP["deep"] == "max"
    # Every mapped value is one the CLI actually supports.
    assert set(kimi._EFFORT_MAP.values()) <= set(kimi._KIMI_EFFORTS)


def test_render_config_handles_a_preexisting_permission_mode_key():
    """The CLI writes `default_permission_mode` itself. Emitting it twice is a
    "Cannot overwrite a value" parse error that kills every kimi call."""
    base = 'default_model = "kimi-code/k3"\ndefault_permission_mode = "yolo"\n\n[models."kimi-code/k3"]\nprovider = "p"\n'
    d = tomllib.loads(kimi.render_config(base, "high"))
    assert d["default_permission_mode"] == "manual"  # ours wins
    assert "kimi-code/k3" in d["models"]


def test_render_config_keeps_same_named_key_inside_a_table():
    """Only the TOP-LEVEL key is stripped — an identically named key inside a
    table is a different key and must survive."""
    base = ('default_model = "kimi-code/k3"\n\n[profiles.x]\n'
            'default_permission_mode = "yolo"\n')
    d = tomllib.loads(kimi.render_config(base, "high"))
    assert d["profiles"]["x"]["default_permission_mode"] == "yolo"
    assert d["default_permission_mode"] == "manual"


def test_render_config_handles_multiline_array_inside_thinking():
    """A bracketed array row is not a table header; treating it as one ended the
    [thinking] skip early and leaked orphaned rows to top level."""
    base = ('default_model = "kimi-code/k3"\n\n[thinking]\n'
            'budgets = [\n  [1, 2],\n  [3, 4],\n]\n\n[models."kimi-code/k3"]\nprovider = "p"\n')
    d = tomllib.loads(kimi.render_config(base, "max"))
    assert d["thinking"] == {"enabled": True, "effort": "max"}  # fully replaced
    assert "kimi-code/k3" in d["models"]  # later tables survived


def test_oversized_prompt_is_refused_before_spawning(monkeypatch):
    """The CLI takes the prompt as one argv value; the kernel caps that at
    ~131k, so Popen would raise OSError(E2BIG) straight past the bridge."""
    monkeypatch.setattr(kimi.shutil, "which", lambda b: "/usr/bin/kimi")

    async def boom(*a, **k):
        raise AssertionError("must not spawn an oversized argv")

    monkeypatch.setattr(kimi.cli_gate, "run_cli_async", boom)
    r = _run(kimi.run("q", "x" * (kimi.MAX_PROMPT_BYTES + 1)))
    assert r.status == "error" and r.kind == "context_too_large"
    assert "ask_atlas" in r.text  # points at the route that has no argv limit


def test_local_alias_for_refuses_unmapped_ids():
    """None means 'do not serve locally' — substituting the default would answer
    as a model the caller never asked for, under the requested id's name."""
    assert kimi.local_alias_for("moonshotai/kimi-linear-48b") is None
    assert kimi.local_alias_for("moonshotai/kimi-k3") == "kimi-code/k3"
    assert kimi.local_alias_for("") is None


def test_unmapped_atlas_kimi_id_stays_on_http(monkeypatch):
    monkeypatch.setattr(oracles.kimi, "available", lambda: True)
    monkeypatch.setattr(oracles.atlas, "configured", lambda: True)
    seen = {}

    async def fake_kimi(*a, **k):
        raise AssertionError("unmappable id must not be served by the local CLI")

    async def fake_atlas(model, q, c="", **kw):
        seen["model"] = model
        return OracleResult("ok", text="from atlas", model=model)

    monkeypatch.setattr(oracles.kimi, "run", fake_kimi)
    monkeypatch.setattr(oracles.atlas, "run", fake_atlas)
    r = _run(oracles.run("atlas:moonshotai/kimi-linear-48b", "q"))
    assert r.status == "ok" and seen["model"] == "moonshotai/kimi-linear-48b"


def test_kimi_effort_env_accepts_the_shared_vocabulary(monkeypatch):
    """ASK_FABLE_KIMI_EFFORT must map like ASK_FABLE_EFFORT, not land on the
    opposite end of the scale."""
    monkeypatch.delenv("ASK_FABLE_EFFORT", raising=False)
    monkeypatch.setenv("ASK_FABLE_KIMI_EFFORT", "quick")
    assert kimi.kimi_effort() == "low"
    monkeypatch.setenv("ASK_FABLE_KIMI_EFFORT", "deep")
    assert kimi.kimi_effort() == "max"
    monkeypatch.setenv("ASK_FABLE_KIMI_EFFORT", "max")
    assert kimi.kimi_effort() == "max"


def test_build_sandbox_is_secure_and_repairs_stale_symlinks(tmp_path, monkeypatch):
    src = tmp_path / "real"
    (src / "oauth").mkdir(parents=True)
    (src / "config.toml").write_text('default_model = "kimi-code/k3"\n')
    monkeypatch.setenv("ASK_FABLE_KIMI_HOME", str(src))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    home = kimi.build_sandbox("high")
    assert home is not None
    # 0700/0600 — it copies the operator's provider blocks and links their creds.
    assert (home.stat().st_mode & 0o777) == 0o700
    assert ((home / "config.toml").stat().st_mode & 0o777) == 0o600
    assert (home / "oauth").readlink() == src / "oauth"
    # workspace-trust is the guard: it must never be linked in.
    assert not (home / "workspace-trust").exists()

    # Point at a NEW home: the stale link must be repaired, not skipped forever.
    src2 = tmp_path / "real2"
    (src2 / "oauth").mkdir(parents=True)
    (src2 / "config.toml").write_text('default_model = "kimi-code/k3"\n')
    monkeypatch.setenv("ASK_FABLE_KIMI_HOME", str(src2))
    home = kimi.build_sandbox("high")
    assert (home / "oauth").readlink() == src2 / "oauth"


def test_build_sandbox_skips_rewriting_unchanged_files(tmp_path, monkeypatch):
    """It runs on the event loop for every turn; the output is identical per effort."""
    src = tmp_path / "real"
    src.mkdir()
    (src / "config.toml").write_text('default_model = "kimi-code/k3"\n')
    monkeypatch.setenv("ASK_FABLE_KIMI_HOME", str(src))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    home = kimi.build_sandbox("high")
    before = (home / "config.toml").stat().st_mtime_ns
    writes = []
    monkeypatch.setattr(kimi._paths, "write_secure",
                        lambda p, c, **k: writes.append(p) or True)
    kimi.build_sandbox("high")
    assert writes == []  # nothing rewritten
    assert (home / "config.toml").stat().st_mtime_ns == before
