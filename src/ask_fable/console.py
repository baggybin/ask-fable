"""Human-facing progress/reasoning output for ask_fable — to STDERR only.

An MCP stdio server owns stdout for the JSON-RPC protocol, so anything meant for
a person MUST go to stderr (Claude Code surfaces server stderr in its MCP logs /
``claude --debug``; in a terminal it prints live). This module renders a tidy,
optionally-colored trace of what the tool is doing — guard result, each model
being asked, elapsed time, streamed reasoning excerpts, and the synthesis step.

Silence everything with ``ASK_FABLE_QUIET=1``; hide model reasoning (keep the
step trace) with ``ASK_FABLE_SHOW_REASONING=0``. With ``ASK_FABLE_STREAM_REASONING=1``
the server streams Fable's reasoning live (block by block, via ``stream_think``) as
it arrives instead of printing one post-hoc excerpt. Output is best-effort and never
raises — reporting must never break a tool call.
"""

from __future__ import annotations

import os
import sys
import textwrap


def _truthy(name: str, default: bool) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _quiet() -> bool:
    return _truthy("ASK_FABLE_QUIET", False)


def _show_reasoning() -> bool:
    return _truthy("ASK_FABLE_SHOW_REASONING", True)


def _color() -> bool:
    # Colorize only for a real terminal, and honor the NO_COLOR convention.
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stderr.isatty()
    except Exception:  # noqa: BLE001
        return False


# ANSI palette (resolved once per Reporter so a non-TTY log stays plain).
_CODES = {
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "magenta": "\033[35m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def _paint(code: str, text: str, color: bool) -> str:
    if not color:
        return text
    return f"{_CODES.get(code, '')}{text}{_CODES['reset']}"


def _bar_line(body: str, color: bool) -> str:
    """One line inside the box — defined once so ``Reporter`` and ``notice`` can
    never drift on the glyph or the palette."""
    return _paint("cyan", "│ ", color) + body


def _write(line: str) -> None:
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — reporting must never break the tool
        pass


def notice(text: str, *, tone: str = "warn") -> None:
    """One operator-facing line that belongs to no single tool call — a circuit
    breaker tripping is about the backend, not the council that happened to
    trigger it. Drawn with the box bar so it reads in place inside whichever
    ``Reporter`` trace is open at the time. ``tone="ok"`` marks good news (a
    backend recovering) so it does not scan as another failure. Honors
    ``ASK_FABLE_QUIET``; best-effort like everything here."""
    if _quiet():
        return
    color = _color()
    mark, shade = ("✓", "green") if tone == "ok" else ("⚠", "yellow")
    _write(_bar_line(f"{_paint(shade, mark, color)} {text}", color))


class Reporter:
    """Renders one boxed trace for a single tool call, to stderr."""

    def __init__(self, title: str) -> None:
        self.enabled = not _quiet()
        self.reasoning = _show_reasoning()
        self.color = _color()
        self.title = title
        if self.enabled:
            self._header(title)

    def _c(self, code: str, text: str) -> str:
        return _paint(code, text, self.color)

    def _emit(self, line: str) -> None:
        if not self.enabled:
            return
        _write(line)

    def _header(self, title: str) -> None:
        bar = "─" * max(8, 46 - len(title))
        self._emit("")
        self._emit(self._c("cyan", f"╭─ {self._c('bold', title)} {bar}"))

    def _bar(self, body: str) -> str:
        return _bar_line(body, self.color)

    def info(self, label: str, detail: str = "") -> None:
        tail = self._c("dim", f" {detail}") if detail else ""
        self._emit(self._bar(f"{self._c('dim', '▸')} {label}{tail}"))

    def start(self, what: str) -> None:
        self._emit(self._bar(f"{self._c('yellow', '⋯')} {what} …"))

    def ok(self, what: str, secs: float | None = None) -> None:
        t = self._c("dim", f" ({secs:.1f}s)") if secs is not None else ""
        self._emit(self._bar(f"{self._c('green', '✓')} {what}{t}"))

    def warn(self, what: str, secs: float | None = None) -> None:
        t = self._c("dim", f" ({secs:.1f}s)") if secs is not None else ""
        self._emit(self._bar(f"{self._c('yellow', '⚠')} {what}{t}"))

    def fail(self, what: str, secs: float | None = None) -> None:
        t = self._c("dim", f" ({secs:.1f}s)") if secs is not None else ""
        self._emit(self._bar(f"{self._c('red', '✗')} {what}{t}"))

    def think(self, who: str, text: str, limit: int = 700) -> None:
        """Render a model's reasoning/thinking as an indented, wrapped excerpt."""
        if not (self.enabled and self.reasoning):
            return
        text = (text or "").strip()
        if not text:
            return
        if len(text) > limit:
            text = text[:limit].rstrip() + " …"
        self._emit(self._bar(self._c("magenta", f"  💭 {who} reasoning:")))
        for para in text.splitlines() or [text]:
            for wrapped in textwrap.wrap(para, width=76) or [""]:
                self._emit(self._bar(self._c("dim", f"     {wrapped}")))

    def stream_think(self, who: str):
        """Return a best-effort sink ``fn(delta)`` that streams a model's reasoning to
        stderr live, as each block arrives — a header is printed once on the first
        non-empty delta. Returns ``None`` when disabled or reasoning is hidden, so the
        caller can fall back to the post-hoc ``think`` excerpt."""
        if not (self.enabled and self.reasoning):
            return None
        state = {"header": False}

        def sink(delta: str) -> None:
            try:
                delta = (delta or "").strip()
                if not delta:
                    return
                if not state["header"]:
                    self._emit(self._bar(self._c("magenta", f"  💭 {who} reasoning (live):")))
                    state["header"] = True
                for para in delta.splitlines() or [delta]:
                    for wrapped in textwrap.wrap(para, width=76) or [""]:
                        self._emit(self._bar(self._c("dim", f"     {wrapped}")))
            except Exception:  # noqa: BLE001 — a streaming sink must never break the tool
                pass

        return sink

    def footer(self, summary: str = "") -> None:
        if summary:
            self._emit(self._bar(self._c("bold", summary)))
        self._emit(self._c("cyan", "╰" + "─" * 47))
        self._emit("")
