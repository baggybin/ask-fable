"""LAN context bus client — transport and request auth for ``context-busd``.

The daemon is a *ciphertext shelf*: blobs are sealed (``context_crypto``) by the
shim in ``context_store`` before they ever reach this module, so nothing here
sees a content key or a plaintext value. This module owns:

- URL resolution (``ASK_FABLE_CONTEXT_BUS`` / config key ``context_bus``) and
  transport over a unix socket or plain HTTP.
- Request authentication with a **separate bus token** (never the content PSK):
  ``X-AF-Auth: <ts>.<nonce>.<hex HMAC-SHA256(token,
  method|path|ts|nonce|sha256(body))>``. The canonical string binds the method
  and path as well as the body, so a captured header cannot be replayed onto a
  different operation.
- Failure classes (``BusError`` family) that the store shim maps to
  ``last_error()`` so a bus outage degrades — it never collapses into "missing".

Fail-closed rules:

- A token file that exists must be 0600 and non-trivial, or every request fails.
- A response whose ``proto`` is not the client's is refused (mixed-version
  fleets degrade instead of mis-parsing).
- The client refuses an oversized body before sending it; the daemon enforces
  the same cap again independently.
- Timeouts are explicit (default 5 s) — a hung daemon degrades the call instead
  of hanging every MCP instance on the machine.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import socket
import time
from contextvars import ContextVar
from pathlib import Path

from . import _paths, config, context_crypto

PROTO = 1
DEFAULT_TIMEOUT = 5.0
TOKEN_MIN_LEN = 16
DEFAULT_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB; the daemon enforces the same cap
_MAX_RESPONSE = 64 * 1024 * 1024
# A bounded listing (``{"bounded": true}`` on /v1/list) carries every row's metadata but
# a row's envelope — the only place its description lives — only while that envelope is
# at most LIST_INLINE_MAX bytes and the running total stays within LIST_INLINE_BUDGET (a
# quarter of the response cap, leaving the rest for metadata). A full listing outgrew
# the response cap once the bus held more than 64 MiB, and listing then failed for good.
LIST_INLINE_MAX = 256 * 1024
LIST_INLINE_BUDGET = 16 * 1024 * 1024
# Clock-skew slack shared by the daemon's persistent write floor and the client's
# reader-side rollback guard: within this window, out-of-order timestamps from
# skewed clocks are tolerated; beyond it, older values are refused.
FLOOR_SLACK_NS = 60 * 1_000_000_000


class BusError(RuntimeError):
    """Base class for every bus-client failure — safe to catch as a group."""


class BusUnreachable(BusError):
    """Transport failed: refused, timed out, DNS, socket error."""


class BusAuthError(BusError):
    """Missing/loose token, or the daemon rejected the request auth."""


class BusRejected(BusError):
    """The daemon refused the request (409 stale/CAS, 413 too large, …)."""


class BusProtocolError(BusError):
    """The daemon speaks a protocol this client does not understand."""


def bus_url() -> str:
    """Configured bus URL (``unix:///abs/path`` or ``http://host:port``), or ""."""
    return (
        config.get_str("context_bus")
        or os.environ.get("ASK_FABLE_CONTEXT_BUS")
        or ""
    ).strip()


def token_path() -> Path:
    override = os.environ.get("ASK_FABLE_CONTEXT_BUS_TOKEN_FILE")
    if override:
        return Path(override).expanduser()
    return _paths.xdg_config_dir() / "ask_fable" / "context_bus_token"


def load_token() -> bytes:
    """Read the bus token. Presence implies intent: a loose/malformed file is a
    hard failure, not "no token"."""
    p = token_path()
    try:
        st = p.stat()
    except OSError as exc:
        raise BusAuthError(f"bus token not readable: {p} ({exc})") from exc
    if st.st_mode & 0o077:
        raise BusAuthError(
            f"bus token {p} is group/other-accessible "
            f"(mode {oct(st.st_mode & 0o777)}); chmod 600 it"
        )
    try:
        raw = p.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise BusAuthError(f"bus token read failed: {p} ({exc})") from exc
    if len(raw) < TOKEN_MIN_LEN:
        raise BusAuthError(f"bus token {p} is too short (need >= {TOKEN_MIN_LEN} chars)")
    return raw.encode("utf-8")


def _optional_token() -> bytes | None:
    """No token file -> None (fine on a unix socket). A present-but-bad file
    raises — that is deliberate."""
    if not token_path().exists():
        return None
    return load_token()


def machine_id() -> str:
    override = (
        config.get_str("machine_id") or os.environ.get("ASK_FABLE_MACHINE_ID") or ""
    ).strip()
    return override or (socket.gethostname() or "unknown")


# The MCP client (harness) that drove the current tool call, recorded by the
# server once per call and read on the seal path so the writer id can attribute
# a blob to its client without threading the id through every store signature. A
# ContextVar copies correctly across the awaits inside one turn and isolates
# concurrent calls (the MCP SDK's own request-context pattern).
_writer_client: ContextVar[str] = ContextVar("ask_fable_writer_client", default="")


def set_writer_client(client: str | None) -> None:
    """Record the calling MCP client for the next seal's writer id."""
    _writer_client.set((client or "").strip())


def _writer_component(s: str) -> str:
    """Reduce one attribution field to a single ``/``-safe token so the composite
    writer stays unambiguously splittable and the daemon's one-line logs stay
    single-token. Keeps alnum and ``._-``; collapses everything else to ``_``."""
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in s).strip("_")


