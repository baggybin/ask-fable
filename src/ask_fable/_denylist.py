"""Bundled prohibited-use denylist — the canonical guard for ask_fable.

This is the ONLY denylist ask_fable uses. It's a bundled, category-level list
that rejects questions about offensive security and biology dual-use before they
reach the model. The model scope contract in ``prompts.py`` does the primary
semantic scoping (it also refuses non-software domains like biology, with
computational domains — neuroscience, cognitive science, AI/ML, computer science
— treated as in-scope). The denylist is the deterministic, free Layer 2 gate.

Unlike earlier versions, ask_fable does NOT upgrade to salient-core's denylist
at runtime. The bundled list is deliberately broader (it carries biology
dual-use terms that the public, verification-free kernel does not).

Readable trigger words are kept out of source (base64-decoded at import) so an
agent that reads this repo doesn't ingest the vocabulary — the same rationale as
salient-core's ``_safeguard_vocab``.

Hot-path: the compiled regex is memoized on the (file mtime, file size) of the
operator's optional denylist extension file, so six guard calls per tool
invocation don't each re-read disk + re-compile. A change to the extension file
invalidates the cache automatically.
"""

from __future__ import annotations

import base64
import functools
import os
import re


def _d(blob: str) -> str:
    return base64.b64decode(blob).decode()


# Optional inflectional suffixes so plurals/gerunds/past-tense don't slip the filter
# ("exploits", "payloads", "keyloggers", "exfiltrating" matched NOTHING before — the
# trailing word boundary sat right after the base term, and one suffix defeated the
# whole list). ``_inflectable`` bakes a suffix onto EACH word of a (possibly multi-
# word) term, so a non-final word can inflect too ("request payloads" is still scrubbed
# rather than becoming a new false positive), and special-cases e-final stems' drop-e
# forms ("exfiltrate" -> "exfiltrating"/"exfiltrated", which a naive suffix misses).
# Derivations like "exploitation"/"deployment" stay unmatched (only these inflections
# are covered), preserving prior behaviour.
_INFLECT = r"(?:s|es|ed|ing)?"
_INFLECT_E = r"e?(?:s|es|d|ed|ing)?"  # e-final stems: drop-e -ing/-ed plus plain forms


def _inflectable(term: str) -> str:
    """Regex fragment matching ``term`` and its simple inflections, per word."""
    parts = []
    for word in term.split():
        esc = re.escape(word)
        parts.append(esc[:-1] + _INFLECT_E if word.endswith("e") else esc + _INFLECT)
    return r"\ ".join(parts)


# Category markers (base64). Whole-word matched, case-insensitive.
# Includes offensive-security and biology dual-use terms.
_OFFENSE_TERMS: tuple[str, ...] = (
    _d("ZXhwbG9pdA=="),
    _d("bWFsd2FyZQ=="),
    _d("cmFuc29td2FyZQ=="),
    _d("cGF5bG9hZA=="),
    _d("cm9vdGtpdA=="),
    _d("a2V5bG9nZ2Vy"),
    _d("Ym90bmV0"),
    _d("YmFja2Rvb3I="),
    _d("cHJpdmlsZWdlIGVzY2FsYXRpb24="),
    _d("bGF0ZXJhbCBtb3ZlbWVudA=="),
    _d("ZXhmaWx0cmF0ZQ=="),
    _d("YzIgc2VydmVy"),
    _d("Ymlvd2VhcG9u"),        # bioweapon
    _d("cGF0aG9nZW4="),        # pathogen
    _d("cGF0aG9nZW5pYw=="),    # pathogenic
    _d("dG94aW4="),            # toxin
    _d("dmlyYWwgdmVjdG9y"),    # viral vector
    _d("Z2VuZSBkcml2ZQ=="),    # gene drive
    _d("c3ludGhldGljIGJpb2xvZ3k="),  # synthetic biology
    _d("YmlvaGF6YXJk"),        # biohazard
)


