from __future__ import annotations

import re
from typing import Final

_SECRET_LINE: Final = re.compile(
    r"(?im)(authorization[ \t]*:[ \t]*(?:bearer[ \t]+)?|"
    r"(?:api[_-]?key|token|password|secret|cookie)[ \t]*[:=][ \t]*)"
    r"(?:\"[^\r\n]*\"|'[^\r\n]*'|[^\r\n]*\S[ \t]*)"
)
_URI_USERINFO: Final = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_JWT: Final = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_JSON_SECRET: Final = re.compile(r'(?i)("(?:api[_-]?key|authorization|cookie|password|secret|token)"\s*:\s*)"[^"]*"')
_XML_SECRET: Final = re.compile(r"(?is)(<(?:password|token|secret|api[_-]?key)>).*?(</(?:password|token|secret|api[_-]?key)>)")
_PEM_PRIVATE: Final = re.compile(r"(?s)-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----")
_KNOWN_TOKEN: Final = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b")


def redact_text(value: str) -> tuple[str, int]:
    count = 0

    def keyed(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}[REDACTED]"

    def json_value(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f'{match.group(1)}"[REDACTED]"'

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

    redacted = _SECRET_LINE.sub(keyed, value)
    redacted = _JSON_SECRET.sub(json_value, redacted)
    redacted = _XML_SECRET.sub(xml_value, redacted)
    redacted = _PEM_PRIVATE.sub(block, redacted)
    redacted = _KNOWN_TOKEN.sub(block, redacted)
    redacted = _URI_USERINFO.sub(uri, redacted)
    return _JWT.sub(block, redacted), count
