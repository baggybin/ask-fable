"""``context-busd`` — the owner daemon for the LAN context bus.

One nominated host runs this process as the **only** opener of the bus database;
every ask_fable instance on every machine (including the owner's) talks to it as
a client. The daemon is a *ciphertext shelf*: it holds no content key and never
sees a plaintext blob or description. Sealing happens client-side.

What the daemon does enforce:

- **Request auth** with the bus token (separate from the content PSK):
  ``X-AF-Auth: <ts>.<nonce>.<hex HMAC-SHA256(token,
  method|path|ts|nonce|sha256(body))>``, constant-time compare, ±60 s skew
  ("clock skew" in the 401 body), and an age-evicted nonce set for replay.
- **Metadata from the sealed envelope, not from the request.** The cleartext
  envelope header (writer + ts) is AAD-bound, so the daemon parses it with
  ``context_crypto.envelope_meta`` and rejects a PUT whose separate JSON fields
  disagree. Per-writer eviction and the persistent anti-replay floor therefore
  compare values a replayer cannot alter without breaking the AEAD tag — a
  mismatch between request metadata and envelope metadata is a hard 400.
- **Persistent replay floor:** a PUT whose envelope ts is older than the stored
  writer_ts (minus 60 s slack) is refused 409 — this survives a daemon restart,
  unlike the in-memory nonce set.
- **Per-writer retention:** ``ASK_FABLE_CONTEXT_MAX_ROWS`` is applied per writer,
  so one machine can never evict another's artifacts. Default 0 = unlimited.
- **Fail-closed exposure:** unix socket by default (0600 in a 0700 dir,
  ``SO_PEERCRED`` uid check where the platform supports it); TCP binds are
  loopback-only unless a token file is configured, and a non-loopback bind
  without one refuses to start. ``/health`` is the only unauthenticated route.
- **One process owns the DB:** an exclusive ``flock`` on ``<db>.lock`` refuses a
  second instance, and every SQLite connection is opened per operation inside
  this process.

CLI::

    context-busd serve   [--db PATH] [--socket PATH | --host H --port P] [--token-file PATH]
    context-busd migrate --from PATH [--bus URL] [--token-file PATH]

``migrate`` runs in the client role: it reads a local ``context.db``, seals every
plaintext row with the keyring, and PUTs it to the bus. Already-sealed rows are
skipped, so a re-run is safe.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import signal
import socket
import socketserver
import sqlite3
import stat
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import _paths, context_bus, context_crypto

SKEW_S = 60
_FLOOR_SLACK_NS = context_bus.FLOOR_SLACK_NS
_SWEEP_EVERY = 100
# Per-connection socket timeout: a peer that announces a body and never sends it
# (or idles on a keep-alive connection) is dropped instead of parking a thread.
REQUEST_TIMEOUT_S = 30.0


def _loopback_host(host_header: str) -> bool:
    """True when an HTTP ``Host`` header names a loopback address. A DNS-rebinding
    page reaches a loopback daemon under its own hostname, so a tokenless TCP bind
    only trusts requests addressed to ``localhost`` or a loopback literal."""
    h = host_header.strip()
    # The port is validated, not just discarded: `[::1]evil`, `localhost:evil` and
    # `[::1]:8788:x` all used to parse as loopback. Nothing reachable from a browser
    # URL, but a host check that accepts malformed input fails open by construction.
    if h.startswith("["):
        literal, _, rest = h[1:].partition("]")
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return False
        h = literal
    elif h.count(":") == 1:
        h, _, port = h.partition(":")
        if not port.isdigit():
            return False
    h = h.rstrip(".").lower()
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def default_db_path() -> Path:
    override = os.environ.get("ASK_FABLE_CONTEXT_BUS_DB")
    if override:
        return Path(override).expanduser()
    return _paths.xdg_state_dir() / "ask_fable" / "context_bus.db"


def default_socket_path() -> Path:
    override = os.environ.get("ASK_FABLE_CONTEXT_BUS_SOCKET")
    if override:
        return Path(override).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "ask_fable" / "context_bus.sock"
    return _paths.xdg_state_dir() / "ask_fable" / "context_bus.sock"


def _max_rows() -> int:
    return _paths.int_env("ASK_FABLE_CONTEXT_MAX_ROWS", 0)


def _max_bytes() -> int:
    return _paths.int_env("ASK_FABLE_CONTEXT_MAX_BYTES", context_bus.DEFAULT_MAX_BYTES)


def _read_token_file(path: Path) -> bytes:
    """Same rules as the client: 0600, >= 16 chars, presence implies intent."""
    try:
        st = path.stat()
    except OSError as exc:
        raise SystemExit(f"context-busd: token not readable: {path} ({exc})") from exc
    if st.st_mode & 0o077:
        raise SystemExit(
            f"context-busd: token {path} is group/other-accessible "
            f"(mode {oct(st.st_mode & 0o777)}); chmod 600 it"
        )
    raw = path.read_text(encoding="utf-8").strip()
    if len(raw) < context_bus.TOKEN_MIN_LEN:
        raise SystemExit(f"context-busd: token {path} is too short")
    return raw.encode("utf-8")


def _peer_uid(conn: socket.socket) -> int | None:
    """Linux SO_PEERCRED; None when unsupported (then the 0700 dir is the guard)."""
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid
    except OSError:
        return None


class _Nonces:
    """Age-evicted replay set. Bounded by the skew window, not by a count, so it
    cannot be force-evicted by flooding within the window."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def claim(self, nonce: str) -> bool:
        now = time.time()
        cutoff = now - 2 * SKEW_S
        with self._lock:
            for n, t in list(self._seen.items()):
                if t < cutoff:
                    del self._seen[n]
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


