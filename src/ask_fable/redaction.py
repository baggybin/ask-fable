from __future__ import annotations

import re
from typing import Final

# The keyword is unanchored on the left so a prefixed name still matches
# (`db_password=`, `client_secret:`, `STRIPE_SECRET_KEY=`); `secret` may carry
# separator-joined suffixes (`aws_secret_access_key =`) but not bare letters, so
# prose like "secretly: ..." is left alone. Plurals count (`secrets:`,
# `passwords =`) — a dump of several credentials is still a dump of credentials.
#
# The suffix repetition is BOUNDED ({0,4} groups of <= 32 chars). Unbounded
# (`(?:[_-][a-z0-9]+)*`) it consumed the rest of the line at EVERY `secret`
# position and backtracked out again when no `:`/`=` followed, which is quadratic
# in the line length: 56 KB of "secret_" took 7 s and 200 KB took 101 s, on the
# event loop, in every sink that redacts (hub, outputs, session dumps, traces).
# Bounding caps the work per start position, so the scan is linear. No real key
# name carries five separator-joined suffixes.
_SECRET_LINE: Final = re.compile(
    r"(?im)(authorization[ \t]*:[ \t]*(?:bearer[ \t]+)?|"
    r"(?:api[_-]?keys?|access[_-]?keys?|private[_-]?keys?|ssh[_-]?keys?"
    r"|passw(?:or)?ds?|passphrases?|secrets?(?:[_-][a-z0-9]{1,32}){0,4}|cookies?)"
    r"[ \t]*[:=][ \t]*)"
    r"(?:\"[^\r\n]*\"|'[^\r\n]*'|[^\r\n]*\S[ \t]*)"
)
# `token` is deliberately NOT in _SECRET_LINE: answers ABOUT tokens (fencing
# tokens, token buckets, tokenizers) are ordinary software prose, and swallowing
# the rest of the line mangles them. A token assignment is redacted only when the
# value actually looks like a credential — quoted, or a bare run of >= 16
# credential characters — so `token = 42` and `token = self.seq` survive while
# `token: abcdef0123456789ABCD` does not. Known credential shapes (JWTs, provider
# keys) are caught by their own patterns regardless of label. The replacement
# keeps the rest of the line, so prose after a redacted value is preserved.
#
# The key prefix is anchored with a lookbehind rather than `\b`: `\b[a-z0-9_-]*`
# started a fresh attempt at every hyphen in a run like "sk-sk-sk-…", consuming
# the rest of the run each time (200 KB took 114 s). A start position that is
# itself preceded by a key character can only repeat work an earlier position
# already did, so refusing it makes the scan linear. The lookbehind is what buys
# that (one attempt per run, not one per character), so the prefix stays
# unbounded: capping it at 64 only dropped real matches — a long id followed by
# `-token:` — without making anything faster.
_TOKEN_LINE: Final = re.compile(
    r"(?i)((?<![a-z0-9_\-])[a-z0-9_\-]*token[ \t]*[:=][ \t]*)"
    r"(\"[^\"\r\n]*\"|'[^'\r\n]*'|[A-Za-z0-9_\-+/=]{16,})"
)
# The scheme start is anchored for the same reason as _TOKEN_LINE's key prefix:
# `[a-z][a-z0-9+.-]*` began a fresh attempt at EVERY letter, each one consuming the
# rest of the run before failing to find `://`. On 180 KB of `token-` that took 80 s.
# This pattern predates the PR #100 batch; it is the third of the same family.
_URI_USERINFO: Final = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_JWT: Final = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
# A quoted key paired with a quoted value — JSON (`"access_token": "…"`), Python
# dict reprs (`'client_secret': '…'`) and hash literals (`"password" => "…"`).
# Whether the key names a secret is decided word-by-word in `_is_secret_key`
# (camelCase is split), so `accessToken`/`db_password`/`clientSecret` are caught
# while `max_tokens`/`tokenizer` are not. Only string values are replaced.
_QUOTED_PAIR: Final = re.compile(
    r"""(?P<head>(?P<kq>["'])(?P<key>[A-Za-z0-9_.\- ]{1,64})(?P=kq)\s*(?:=>|[:=])\s*)"""
    r"""(?P<val>"(?:[^"\\\r\n]|\\.)*"|'(?:[^'\\\r\n]|\\.)*')"""
)
_SECRET_WORDS: Final = frozenset(
    {"password", "passwd", "passphrase", "secret", "cookie", "authorization", "token",
     "apikey", "credential", "bearer", "jwt", "pwd"}
)
_SECRET_PAIRS: Final = frozenset(
    {("api", "key"), ("private", "key"), ("access", "key"), ("ssh", "key")}
)
# A name whose LAST word describes a credential rather than being one: the type,
# the id, where it lives, how many there are. `"token_type": "Bearer"`,
# `"api_key_id"`, `"credentials_file"` and `"tokens_used"` are metadata every
# reader needs, and blanking them hid usage numbers and auth diagnostics.
_DESCRIPTOR_TAILS: Final = frozenset(
    {"count", "counts", "length", "limit", "size", "usage", "used", "remaining",
     "name", "hint", "file", "path", "dir", "type", "kind", "format", "id", "ids",
     "expiry", "expires", "exp", "ttl", "prefix", "provider", "present", "set",
     "enabled", "required", "version"}
)
# Deliberately NOT tails: `header`, `field`, `value`, `data`, `string`, `scope`,
# `source`, `status`, `error`, `label`, `suffix`. Each names a container that can
# hold the credential itself — `cookie_header` IS the cookies — so disarming on
# them would turn a readability nicety into a leak.
# Words that qualify a token COUNT (`max_tokens`, `cache_read_input_tokens`).
# A plural `tokens` tail preceded only by these is a number, not a credential —
# but `api_tokens` / `auth_tokens` are a list of credentials.
_COUNT_QUALIFIERS: Final = frozenset(
    {"total", "max", "min", "input", "output", "prompt", "completion", "cache",
     "cached", "creation", "read", "write", "reasoning", "thinking", "num", "n",
     "avg", "sum", "budget", "context", "window", "chunk", "estimated", "est"}
)


