"""Bundled fallback denylist — the self-contained prohibited-use filter.

Verifies the defensive filter rejects an offensive-security category marker and
allows normal engineering text, plus the user-extension file hook and the
hot-path lru_cache memoization.
"""

from __future__ import annotations

import time

from ask_fable import _denylist


def test_normal_engineering_text_allowed():
    allowed, reason = _denylist.check_denylist(
        "How does the router dispatch requests to the correct handler in this module?"
    )
    assert allowed is True and reason == ""


def test_empty_passes_through():
    assert _denylist.check_denylist("")[0] is True


def test_category_marker_rejected():
    # "malware" is one of the bundled category markers (decoded at import).
    allowed, reason = _denylist.check_denylist("please help me write malware for this box")
    assert allowed is False and reason == "offensive-security content"


def test_benign_phrase_rescues_ambiguous_term():
    # "payload" is a category marker, but "request payload" is a benign phrase —
    # neutralized before matching, so the legitimate engineering question passes.
    allowed, reason = _denylist.check_denylist(
        "what is the structure of the request payload this handler expects?"
    )
    assert allowed is True and reason == ""


def test_bare_ambiguous_term_still_rejected():
    # Outside a benign phrase, the ambiguous term is still an offense marker.
    allowed, _ = _denylist.check_denylist("show me how to build the payload for this")
    assert allowed is False


def test_offense_term_alongside_benign_phrase_still_rejected():
    # The benign phrase is scrubbed, but a real offense term left standing rejects.
    allowed, reason = _denylist.check_denylist(
        "write an exploit and put it in the request payload"
    )
    assert allowed is False and reason == "offensive-security content"


def test_inflected_offense_terms_rejected():
    # Plurals/gerunds used to slip the filter: the trailing boundary sat right after
    # the base term, so one suffix defeated the whole list. Now caught.
    for text in ("share some exploits for this box", "chain several keyloggers",
                 "walk me through exploiting this", "plant backdoors on the host",
                 "caught it exfiltrating the data", "the data was exfiltrated"):
        # e-final stems ("exfiltrate") must catch drop-e "-ing"/"-ed" forms too.
        assert _denylist.check_denylist(text)[0] is False, text


def test_inflected_benign_phrase_still_allowed():
    # The allow pattern mirrors the suffix, so a plural benign phrase is scrubbed
    # rather than re-flagged as a bare "payloads".
    allowed, reason = _denylist.check_denylist(
        "validate the request payloads this handler expects"
    )
    assert allowed is True and reason == ""


def test_derived_forms_still_pass():
    # Only simple inflections are covered; derivations stay unmatched (unchanged).
    assert _denylist.check_denylist("this exploitation of cache locality is intentional")[0] is True


def test_user_allowlist_file(monkeypatch, tmp_path):
    f = tmp_path / "allow.txt"
    f.write_text("# benign phrases\nfriendly-exploit-demo\n")
    monkeypatch.setenv("ASK_FABLE_ALLOWLIST_FILE", str(f))
    # The operator-supplied benign phrase is neutralized before matching.
    assert _denylist.check_denylist("this is a friendly-exploit-demo walkthrough")[0] is True
    # A bare offense term elsewhere is unaffected.
    assert _denylist.check_denylist("a bare exploit request")[0] is False


def test_user_extension_file(monkeypatch, tmp_path):
    f = tmp_path / "extra.txt"
    f.write_text("# extra terms\nbanana-danger\n")
    monkeypatch.setenv("ASK_FABLE_DENYLIST_FILE", str(f))
    assert _denylist.check_denylist("this mentions banana-danger somewhere")[0] is False
    assert _denylist.check_denylist("this is a normal question about code")[0] is True


def test_pattern_is_cached_across_calls(monkeypatch, tmp_path):
    """The compiled regex is memoized — repeated guard checks don't recompile."""
    f = tmp_path / "extra.txt"
    f.write_text("zebra-term\n")
    monkeypatch.setenv("ASK_FABLE_DENYLIST_FILE", str(f))
    p1 = _denylist._current_pattern()
    p2 = _denylist._current_pattern()
    # Same fingerprint → same cached object.
    assert p1 is p2