def writer_id() -> str:
    """Composite writer attribution: ``machine[/client[/model]]``.

    Every component is reduced to a ``/``-safe token so readers recover the
    fields by splitting on ``/``; a realistic machine id (a hostname) is
    unchanged by that, so a pre-composite bare-machine writer still parses as the
    same one-element split. ``machine`` is always present (``machine_id``).
    ``client`` is the auto-detected MCP harness when known
    (``set_writer_client``); it is omitted rather than written as ``unknown`` so
    an unresolved client leaves a clean bare-machine writer. ``model`` is
    appended only when explicitly supplied via ``ASK_FABLE_MODEL_ID`` or config
    ``model_id`` — no source can auto-detect the driving model — and only when a
    client is present, so positions never shift. The whole string is bound into
    the envelope AAD, so every component is authenticated together."""
    parts = [_writer_component(machine_id()) or "unknown"]
    client = _writer_component(_writer_client.get() or "")
    if client and client.lower() != "unknown":
        parts.append(client)
        model = _writer_component(
            config.get_str("model_id") or os.environ.get("ASK_FABLE_MODEL_ID") or ""
        )
        if model:
            parts.append(model)
    return "/".join(parts)


def max_body_bytes() -> int:
    try:
        n = int(os.environ.get("ASK_FABLE_CONTEXT_MAX_BYTES") or DEFAULT_MAX_BYTES)
    except (TypeError, ValueError):
        return DEFAULT_MAX_BYTES
    return n if n > 0 else DEFAULT_MAX_BYTES


