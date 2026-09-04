"""safe_fs — the filesystem safety kernel for context packing.

Adversarial vectors are written FIRST (before any tool can reach the filesystem):
symlink/absolute/`..` escape, the symlinked-root regression, the blocklist
bypasses, the range grammar, and the read-path caps. Every rejection must be a
typed Reason, never an exception.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ask_fable import safe_fs
from ask_fable.safe_fs import Reason, Resolved


@pytest.fixture
def root(tmp_path):
    # realpath so assertions compare against the resolved form on symlinked tmp
    # (macOS /tmp -> /private/tmp); on Linux this is a no-op.
    d = tmp_path / "repo"
    d.mkdir()
    (d / "a.txt").write_text("line1\nline2\nline3\nline4\nline5\n")
    return os.path.realpath(str(d))


# --- containment / escape -----------------------------------------------------

def test_plain_file_resolves(root):
    r = safe_fs.resolve_within(root, "a.txt")
    assert isinstance(r, Resolved)
    assert r.rel_path == "a.txt"
    assert (r.start, r.end) == (None, None)


def test_absolute_spec_escapes(root):
    # os.path.join(root, "/etc/passwd") discards root -> containment reject.
    assert safe_fs.resolve_within(root, "/etc/passwd") is Reason.ESCAPE


def test_dotdot_escape(root):
    assert safe_fs.resolve_within(root, "../../etc/passwd") is Reason.ESCAPE
    assert safe_fs.resolve_within(root, "sub/../../outside.txt") is Reason.ESCAPE


def test_symlink_pointing_outside_root_escapes(root, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("SECRET")
    os.symlink(str(secret), os.path.join(root, "link.txt"))
    # realpath resolves the symlink to a path outside root -> escape, never read.
    assert safe_fs.resolve_within(root, "link.txt") is Reason.ESCAPE


def test_symlinked_root_resolves_legit_files(tmp_path):
    # Regression guard for the near-fatal: a symlinked ROOT must still contain its
    # own files. Without realpath(root) this rejected every legitimate file.
    real = tmp_path / "real_repo"
    real.mkdir()
    (real / "x.txt").write_text("hello\n")
    link = tmp_path / "linked_repo"
    os.symlink(str(real), str(link))
    r = safe_fs.resolve_within(str(link), "x.txt")
    assert isinstance(r, Resolved)
    assert r.abs_path == os.path.realpath(str(real / "x.txt"))


# --- blocklist ----------------------------------------------------------------

def test_blocklist_env_and_git(root):
    (root_p := Path(root))
    (root_p / ".env").write_text("SECRET=1")
    (root_p / ".env.local").write_text("SECRET=2")
    gitdir = root_p / ".git"
    gitdir.mkdir()
    (gitdir / "config").write_text("[core]")
    sub = root_p / "svc"
    sub.mkdir()
    (sub / ".env").write_text("SECRET=3")
    assert safe_fs.resolve_within(root, ".env") is Reason.BLOCKED
    assert safe_fs.resolve_within(root, ".env.local") is Reason.BLOCKED
    assert safe_fs.resolve_within(root, ".git/config") is Reason.BLOCKED
    assert safe_fs.resolve_within(root, "svc/.env") is Reason.BLOCKED  # subdir .env


def test_blocklist_does_not_false_positive(root):
    p = Path(root)
    (p / ".gitignore").write_text("*.pyc")
    gh = p / ".github"
    gh.mkdir()
    (gh / "ci.yml").write_text("on: push")
    # component-anchored, not substring: .gitignore and .github/ are allowed.
    assert isinstance(safe_fs.resolve_within(root, ".gitignore"), Resolved)
    assert isinstance(safe_fs.resolve_within(root, ".github/ci.yml"), Resolved)


def test_blocklist_via_symlink_to_env(root):
    # notes.txt -> .env: realpath lands on .env, blocklist runs on the resolved
    # basename, so the disguised secret is still blocked.
    p = Path(root)
    (p / ".env").write_text("SECRET=1")
    os.symlink(str(p / ".env"), str(p / "notes.txt"))
    assert safe_fs.resolve_within(root, "notes.txt") is Reason.BLOCKED


def test_extra_blocklist_from_config(root):
    p = Path(root)
    (p / "secrets.yaml").write_text("k: v")
    assert isinstance(safe_fs.resolve_within(root, "secrets.yaml"), Resolved)
    assert safe_fs.resolve_within(root, "secrets.yaml", extra_blocklist=("secrets.yaml",)) is Reason.BLOCKED


# --- range grammar ------------------------------------------------------------

def test_range_grammar(root):
    r = safe_fs.resolve_within(root, "a.txt:2-4")
    assert isinstance(r, Resolved) and (r.start, r.end) == (2, 4)


@pytest.mark.parametrize("spec", ["a.txt:10", "a.txt:10-", "a.txt:-5", "a.txt:40-10", "a.txt:0-5"])
def test_bad_ranges_rejected(root, spec):
    assert safe_fs.resolve_within(root, spec) is Reason.BAD_RANGE


def test_colon_filename_is_not_a_range(tmp_path):
    p = tmp_path / "repo"
    p.mkdir()
    (p / "weird:name.txt").write_text("x\n")
    root = os.path.realpath(str(p))
    r = safe_fs.resolve_within(root, "weird:name.txt")
    assert isinstance(r, Resolved) and (r.start, r.end) == (None, None)


# --- not_file -----------------------------------------------------------------

def test_directory_is_not_file(root):
    Path(root, "sub").mkdir()
    assert safe_fs.resolve_within(root, "sub") is Reason.NOT_FILE


def test_missing_file_is_not_file(root):
    assert safe_fs.resolve_within(root, "nope.txt") is Reason.NOT_FILE


# --- read_content -------------------------------------------------------------

def test_read_whole_file(root):
    r = safe_fs.resolve_within(root, "a.txt")
    out = safe_fs.read_content(r, max_bytes=1_000_000)
    assert out == ("line1\nline2\nline3\nline4\nline5\n", 30)


def test_read_range_inclusive_1indexed(root):
    r = safe_fs.resolve_within(root, "a.txt:2-4")
    text, _ = safe_fs.read_content(r, max_bytes=1_000_000)
    assert text == "line2\nline3\nline4\n"


def test_read_range_clamps_end_to_eof(root):
    r = safe_fs.resolve_within(root, "a.txt:4-999")
    text, _ = safe_fs.read_content(r, max_bytes=1_000_000)
    assert text == "line4\nline5\n"


def test_read_range_start_past_eof_is_bad_range(root):
    r = safe_fs.resolve_within(root, "a.txt:99-100")
    assert safe_fs.read_content(r, max_bytes=1_000_000) is Reason.BAD_RANGE


def test_whole_file_over_cap_is_too_large(root):
    Path(root, "big.txt").write_bytes(b"x\n" * 10_000)
    r = safe_fs.resolve_within(root, "big.txt")
    assert safe_fs.read_content(r, max_bytes=100) is Reason.TOO_LARGE


def test_range_on_huge_file_still_reads(root):
    # A small top-of-file slice succeeds even though the whole file blows the cap.
    Path(root, "big.log").write_bytes(b"L\n" * 100_000)
    r = safe_fs.resolve_within(root, "big.log:1-5")
    out = safe_fs.read_content(r, max_bytes=100)
    assert out == ("L\nL\nL\nL\nL\n", 10)


def test_binary_file_is_binary(root):
    Path(root, "bin.dat").write_bytes(b"abc\x00def")
    r = safe_fs.resolve_within(root, "bin.dat")
    assert safe_fs.read_content(r, max_bytes=1_000_000) is Reason.BINARY


def test_read_missing_file_unreadable(root):
    # Resolve a real file, then delete it before reading -> UNREADABLE, no raise.
    r = safe_fs.resolve_within(root, "a.txt")
    os.unlink(r.abs_path)
    assert safe_fs.read_content(r, max_bytes=1_000_000) is Reason.UNREADABLE


# --- root resolution ----------------------------------------------------------

def test_resolve_root_from_env(monkeypatch, tmp_path, root):
    monkeypatch.delenv("ASK_FABLE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("ASK_FABLE_PROJECT_ROOT", root)
    # config precedence: ensure no config file shadows the env var
    monkeypatch.setenv("ASK_FABLE_CONFIG_FILE", str(tmp_path / "nonexistent.json"))
    assert safe_fs.resolve_root() == os.path.realpath(root)


def test_resolve_root_unset_is_none(monkeypatch, tmp_path):
    monkeypatch.delenv("ASK_FABLE_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("ASK_FABLE_CONFIG_FILE", str(tmp_path / "nonexistent.json"))
    assert safe_fs.resolve_root() is None


def test_resolve_root_nondir_is_none(monkeypatch, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    monkeypatch.setenv("ASK_FABLE_CONFIG_FILE", str(tmp_path / "nonexistent.json"))
    monkeypatch.setenv("ASK_FABLE_PROJECT_ROOT", str(f))
    assert safe_fs.resolve_root() is None
