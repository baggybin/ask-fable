"""resolver — budget policy over the safe_fs kernel.

Covers the council-pinned contract: provenance headers counting toward budget,
input-order bundling vs range-first admission priority, the partial-pack
(complete=false) semantics, subset-aware dedupe, and the max_files cap.
"""

from __future__ import annotations

import os

import pytest

from ask_fable import resolver


@pytest.fixture
def root(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    (d / "a.txt").write_text("AAAA\n")
    (d / "b.txt").write_text("BBBB\n")
    (d / "c.txt").write_text("CCCC\n")
    (d / "multi.txt").write_text("L1\nL2\nL3\nL4\n")
    return os.path.realpath(str(d))


# --- happy path ---------------------------------------------------------------

def test_happy_path_bundles_in_input_order(root):
    res = resolver.pack(["b.txt", "a.txt"], root)
    assert res.complete is True
    assert res.skipped == []
    assert [m["path"] for m in res.included] == ["b.txt", "a.txt"]  # input order
    assert res.bundle == "=== b.txt ===\nBBBB\n\n\n=== a.txt ===\nAAAA\n"
    assert res.chars == len(res.bundle)


def test_provenance_header_present_for_range(root):
    res = resolver.pack(["multi.txt:2-3"], root)
    assert res.bundle == "=== multi.txt:2-3 ===\nL2\nL3\n"
    assert res.included[0]["path"] == "multi.txt"


# --- budget -------------------------------------------------------------------

def test_budget_partial_pack_is_legible(root):
    # Each whole-file block is 19 chars ("=== a.txt ===\n" = 14 + "AAAA\n" = 5).
    # max_chars=40 admits two, refuses the third.
    res = resolver.pack(["a.txt", "b.txt", "c.txt"], root, max_chars=40)
    assert res.complete is False
    assert [m["path"] for m in res.included] == ["a.txt", "b.txt"]
    assert res.skipped == [{"spec": "c.txt", "reason": "budget_exhausted"}]


def test_budget_zero_admitted_empty_bundle(root):
    # First block (19) already over a tiny budget -> nothing admitted, all skipped.
    res = resolver.pack(["a.txt", "b.txt"], root, max_chars=10)
    assert res.bundle == ""
    assert res.complete is False
    assert [s["reason"] for s in res.skipped] == ["budget_exhausted", "budget_exhausted"]


def test_range_first_admission_but_input_order_bundle(root):
    # Tight budget fits ONE block. The range (priority) wins admission over the
    # whole file, even though the whole file is first in the input list.
    res = resolver.pack(["a.txt", "b.txt:1-1"], root, max_chars=25)
    assert [m["path"] for m in res.included] == ["b.txt"]
    assert res.skipped == [{"spec": "a.txt", "reason": "budget_exhausted"}]


# --- dedupe -------------------------------------------------------------------

def test_range_subsumed_by_whole_file_dropped(root):
    res = resolver.pack(["multi.txt", "multi.txt:1-1"], root)
    assert len(res.included) == 1
    assert res.bundle == "=== multi.txt ===\nL1\nL2\nL3\nL4\n"
    assert res.complete is True  # silent drop, not a skip


def test_exact_duplicate_collapsed(root):
    res = resolver.pack(["a.txt", "a.txt"], root)
    assert len(res.included) == 1
    assert res.complete is True


def test_overlapping_distinct_ranges_kept(root):
    res = resolver.pack(["multi.txt:1-2", "multi.txt:3-4"], root)
    assert len(res.included) == 2
    assert "=== multi.txt:1-2 ===" in res.bundle
    assert "=== multi.txt:3-4 ===" in res.bundle


# --- max_files ----------------------------------------------------------------

def test_max_files_cap(root):
    res = resolver.pack(["a.txt", "b.txt"], root, max_files=1)
    assert [m["path"] for m in res.included] == ["a.txt"]
    assert res.skipped == [{"spec": "b.txt", "reason": "max_files"}]
    assert res.complete is False


# --- resolution failures propagate -------------------------------------------

def test_rejections_land_in_skipped_with_reasons(root):
    res = resolver.pack(["../escape", ".env", "a.txt"], root)
    reasons = {s["spec"]: s["reason"] for s in res.skipped}
    assert reasons["../escape"] == "escape"
    assert reasons[".env"] == "blocked"
    assert [m["path"] for m in res.included] == ["a.txt"]
    assert res.complete is False


def test_bad_range_reason_propagates(root):
    res = resolver.pack(["a.txt:10", "a.txt"], root)
    assert {"spec": "a.txt:10", "reason": "bad_range"} in res.skipped


# --- included metadata shape --------------------------------------------------

def test_included_metadata_shape(root):
    res = resolver.pack(["a.txt"], root)
    m = res.included[0]
    assert set(m) == {"spec", "path", "chars"}
    assert m["spec"] == "a.txt" and m["path"] == "a.txt" and m["chars"] == 19