def _timeout() -> float:
    try:
        t = float(os.environ.get("ASK_FABLE_CONTEXT_BUS_TIMEOUT") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return t if t > 0 else DEFAULT_TIMEOUT


def canonical(method: str, path: str, ts: int, nonce: str, body: bytes) -> str:
    """The exact string the HMAC covers. Method+path are bound, so a captured
    header cannot be spliced onto another route; the body is hashed, not signed
    verbatim, so the canonical string stays small and fixed-shape."""
    return f"{method}|{path}|{ts}|{nonce}|{hashlib.sha256(body).hexdigest()}"


def _auth_header(token: bytes, method: str, path: str, body: bytes) -> str:
    ts = int(time.time())
    nonce = os.urandom(16).hex()
    sig = hmac.new(token, canonical(method, path, ts, nonce, body).encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{nonce}.{sig}"


class _UnixHTTPConnection(http.client.HTTPConnection):
    """Minimal AF_UNIX transport for http.client (stdlib has none)."""

    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._unix_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._unix_path)


def _connection(url: str, timeout: float) -> http.client.HTTPConnection:
    if url.startswith("unix://"):
        path = url[len("unix://"):]
        if not path.startswith("/"):
            raise BusError(f"unix bus URL must be unix:///abs/path, got {url!r}")
        return _UnixHTTPConnection(path, timeout)
    if url.startswith("http://"):
        hostport = url[len("http://"):].split("/", 1)[0]
        host, _, port_s = hostport.partition(":")
        try:
            port = int(port_s) if port_s else 80
        except ValueError as exc:
            raise BusError(f"bad port in bus URL {url!r}") from exc
        return http.client.HTTPConnection(host, port, timeout=timeout)
    raise BusError(f"unsupported bus URL (need unix:// or http://): {url!r}")


def request(
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    url: str | None = None,
    token: bytes | None = None,
    timeout: float | None = None,
    authenticated: bool = True,
) -> dict:
    """One request/response against the daemon. Raises ``BusError`` subclasses;
    never returns a non-ok response body. ``authenticated=False`` is for
    ``/health`` only, so triage works without the token."""
    target = (url if url is not None else bus_url()).strip()
    if not target:
        raise BusError("context bus URL is not configured")
    body = json.dumps(payload or {}, separators=(",", ":")).encode("utf-8")
    cap = max_body_bytes()
    if len(body) > cap:
        raise BusRejected(f"request body is {len(body)} bytes (cap {cap})")
    headers = {"Content-Type": "application/json", "X-AF-Proto": str(PROTO)}
    if authenticated:
        tok = token
        if tok is None:
            tok = _optional_token()
        if tok is not None:
            headers["X-AF-Auth"] = _auth_header(tok, method, path, body)
    conn = _connection(target, timeout if timeout is not None else _timeout())
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read(_MAX_RESPONSE + 1)
        status = resp.status
        detail = ""
        try:
            obj = json.loads(raw.decode("utf-8")) if raw else {}
            if isinstance(obj, dict):
                detail = str(obj.get("error") or "")
        except (UnicodeDecodeError, ValueError):
            obj = {}
        if status == 401:
            raise BusAuthError(detail or "daemon rejected the request auth")
        if status in (409, 413):
            raise BusRejected(f"{status} {detail or 'rejected'}")
        if status >= 400:
            raise BusError(f"daemon returned {status} {detail or ''}".strip())
        if len(raw) > _MAX_RESPONSE:
            raise BusProtocolError("response exceeds the client cap")
        if not isinstance(obj, dict):
            raise BusProtocolError("response is not a JSON object")
        resp_proto = obj.get("proto")
        if resp_proto != PROTO:
            raise BusProtocolError(
                f"daemon speaks proto {resp_proto!r}, client speaks {PROTO}"
            )
        return obj
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        raise BusUnreachable(f"unreachable {target}: {type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()


def _check_meta(key: str, envelope: str, writer: str, ts_ns: int) -> None:
    """Cross-check the daemon's claimed writer/ts against the AAD-bound envelope
    header. The daemon cannot read inside the ciphertext, so this comparison is
    how a reader catches a daemon that stored or served inconsistent metadata —
    refuse it (degraded) rather than return data under a false label."""
    try:
        _ver, _kid, head_writer, head_ts = context_crypto.envelope_meta(envelope)
    except context_crypto.SealedError as exc:
        raise BusProtocolError(f"daemon served an unparseable envelope for '{key}': {exc}") from exc
    if writer != head_writer or ts_ns != head_ts:
        raise BusProtocolError(
            f"metadata mismatch for '{key}': daemon claims writer={writer!r} ts={ts_ns}, "
            f"envelope header says writer={head_writer!r} ts={head_ts}"
        )


class RemoteBusBackend:
    """The ``context_store`` backend for bus mode. Values handed to ``put`` are
    already sealed; rows handed back carry the armor, which the shim unseals."""

    def mode(self) -> str:
        return "bus"

    def put(self, key: str, stored_value: str, meta: dict,
            expected_version: int | None = None) -> bool:
        # The bus has its own concurrency control on the daemon (a guarded conditional upsert
        # on writer_ts); `expected_version` is accepted for interface parity with the local
        # backend but not used here — a version-based precondition would be a daemon protocol
        # change (deferred). Bus writes stay newest-wins.
        request(
            "POST",
            "/v1/put",
            {
                "key": key,
                "envelope": stored_value,
                "writer": str(meta.get("writer") or ""),
                "ts_ns": int(meta.get("ts_ns") or 0),
                "plaintext_bytes": int(meta.get("plaintext_bytes") or 0),
            },
        )
        return True

    def row(self, key: str) -> tuple[str, float, str] | None:
        obj = request("POST", "/v1/get", {"key": key})
        if not obj.get("found"):
            return None
        envelope = str(obj.get("envelope") or "")
        _check_meta(key, envelope, str(obj.get("writer") or ""), int(obj.get("ts_ns") or 0))
        return (envelope, float(obj.get("received_ts") or 0.0), "")

    def row_versioned(self, key: str) -> tuple[str, int] | None:
        # No row version over the bus (it is newest-wins, not CAS): report version 0, which the
        # local CAS treats as "expect absent" and the save path passes straight through.
        row = self.row(key)
        return (row[0], 0) if row is not None else None

    def delete(self, key: str) -> bool:
        return bool(request("POST", "/v1/delete", {"key": key}).get("deleted"))

    def entries(self) -> list[dict]:
        # Bounded: a row carries its envelope only when the daemon inlined it; the rest
        # list as sealed metadata with the writer's claimed plaintext size. An older
        # daemon ignores the flag and inlines every envelope, which parses the same way.
        obj = request("POST", "/v1/list", {"bounded": True})
        rows = obj.get("rows")
        out: list[dict] = []
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict):
                continue
            key = str(r.get("key") or "")
            row = {
                "key": key,
                "ts": float(r.get("received_ts") or 0.0),
                "description": "",
                "bytes": int(r.get("plaintext_bytes") or 0),
                "sealed": True,
            }
            if "envelope" in r:
                envelope = str(r.get("envelope") or "")
                _check_meta(key, envelope, str(r.get("writer") or ""), int(r.get("ts_ns") or 0))
                row["value"] = envelope
            out.append(row)
        return out

    def location(self) -> str:
        return bus_url() or "context-bus:unconfigured"


def bus_status() -> dict:
    """Triage helper: configured/reachable/proto plus keyring kid count. Health
    is fetched unauthenticated so it works before the token is fixed."""
    url = bus_url()
    out: dict = {"configured": bool(url), "url": url or None, "reachable": False,
                 "proto": None, "keyring_kids": None}
    try:
        out["keyring_kids"] = len(context_crypto.load_keyring().kids())
    except context_crypto.CryptoError as exc:
        out["keyring_error"] = str(exc)
    if not url:
        return out
    try:
        health = request("GET", "/health", None, authenticated=False)
        out["reachable"] = True
        out["proto"] = health.get("proto")
        out["rows"] = health.get("rows")
    except BusError as exc:
        out["error"] = str(exc)
    return out
