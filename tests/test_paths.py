"""Tests for the shared _paths primitives: XDG fallback, atomic write, secure mode."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from ask_fable import _paths


def test_xdg_state_dir_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "custom-state"))
    assert _paths.xdg_state_dir() == tmp_path / "custom-state"


def test_xdg_state_dir_falls_back(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = Path(str(tmp_path)) / ".local" / "state"
    assert _paths.xdg_state_dir() == expected


def test_xdg_config_dir_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "custom-config"))
    assert _paths.xdg_config_dir() == tmp_path / "custom-config"


def test_ensure_dir_secure_creates_dir_with_mode(monkeypatch, tmp_path):
    d = tmp_path / "newdir"
    assert _paths.ensure_dir_secure(d) is True
    assert d.is_dir()
    if sys.platform != "win32":
        mode = d.stat().st_mode & 0o777
        assert mode == 0o700


def test_ensure_dir_secure_existing_dir_is_idempotent(monkeypatch, tmp_path):
    d = tmp_path / "newdir"
    d.mkdir()
    assert _paths.ensure_dir_secure(d) is True
    assert d.is_dir()


def test_write_secure_creates_file_with_mode(tmp_path):
    p = tmp_path / "x" / "y" / "file.txt"
    assert _paths.write_secure(p, "hello") is True
    assert p.read_text() == "hello"
    if sys.platform != "win32":
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600


def test_write_secure_accepts_bytes(tmp_path):
    p = tmp_path / "file.bin"
    assert _paths.write_secure(p, b"\x00\x01\x02") is True
    assert p.read_bytes() == b"\x00\x01\x02"


def test_write_secure_atomic_under_simulated_crash(tmp_path, monkeypatch):
    """If the actual write fails partway, the target file is untouched (atomic
    rename means readers see the OLD content, never a partial write)."""
    p = tmp_path / "victim"
    p.write_text("OLD")
    # Force the rename itself to fail.
    import ask_fable._paths as paths_mod

    def boom(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(paths_mod.os, "replace", boom)
    result = _paths.write_secure(p, "NEW")
    assert result is False
    # Target file unchanged.
    assert p.read_text() == "OLD"


def test_write_secure_overwrites_existing_file_atomically(tmp_path):
    p = tmp_path / "victim"
    p.write_text("v1")
    assert _paths.write_secure(p, "v2") is True
    assert p.read_text() == "v2"
    # No leftover .tmp files in the parent.
    leftover = list(p.parent.glob(".victim.*.tmp"))
    assert leftover == []


def test_write_secure_cleans_up_temp_on_failure(tmp_path, monkeypatch):
    """If the rename fails, the orphan .tmp file is removed."""
    p = tmp_path / "victim"
    import ask_fable._paths as paths_mod

    def boom(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(paths_mod.os, "replace", boom)
    assert _paths.write_secure(p, "anything") is False
    assert not p.exists()
    # No orphan .tmp files left behind.
    leftover = list(p.parent.glob(".victim.*.tmp"))
    assert leftover == []


def test_open_append_secure_creates_file_at_0o600(tmp_path):
    p = tmp_path / "sub" / "log.jsonl"
    fh = _paths.open_append_secure(p)
    assert fh is not None
    try:
        fh.write("hello\n")
    finally:
        fh.close()
    assert p.read_text() == "hello\n"
    if sys.platform != "win32":
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600


def test_open_append_secure_appends_to_existing(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text("line1\n")
    fh = _paths.open_append_secure(p)
    try:
        fh.write("line2\n")
    finally:
        fh.close()
    assert p.read_text() == "line1\nline2\n"


def test_secure_sqlite_sidecars_chmods_db_and_sidecars(tmp_path):
    db = tmp_path / "x.db"
    files = [db] + [tmp_path / f"x.db{s}" for s in ("-wal", "-shm", "-journal")]
    for f in files:
        f.write_text("x")
        if sys.platform != "win32":
            f.chmod(0o644)
    _paths.secure_sqlite_sidecars(db)
    if sys.platform != "win32":
        for f in files:
            assert (f.stat().st_mode & 0o777) == 0o600


def test_secure_sqlite_sidecars_ignores_missing(tmp_path):
    db = tmp_path / "x.db"
    db.write_text("x")
    _paths.secure_sqlite_sidecars(db)  # no -wal/-shm present — must not raise


_ANY_MD = re.compile(r"\d+\.md")


def test_prune_dir_keeps_newest(tmp_path):
    for i in range(5):
        (tmp_path / f"{i}.md").write_text(str(i))
        os.utime(tmp_path / f"{i}.md", (i, i))
    deleted = _paths.prune_dir(tmp_path, keep=3, pattern=_ANY_MD)
    assert deleted == 2
    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["2.md", "3.md", "4.md"]


def test_prune_dir_off_by_default_and_zero_cap(tmp_path):
    (tmp_path / "a.md").write_text("x")
    assert _paths.prune_dir(tmp_path, keep=0, pattern=_ANY_MD) == 0
    assert (tmp_path / "a.md").exists()


def test_prune_dir_never_deletes_foreign_files(tmp_path):
    """Files not matching the caller's own filename shape are neither counted
    against the cap nor deleted — a user's notes in a shared/pointed dir
    must survive retention."""
    for i in range(4):
        (tmp_path / f"{i}.md").write_text(str(i))
        os.utime(tmp_path / f"{i}.md", (i, i))
    (tmp_path / "my-notes.md").write_text("precious")
    os.utime(tmp_path / "my-notes.md", (0, 0))  # oldest file in the dir
    deleted = _paths.prune_dir(tmp_path, keep=2, pattern=_ANY_MD)
    assert deleted == 2
    assert (tmp_path / "my-notes.md").exists()
    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["2.md", "3.md", "my-notes.md"]


def test_prune_dir_skips_file_vanishing_mid_scan(tmp_path, monkeypatch):
    """A file unlinked between scandir and stat (a concurrent pruner) is
    skipped; the rest of the round still prunes. Regression: a single
    FileNotFoundError used to abort the whole round with 0 deleted."""
    import ask_fable._paths as paths_mod

    for i in range(5):
        (tmp_path / f"{i}.md").write_text(str(i))
        os.utime(tmp_path / f"{i}.md", (i, i))

    real_scandir = os.scandir

    class _VanishingEntry:
        def __init__(self, entry):
            self._e = entry

        @property
        def name(self):
            return self._e.name

        @property
        def path(self):
            return self._e.path

        def is_file(self, **kw):
            return self._e.is_file(**kw)

        def stat(self, **kw):
            if self._e.name == "0.md":
                raise FileNotFoundError(self._e.path)
            return self._e.stat(**kw)

    class _Scan:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            self._it = self._inner.__enter__()
            return self

        def __exit__(self, *a):
            return self._inner.__exit__(*a)

        def __iter__(self):
            return (_VanishingEntry(e) for e in self._it)

    monkeypatch.setattr(paths_mod.os, "scandir", lambda d: _Scan(real_scandir(d)))
    deleted = _paths.prune_dir(tmp_path, keep=2, pattern=_ANY_MD)
    # 0.md is skipped (vanished); of the 4 statted files the oldest 2 go.
    assert deleted == 2
    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["0.md", "3.md", "4.md"]