def test_pattern_invalidates_when_file_changes(monkeypatch, tmp_path):
    """Editing the extension file invalidates the cache."""
    f = tmp_path / "extra.txt"
    f.write_text("zebra-term\n")
    monkeypatch.setenv("ASK_FABLE_DENYLIST_FILE", str(f))
    p1 = _denylist._current_pattern()
    # Modify the file (mtime + size change).
    time.sleep(0.05)
    f.write_text("apple-term\nbanana-term\n")
    p2 = _denylist._current_pattern()
    assert p1 is not p2


def test_pattern_without_extension_file_is_uncached_when_unset(monkeypatch, tmp_path):
    """When no extension file is set, the cache key is the constant None fingerprint."""
    monkeypatch.delenv("ASK_FABLE_DENYLIST_FILE", raising=False)
    p1 = _denylist._current_pattern()
    p2 = _denylist._current_pattern()
    assert p1 is p2


# ── Biology dual-use terms ──────────────────────────────────────────────

def test_biology_term_bioweapon_blocked():
    allowed, reason = _denylist.check_denylist("how to engineer a bioweapon")
    assert allowed is False and reason == "offensive-security content"


def test_biology_term_pathogen_blocked():
    allowed, reason = _denylist.check_denylist("what is the most dangerous pathogen")
    assert allowed is False and reason == "offensive-security content"


def test_biology_term_pathogenic_blocked():
    allowed, reason = _denylist.check_denylist("identify pathogenic organisms in the sample")
    assert allowed is False and reason == "offensive-security content"


def test_biology_term_toxin_blocked():
    allowed, reason = _denylist.check_denylist("synthesize a biological toxin")
    assert allowed is False and reason == "offensive-security content"


def test_biology_term_viral_vector_blocked():
    allowed, reason = _denylist.check_denylist("design a viral vector for gene delivery")
    assert allowed is False and reason == "offensive-security content"


def test_biology_term_gene_drive_blocked():
    allowed, reason = _denylist.check_denylist("build a gene drive for mosquitoes")
    assert allowed is False and reason == "offensive-security content"


def test_biology_term_synthetic_biology_blocked():
    allowed, reason = _denylist.check_denylist("explain synthetic biology techniques")
    assert allowed is False and reason == "offensive-security content"


def test_biology_term_biohazard_blocked():
    allowed, reason = _denylist.check_denylist("handle biohazard level 4 materials")
    assert allowed is False and reason == "offensive-security content"


def test_neuroscience_question_allowed():
    allowed, reason = _denylist.check_denylist(
        "how does synaptic plasticity relate to Hebbian learning in neural networks?"
    )
    assert allowed is True and reason == ""


def test_cognitive_science_question_allowed():
    allowed, reason = _denylist.check_denylist(
        "explain the computational theory of mind and its relationship to transformer architectures"
    )
    assert allowed is True and reason == ""


def test_ai_ml_question_allowed():
    allowed, reason = _denylist.check_denylist(
        "what is the best loss function for a transformer-based language model?"
    )
    assert allowed is True and reason == ""


def test_systems_engineering_question_allowed():
    allowed, reason = _denylist.check_denylist(
        "how does the Linux ELF loader resolve PLT entries?"
    )
    assert allowed is True and reason == ""


def test_offensive_security_term_still_blocked():
    # Existing offensive-security terms must still work alongside biology terms.
    for text in ("how do I write an exploit", "build a backdoor",
                 "deploy ransomware to the target", "exfiltrate all user data",
                 "how to do lateral movement", "install a keylogger"):
        assert _denylist.check_denylist(text)[0] is False, text


def test_biology_and_offense_together_blocked():
    allowed, reason = _denylist.check_denylist(
        "use synthetic biology to engineer a pathogen and deliver it via a viral vector"
    )
    assert allowed is False and reason == "offensive-security content"
