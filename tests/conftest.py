"""Shared test fixtures for ask_fable."""

import pytest


@pytest.fixture(autouse=True)
def _no_answer_save(monkeypatch):
    """Keep the suite hermetic: don't write answer files to the real state dir.

    With saving off, ``outputs.save`` returns None and handlers omit the
    ``saved`` key, so payload-equality assertions stay stable. Tests that
    exercise saving opt back in explicitly.
    """
    monkeypatch.setenv("ASK_FABLE_SAVE", "0")


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path):
    """Point the tool-writable config file at a per-test temp path so the suite
    never reads or writes the real ~/.config/ask_fable/config.json."""
    monkeypatch.setenv("ASK_FABLE_CONFIG_FILE", str(tmp_path / "config.json"))


@pytest.fixture(autouse=True)
def _no_provider_keys(monkeypatch):
    """Strip real provider API keys so availability-dependent behavior (e.g. the
    default council growing deepseek via ``oracles.default_models()``) is
    deterministic regardless of the developer's environment. Tests that need a
    key set it explicitly."""
    monkeypatch.delenv("ASK_FABLE_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ASK_FABLE_GLM_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _no_retention_caps(monkeypatch):
    """Strip operator retention caps so save/dump paths never prune mid-suite.

    With ``ASK_FABLE_MAX_SESSIONS`` exported (e.g. ``1``), a session dump would
    prune an earlier test-written transcript and multi-dump assertions flake
    depending on the developer's environment. Tests that exercise retention set
    the knobs explicitly."""
    monkeypatch.delenv("ASK_FABLE_MAX_ANSWERS", raising=False)
    monkeypatch.delenv("ASK_FABLE_MAX_SESSIONS", raising=False)


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch, tmp_path):
    """Disable the answer cache by default so handler payload-equality assertions
    stay stable and no cache.db is written to the real state dir. Cache-specific
    tests opt back in and point ASK_FABLE_CACHE_PATH at a temp file."""
    monkeypatch.setenv("ASK_FABLE_CACHE", "0")
    monkeypatch.setenv("ASK_FABLE_CACHE_PATH", str(tmp_path / "cache.db"))


@pytest.fixture(autouse=True)
def _isolate_hub(monkeypatch, tmp_path):
    """Point the cross-instance hub at a per-test temp db and leave it enabled.

    Multi-oracle success paths (council/chain/debate) mirror into the hub; without
    isolation the suite would write into the operator's real hub.db. Hub tests that
    need a known path set ``ASK_FABLE_HUB_PATH`` themselves (still under tmp_path).
    Tests that need the hub off set ``ASK_FABLE_HUB=0``."""
    monkeypatch.setenv("ASK_FABLE_HUB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.delenv("ASK_FABLE_HUB", raising=False)


@pytest.fixture(autouse=True)
def _isolate_audit_and_traces(monkeypatch, tmp_path):
    """Keep schema-v2 events and full-mode bundles out of the operator state dir.

    Without this, every suite run appends to ``~/.local/state/ask_fable/decisions.jsonl``
    and can write ``traces/`` under the real project fingerprint — polluting
    ``stats`` / ``trace_list`` with fixture noise (e.g. 6ms ``{"status":"ok"}``
    councils). Tests that need a specific path set the env vars themselves
    (still under ``tmp_path``)."""
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "decisions.jsonl"))
    monkeypatch.setenv("ASK_FABLE_TRACE_DIR", str(tmp_path / "traces"))


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    """Clear the process-local oracle circuit breaker between tests.

    Handler tests record real error outcomes (timeouts, binary_missing) into the
    shared breaker; without a reset, later tests in the same process can see
    ``circuit_open`` and skip stubbed bridges — flaking councils/chains that
    expect both oracles to answer."""
    from ask_fable import health

    health.breaker._states.clear()
    yield
    health.breaker._states.clear()


class FakePopen:
    """Shared stand-in for subprocess.Popen used by every CLI-bridge test.

    Records spawn argv/kwargs and stdin, replays a fixed communicate result;
    ``timeout_first`` makes the first ``communicate`` raise TimeoutExpired (the
    second, post-kill drain, returns). One copy here instead of five per-file
    variants, so the spawn contract every bridge now shares via
    ``cli_gate.run_cli`` is asserted identically everywhere.
    """

    instances: list["FakePopen"] = []

    def __init__(self, argv=None, *, returncode=0, stdout="", stderr="", timeout_first=False):
        import subprocess as _subprocess

        self._subprocess = _subprocess
        self.argv = argv
        self.kw: dict = {}
        self.pid = 4242
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._timeout_first = timeout_first
        self._calls = 0
        self.killed = False
        self.seen_input = None
        self.stdin_data = None
        FakePopen.instances.append(self)

    @classmethod
    def factory(cls, *, returncode=0, stdout="", stderr="", timeout_first=False):
        def make(argv, **kw):
            proc = cls(
                argv,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                timeout_first=timeout_first,
            )
            proc.kw = kw
            return proc

        return make

    def communicate(self, input=None, timeout=None):
        self._calls += 1
        if input is not None:
            self.seen_input = input
            self.stdin_data = input
        if self._timeout_first and self._calls == 1:
            raise self._subprocess.TimeoutExpired(cmd="cli", timeout=timeout)
        self.returncode = 0 if self.returncode is None else self.returncode
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True
        self.returncode = -9