class ServerConfig:
    """Everything a handler needs; tests build this directly."""

    def __init__(self, db_path: Path, *, token: bytes | None = None, max_bytes: int | None = None):
        self.db_path = Path(db_path)
        self.token = token
        self.max_bytes = max_bytes if max_bytes is not None else _max_bytes()
        self.max_rows = _max_rows()
        self.started = time.time()
        self.nonces = _Nonces()
        self.puts = 0


def _open_db(path: Path) -> sqlite3.Connection:
    _paths.ensure_dir_secure(path.parent)
    if not path.exists():
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.DatabaseError:
        pass
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bus_context ("
        "key TEXT PRIMARY KEY, writer TEXT, writer_ts INTEGER, "
        "received_ts REAL, envelope TEXT, plaintext_bytes INTEGER)"
    )
    _paths.secure_sqlite_sidecars(path)
    return conn


def acquire_lock(db_path: Path) -> int | None:
    """Exclusive single-instance lock; returns the held fd or None. The caller
    keeps the fd open for the process lifetime (closing releases it)."""
    lock_path = db_path.with_suffix(db_path.suffix + ".lock")
    _paths.ensure_dir_secure(lock_path.parent)
    fd: int | None = None
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        return None


def _sweep(conn: sqlite3.Connection, cap: int) -> None:
    """Per-writer retention: each writer keeps its own cap of newest blobs."""
    if cap <= 0:
        return
    for writer, count in conn.execute(
        "SELECT writer, COUNT(*) FROM bus_context GROUP BY writer"
    ).fetchall():
        if count > cap:
            evict = count - int(cap * 0.9)
            conn.execute(
                "DELETE FROM bus_context WHERE key IN "
                "(SELECT key FROM bus_context WHERE writer=? "
                "ORDER BY writer_ts ASC LIMIT ?)",
                (writer, evict),
            )
    conn.commit()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ask-fable-busd/1"
    timeout = REQUEST_TIMEOUT_S

    # --- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib hook
        pass  # structured logging below; never bodies

    def _log(self, op: str, key: str, writer: str, status: int) -> None:
        print(
            json.dumps({"ts": time.time(), "op": op, "key": key, "writer": writer,
                        "status": status}),
            file=sys.stderr,
            flush=True,
        )

    def _send(self, status: int, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _reject(self, status: int, msg: str) -> None:
        self._send(status, {"proto": context_bus.PROTO, "error": msg})

    def _read_body(self) -> bytes | None:
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._reject(400, "bad Content-Length")
            return None
        cap = self.server.cfg.max_bytes  # type: ignore[attr-defined]
        if n < 0 or n > cap:
            self._reject(413, f"body too large ({n} bytes, cap {cap})")
            return None
        return self.rfile.read(n)

    def _authorized(self, path: str, raw: bytes) -> bool:
        cfg = self.server.cfg  # type: ignore[attr-defined]
        if cfg.token is None:
            return True
        hdr = self.headers.get("X-AF-Auth") or ""
        parts = hdr.split(".")
        if len(parts) != 3:
            self._reject(401, "missing or malformed X-AF-Auth")
            return False
        ts_s, nonce, sig = parts
        try:
            ts = int(ts_s)
        except ValueError:
            self._reject(401, "bad auth timestamp")
            return False
        if abs(time.time() - ts) > SKEW_S:
            self._reject(401, "clock skew")
            return False
        if not nonce or len(nonce) > 128 or not sig:
            self._reject(401, "bad auth fields")
            return False
        canon = context_bus.canonical("POST", path, ts, nonce, raw)
        expected = hmac.new(cfg.token, canon.encode(), hashlib.sha256).hexdigest()
        # compare_digest raises TypeError on a non-ASCII str, which killed the handler
        # thread with a traceback instead of returning 401. A signature is hex.
        if not sig.isascii() or not hmac.compare_digest(expected, sig):
            self._reject(401, "bad signature")
            return False
        if not cfg.nonces.claim(nonce):
            self._reject(401, "replay")
            return False
        return True

    # --- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        if self.path.split("?", 1)[0] != "/health":
            self._reject(404, "not found")
            return
        cfg = self.server.cfg  # type: ignore[attr-defined]
        conn = _open_db(cfg.db_path)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM bus_context").fetchone()[0]
        finally:
            conn.close()
        self._send(200, {"proto": context_bus.PROTO, "status": "ok", "rows": int(rows),
                         "uptime_s": int(time.time() - cfg.started)})

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/put", "/v1/get", "/v1/delete", "/v1/list"):
            self._reject(404, "not found")
            return
        if self.headers.get("X-AF-Proto") != str(context_bus.PROTO):
            self._reject(400, f"unsupported proto (need X-AF-Proto: {context_bus.PROTO})")
            return
        # Bus clients never send Origin; a browser always does. With no token on a
        # TCP bind, also require a loopback Host so a DNS-rebinding page (same-origin
        # to the daemon under its own hostname) cannot drive the API.
        if self.headers.get("Origin") is not None:
            self._reject(403, "browser requests are refused (Origin header present)")
            return
        cfg = self.server.cfg  # type: ignore[attr-defined]
        if (
            cfg.token is None
            and isinstance(self.server, _TCPHTTPServer)
            and not _loopback_host(self.headers.get("Host") or "")
        ):
            self._reject(403, "tokenless bus only accepts a loopback Host header")
            return
        raw = self._read_body()
        if raw is None:
            return
        if not self._authorized(path, raw):
            return
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            self._reject(400, "body is not valid JSON")
            return
        if not isinstance(payload, dict):
            self._reject(400, "body must be a JSON object")
            return
        {"put": self._put, "get": self._get, "delete": self._delete,
         "list": self._list}[path[4:]](payload)

    def _put(self, payload: dict) -> None:
        cfg = self.server.cfg  # type: ignore[attr-defined]
        key = str(payload.get("key") or "").strip()
        envelope = str(payload.get("envelope") or "")
        if not key or not envelope:
            self._reject(400, "key and envelope are required")
            return
        try:
            _ver, _kid, head_writer, head_ts = context_crypto.envelope_meta(envelope)
        except context_crypto.CryptoError as exc:
            self._reject(400, f"malformed envelope: {exc}")
            return
        # Metadata MUST come from the AAD-bound header. The separate JSON fields
        # are a client convenience; a disagreement is a broken or forging client.
        if str(payload.get("writer") or "") != head_writer:
            self._reject(400, "writer does not match the sealed envelope header")
            return
        try:
            sent_ts = int(payload.get("ts_ns") or 0)
        except (TypeError, ValueError):
            sent_ts = -1
        if sent_ts != head_ts:
            self._reject(400, "ts_ns does not match the sealed envelope header")
            return
        # The daemon cannot open the envelope, so a peer can mint a header with any
        # ts. A far-future ts would become the key's replay floor and 409 every
        # honest write after it (and >= 2**63 overflows SQLite). Bound it to the same
        # skew window the floor already tolerates.
        if head_ts < 0 or head_ts > time.time_ns() + _FLOOR_SLACK_NS:
            self._reject(400, "envelope ts is in the future (clock skew or forgery)")
            return
        try:
            plaintext_bytes = max(0, int(payload.get("plaintext_bytes") or 0))
        except (TypeError, ValueError):
            plaintext_bytes = 0

        # Read-check-then-write was a lost-update TOCTOU: this is a ThreadingHTTPServer, so
        # two threads could both SELECT the same stored writer_ts, both pass the checks, and
        # the LATER committer win with the OLDER ts. Collapse it into ONE guarded upsert —
        # SQLite serializes the statement under the write lock, so the WHERE is evaluated
        # against the live row, never a stale read. The acceptance rule is unchanged: reject
        # a write more than the 60s skew slack below the stored ts (the SAME floor
        # context_hwm enforces on the read side — deliberately NOT strict; clocks across
        # machines are unsynced and the protocol bounds disorder to that window), and honor
        # If-Match as a compare-and-set folded INTO the write (checking it before the upsert
        # would let two racers both pass on the same stale read).
        if_match_raw = self.headers.get("If-Match-Writer-Ts")
        if_match_int: int | None = None
        if if_match_raw is not None:
            try:
                if_match_int = int(if_match_raw.strip())
            except (TypeError, ValueError):
                self._reject(409, "If-Match-Writer-Ts mismatch (unparseable)")
                return

        conn = _open_db(cfg.db_path)
        try:
            cur = conn.execute(
                "INSERT INTO bus_context "
                "(key, writer, writer_ts, received_ts, envelope, plaintext_bytes) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "writer=excluded.writer, writer_ts=excluded.writer_ts, "
                "received_ts=excluded.received_ts, envelope=excluded.envelope, "
                "plaintext_bytes=excluded.plaintext_bytes "
                "WHERE excluded.writer_ts >= COALESCE(bus_context.writer_ts, 0) - ? "
                "AND (? IS NULL OR COALESCE(bus_context.writer_ts, 0) = ?)",
                (key, head_writer, head_ts, time.time(), envelope, plaintext_bytes,
                 _FLOOR_SLACK_NS, if_match_int, if_match_int),
            )
            conn.commit()
            if cur.rowcount == 0:
                # The guard excluded the row: the stored writer_ts is newer (beyond slack)
                # or If-Match didn't match. Report the current ts so the client can re-seal
                # — a silent 200 would falsely claim its stale envelope is published.
                row = conn.execute(
                    "SELECT writer_ts FROM bus_context WHERE key = ?", (key,)
                ).fetchone()
                stored_ts = int(row[0] or 0) if row is not None else 0
                if if_match_int is not None and if_match_int != stored_ts:
                    self._reject(409, f"If-Match-Writer-Ts mismatch (current {stored_ts})")
                    return
                self._reject(409, "stale writer_ts (possible replay)")
                return
            cfg.puts += 1
            if cfg.puts % _SWEEP_EVERY == 0:
                _sweep(conn, cfg.max_rows)
        finally:
            conn.close()
        self._log("put", key, head_writer, 200)
        self._send(200, {"proto": context_bus.PROTO, "ok": True})

    def _get(self, payload: dict) -> None:
        cfg = self.server.cfg  # type: ignore[attr-defined]
        key = str(payload.get("key") or "").strip()
        conn = _open_db(cfg.db_path)
        try:
            row = conn.execute(
                "SELECT envelope, writer, writer_ts, received_ts, plaintext_bytes "
                "FROM bus_context WHERE key = ?",
                (key,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            self._send(200, {"proto": context_bus.PROTO, "found": False})
            return
        self._send(200, {
            "proto": context_bus.PROTO, "found": True, "key": key,
            "envelope": row[0], "writer": row[1], "ts_ns": int(row[2] or 0),
            "received_ts": float(row[3] or 0.0), "plaintext_bytes": int(row[4] or 0),
        })

    def _delete(self, payload: dict) -> None:
        cfg = self.server.cfg  # type: ignore[attr-defined]
        key = str(payload.get("key") or "").strip()
        conn = _open_db(cfg.db_path)
        try:
            cur = conn.execute("DELETE FROM bus_context WHERE key = ?", (key,))
            conn.commit()
            deleted = cur.rowcount > 0
        finally:
            conn.close()
        self._log("delete", key, "", 200)
        self._send(200, {"proto": context_bus.PROTO, "deleted": bool(deleted)})

    def _list(self, payload: dict) -> None:
        cfg = self.server.cfg  # type: ignore[attr-defined]
        if payload.get("bounded") is True:
            self._list_bounded(cfg)
            return
        # An older client (no "bounded" flag) cannot use a row without its envelope,
        # so it still gets every one.
        conn = _open_db(cfg.db_path)
        try:
            rows = conn.execute(
                "SELECT key, envelope, writer, writer_ts, received_ts, plaintext_bytes "
                "FROM bus_context ORDER BY received_ts DESC"
            ).fetchall()
        finally:
            conn.close()
        self._send(200, {"proto": context_bus.PROTO, "rows": [
            {"key": r[0], "envelope": r[1], "writer": r[2],
             "ts_ns": int(r[3] or 0), "received_ts": float(r[4] or 0.0),
             "plaintext_bytes": int(r[5] or 0)}
            for r in rows
        ]})

    def _list_bounded(self, cfg: ServerConfig) -> None:
        """Every row's metadata, plus its envelope (where the description is sealed)
        only while it is at most ``LIST_INLINE_MAX`` bytes and the newest-first running
        total stays within ``LIST_INLINE_BUDGET`` — so the listing fits the client's
        response cap however much the bus holds. Larger envelopes never leave SQLite."""
        budget = context_bus.LIST_INLINE_BUDGET
        rows: list[dict] = []
        conn = _open_db(cfg.db_path)
        try:
            for key, writer, writer_ts, received_ts, plaintext_bytes, envelope in conn.execute(
                "SELECT key, writer, writer_ts, received_ts, plaintext_bytes, "
                "CASE WHEN length(envelope) <= ? THEN envelope END "
                "FROM bus_context ORDER BY received_ts DESC",
                (context_bus.LIST_INLINE_MAX,),
            ):
                row = {"key": key, "writer": writer, "ts_ns": int(writer_ts or 0),
                       "received_ts": float(received_ts or 0.0),
                       "plaintext_bytes": int(plaintext_bytes or 0)}
                if envelope is not None and len(envelope) <= budget:
                    row["envelope"] = envelope
                    budget -= len(envelope)
                rows.append(row)
        finally:
            conn.close()
        self._send(200, {"proto": context_bus.PROTO, "rows": rows})


class _UnixHTTPServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = 0

    def verify_request(self, request, client_address) -> bool:
        uid = _peer_uid(request)
        return uid is None or uid == os.getuid()


class _TCPHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # Skip HTTPServer's getfqdn (a DNS lookup we do not need).
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


def build_server(
    cfg: ServerConfig,
    *,
    socket_path: Path | None = None,
    host: str | None = None,
    port: int = 0,
):
    """Bind (not serve) the daemon. Tests use this directly; ``serve`` adds the
    lock, logging, and the serve loop."""
    if socket_path is not None:
        _paths.ensure_dir_secure(socket_path.parent)
        server = _UnixHTTPServer(str(socket_path), _Handler)
        try:
            os.chmod(socket_path, 0o600)
        except OSError:
            pass
    else:
        server = _TCPHTTPServer((host or "127.0.0.1", port), _Handler)
    server.cfg = cfg  # type: ignore[attr-defined]
    return server


def _exit_on_sigterm(signum, _frame) -> None:
    # Ignore further signals first: a second SIGTERM arriving while server_close()
    # joins the handler threads raised SystemExit inside the shutdown `finally`,
    # skipping the socket unlink and the lock release.
    with contextlib.suppress(OSError, ValueError):
        signal.signal(signum, signal.SIG_IGN)
    raise SystemExit(0)


def _unlink_stale_socket(path: Path) -> None:
    """Remove a socket file left by a daemon that died without cleanup. The DB lock
    we hold is per-DB, not per-socket — a daemon serving ANOTHER db can own this
    path — so only a socket that refuses a connection is stale. A live one aborts
    (unlinking it would orphan that daemon and silently re-point its clients); a
    non-socket file is left alone (bind will fail loudly instead of deleting data)."""
    try:
        if not stat.S_ISSOCK(path.lstat().st_mode):
            return
    except OSError:
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        probe.connect(str(path))
    except ConnectionRefusedError:
        pass  # nobody listening: a leftover from a dead daemon
    except OSError:
        return  # can't tell (permissions, timeout): leave it, bind fails loudly
    else:
        raise SystemExit(f"context-busd: a live daemon is already listening on {path}")
    finally:
        probe.close()
    try:
        path.unlink()
    except OSError:
        pass


def serve(
    *,
    db_path: Path | None = None,
    socket_path: Path | None = None,
    host: str | None = None,
    port: int = 8788,
    token_file: str | None = None,
) -> None:
    """Run the daemon in the foreground. Default transport: a unix socket."""
    db = Path(db_path) if db_path is not None else default_db_path()
    use_socket: Path | None
    if socket_path is not None:
        use_socket = Path(socket_path)
    elif host is None:
        use_socket = default_socket_path()
    else:
        use_socket = None

    token: bytes | None = None
    tf = token_file or os.environ.get("ASK_FABLE_CONTEXT_BUS_TOKEN_FILE")
    if tf:
        token = _read_token_file(Path(tf))
    if use_socket is None and host not in ("127.0.0.1", "::1", "localhost") and token is None:
        raise SystemExit(
            "context-busd: refusing a non-loopback bind without --token-file "
            "(IP allowlists are hygiene, not a boundary)"
        )

    lock_fd = acquire_lock(db)
    if lock_fd is None:
        raise SystemExit(f"context-busd: another daemon already owns {db}")
    if use_socket is not None:
        _unlink_stale_socket(use_socket)
    cfg = ServerConfig(db, token=token)
    server = build_server(cfg, socket_path=use_socket, host=host, port=port)
    where = str(use_socket) if use_socket is not None else f"{host}:{server.server_port}"
    print(f"context-busd: db={db} listening on {where} "
          f"(token={'yes' if token else 'no'})", file=sys.stderr, flush=True)
    # systemctl stop/restart sends SIGTERM; turn it into SystemExit so the finally
    # below still removes the socket and releases the lock.
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if use_socket is not None:
            try:
                use_socket.unlink()
            except OSError:
                pass
        os.close(lock_fd)


def migrate(src: str, *, bus: str | None = None, token_file: str | None = None) -> dict:
    """Seal every plaintext row of a local ``context.db`` onto the bus. Rows that
    are already envelopes are skipped, so a re-run is safe. Runs in the client
    role — the source file is only read, never modified."""
    url = (bus or context_bus.bus_url()).strip()
    if not url:
        raise SystemExit("context-busd migrate: no bus configured (--bus or ASK_FABLE_CONTEXT_BUS)")
    token = (
        _read_token_file(Path(token_file))
        if token_file
        else (context_bus.load_token() if context_bus.token_path().exists() else None)
    )
    try:
        conn = sqlite3.connect(str(Path(src).expanduser()), timeout=5.0)
        try:
            rows = conn.execute("SELECT key, value, description FROM context").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise SystemExit(f"context-busd migrate: cannot read {src}: {exc}") from exc

    migrated = skipped = failed = 0
    writer = context_bus.machine_id()
    for key, value, description in rows:
        key = str(key or "").strip()
        value = str(value or "")
        if not key:
            continue
        if context_crypto.is_sealed(value):
            skipped += 1
            continue
        try:
            ts_ns = time.time_ns()
            envelope = context_crypto.seal(
                value, str(description or ""), key_name=key, writer=writer, ts_ns=ts_ns
            )
            context_bus.request(
                "POST", "/v1/put",
                {"key": key, "envelope": envelope, "writer": writer,
                 "ts_ns": ts_ns, "plaintext_bytes": len(value)},
                url=url, token=token,
            )
            migrated += 1
        except (context_crypto.CryptoError, context_bus.BusError) as exc:
            failed += 1
            print(f"context-busd migrate: '{key}': {exc}", file=sys.stderr)
    return {"total": len(rows), "migrated": migrated, "skipped": skipped, "failed": failed}


def _main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="context-busd",
        description="Owner daemon for the ask_fable LAN context bus (ciphertext only).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the daemon (foreground)")
    s.add_argument("--db", default=None, help="bus database path")
    s.add_argument("--socket", default=None, help="unix socket path (default)")
    s.add_argument("--host", default=None, help="TCP bind host (implies TCP)")
    s.add_argument("--port", type=int, default=8788, help="TCP port (default 8788)")
    s.add_argument("--token-file", default=None, help="bus request-auth token file")

    m = sub.add_parser("migrate", help="seal a local context.db onto the bus")
    m.add_argument("--from", dest="src", required=True, help="local context.db path")
    m.add_argument("--bus", default=None, help="bus URL (default: configured bus)")
    m.add_argument("--token-file", default=None, help="bus request-auth token file")

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        serve(
            db_path=Path(args.db) if args.db else None,
            socket_path=Path(args.socket) if args.socket else None,
            host=args.host,
            port=args.port,
            token_file=args.token_file,
        )
        return
    result = migrate(args.src, bus=args.bus, token_file=args.token_file)
    print(json.dumps(result))
    raise SystemExit(1 if result["failed"] else 0)


if __name__ == "__main__":
    _main()