def _words(name: str) -> list[str]:
    """Split an identifier into lowercase words (camelCase and _ . - separators)."""
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
    return [w for w in re.split(r"[_.\- ]+", split) if w]


def _singular(word: str) -> str:
    """`secrets` -> `secret`, so a dump of several credentials is still caught.
    Words already ending in `ss` (`access`) and short words are left alone."""
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word
_XML_SECRET: Final = re.compile(r"(?is)(<(?:password|token|secret|api[_-]?key)>).*?(</(?:password|token|secret|api[_-]?key)>)")
_PEM_PRIVATE: Final = re.compile(r"(?s)-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----")
_KNOWN_TOKEN: Final = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b")
# Bare provider keys that appear on their own (no `api_key:`/`token=` label to catch
# them via _SECRET_LINE) — e.g. a provider echoing an "invalid api key sk-..." error
# into a tool result or trace. Prefix-anchored so normal prose ("risk-", "task-")
# can't match: each needs a distinctive provider prefix + a long alnum body.
_BARE_PROVIDER_KEY: Final = re.compile(
    r"\b(?:"
    r"sk-ant-[A-Za-z0-9_-]{20,}"        # Anthropic
    r"|sk-(?:proj-)?[A-Za-z0-9_-]{20,}"  # OpenAI (incl. sk-proj- keys, which contain -/_)
    r"|AIza[A-Za-z0-9_-]{20,}"         # Google
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"   # Slack
    r"|[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"  # Stripe secret / restricted keys
    r")\b"
)


def is_secret_key(name: str) -> bool:
    """True when a key NAMES a credential, judged word by word.

    camelCase is split, plurals are folded (`secrets`, `passwords`, `api_tokens`),
    and a trailing descriptor (`_type`, `_count`, `_id`) means the key describes a
    credential instead of holding one. A word ENDING in `token` counts
    (`csrftoken`) while a word merely containing it does not (`tokenizer`)."""
    parts = _words(name)
    if not parts:
        return False
    if parts[-1] in _DESCRIPTOR_TAILS:
        return False
    # `tokens` after count qualifiers only (`max_tokens`, `cache_read_input_tokens`).
    if parts[-1] == "tokens" and all(w in _COUNT_QUALIFIERS for w in parts[:-1]):
        return False
    words = [_singular(w) for w in parts]
    if any(w in _SECRET_WORDS for w in words):
        return True
    if any(len(w) > 5 and w.endswith("token") for w in words):
        return True
    return any(pair in _SECRET_PAIRS for pair in zip(words, words[1:], strict=False))


_is_secret_key = is_secret_key  # internal alias kept for existing call sites


def redact_text(value: str) -> tuple[str, int]:
    count = 0

    def keyed(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}[REDACTED]"

    def quoted_pair(match: re.Match[str]) -> str:
        nonlocal count
        if not _is_secret_key(match.group("key")):
            return match.group(0)
        count += 1
        q = match.group("val")[0]
        return f"{match.group('head')}{q}[REDACTED]{q}"

    def xml_value(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}[REDACTED]{match.group(2)}"

    def block(_match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "[REDACTED]"

    def uri(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}[REDACTED]@"

    redacted = _TOKEN_LINE.sub(keyed, value)
    redacted = _SECRET_LINE.sub(keyed, redacted)
    redacted = _QUOTED_PAIR.sub(quoted_pair, redacted)
    redacted = _XML_SECRET.sub(xml_value, redacted)
    redacted = _PEM_PRIVATE.sub(block, redacted)
    redacted = _KNOWN_TOKEN.sub(block, redacted)
    redacted = _BARE_PROVIDER_KEY.sub(block, redacted)
    redacted = _URI_USERINFO.sub(uri, redacted)
    return _JWT.sub(block, redacted), count