# Benign multi-word phrases (base64) that rescue an otherwise-ambiguous offense
# term from a false positive. These are NOT a kill-switch: only the exact benign
# phrase is neutralized before matching, so "request payload" passes while a bare
# "exploit"/"backdoor" — or "payload" outside a benign phrase — is still rejected.
_ALLOW_TERMS: tuple[str, ...] = (
    _d("cmVxdWVzdCBwYXlsb2Fk"),
    _d("cmVzcG9uc2UgcGF5bG9hZA=="),
    _d("anNvbiBwYXlsb2Fk"),
    _d("aHR0cCBwYXlsb2Fk"),
    _d("bWVzc2FnZSBwYXlsb2Fk"),
    _d("ZXZlbnQgcGF5bG9hZA=="),
    _d("cGF5bG9hZCBzdHJ1Y3R1cmU="),
    _d("cGF5bG9hZCBzY2hlbWE="),
    _d("cGF5bG9hZCBib2R5"),
    _d("eG1sIHBheWxvYWQ="),
    _d("YXBpIHBheWxvYWQ="),
    _d("d2ViaG9vayBwYXlsb2Fk"),
)


def _extra_terms(env_var: str) -> list[str]:
    """Optional user-supplied lines, one term per line, from the file named by
    ``env_var`` (so an operator can tighten or loosen the filter without a code
    change)."""
    path = os.environ.get(env_var)
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    except OSError:
        return []


def _fingerprint(env_var: str) -> tuple[int, int] | None:
    """(mtime, size) of the file named by ``env_var``, or None if not configured.
    Used as the cache key so editing the file invalidates the memoized pattern."""
    path = os.environ.get(env_var)
    if not path:
        return None
    try:
        st = os.stat(path)
        return (int(st.st_mtime), int(st.st_size))
    except OSError:
        return (0, 0)  # file unconfigured / unreadable — empty fingerprint


@functools.lru_cache(maxsize=4)
def _pattern(fingerprint: tuple[int, int] | None) -> re.Pattern[str]:
    terms = [_inflectable(t) for t in (*_OFFENSE_TERMS, *_extra_terms("ASK_FABLE_DENYLIST_FILE"))]
    joined = "|".join(terms)
    # Word-ish boundaries so "exploit" matches but incidental substrings inside a
    # larger token are less likely to false-trip. ``_inflectable`` also catches plain
    # inflections (exploits/exploiting) that a bare boundary would let through.
    return re.compile(rf"(?<![A-Za-z0-9_])(?:{joined})(?![A-Za-z0-9_])", re.IGNORECASE)


@functools.lru_cache(maxsize=4)
def _allow_pattern(fingerprint: tuple[int, int] | None) -> re.Pattern[str] | None:
    raw = [*_ALLOW_TERMS, *_extra_terms("ASK_FABLE_ALLOWLIST_FILE")]
    if not raw:
        return None
    # Longest-first so a phrase isn't half-consumed by a shorter overlapping one.
    raw.sort(key=len, reverse=True)
    # Mirror the offense pattern's inflection so a plural benign phrase
    # ("request payloads") is scrubbed too — otherwise the offense pass would
    # re-flag the now-plural "payloads" and undo the rescue.
    joined = "|".join(_inflectable(t) for t in raw)
    return re.compile(rf"(?<![A-Za-z0-9_])(?:{joined})(?![A-Za-z0-9_])", re.IGNORECASE)


def _current_pattern() -> re.Pattern[str]:
    """Return the cached compiled pattern, reloading if the extension file changed."""
    return _pattern(_fingerprint("ASK_FABLE_DENYLIST_FILE"))


def _current_allow_pattern() -> re.Pattern[str] | None:
    return _allow_pattern(_fingerprint("ASK_FABLE_ALLOWLIST_FILE"))


def check_denylist(prompt: str) -> tuple[bool, str]:
    """(allowed, reason). Mirrors salient_core's check_prompt_intent contract.

    Benign allowlisted phrases (e.g. "request payload") are neutralized from the
    text before the offense pattern runs, so a legitimate engineering question
    isn't tripped by an ambiguous word used in a plainly-benign phrase. Any
    offense term left standing outside such a phrase still rejects.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return True, ""
    scrubbed = prompt
    allow = _current_allow_pattern()
    if allow is not None:
        scrubbed = allow.sub(" ", prompt)
    if _current_pattern().search(scrubbed):
        return False, "offensive-security content"
    return True, ""
