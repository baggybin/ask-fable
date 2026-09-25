"""LAN bus client: transport, request auth, metadata cross-check, fail-closed map."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ask_fable import context_bus, context_crypto, context_store

TOKEN = b"0123456789abcdef0123456789abcdef"
PSK = b"\x05" * 32


class _StubHandler(BaseHTTPRequestHandler):
    server_version = "stub/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence the default stderr chatter
        pass

    def _serve(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        self.server.calls.append({
            "method": self.command, "path": self.path, "body": body,
            "proto": self.headers.get("X-AF-Proto"),
            "auth": self.headers.get("X-AF-Auth"),
        })
        spec = self.server.responses.get(self.path)
        if callable(spec):
            status, payload = spec(self, body)
        elif isinstance(spec, dict):
            if spec.get("delay"):
                time.sleep(spec["delay"])
            status = int(spec.get("status", 200))
            payload = spec.get("payload", {"proto": context_bus.PROTO, "ok": True})
        else:
            status, payload = 404, {"proto": context_bus.PROTO, "error": "no route"}
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:  # client timed out and hung up
            pass

    do_POST = _serve
    do_GET = _serve


@pytest.fixture
def stub(tmp_path, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    srv.calls = []
    srv.responses = {}
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    kr = tmp_path / "kr"
    kr.write_text(f"1:{base64.b64encode(PSK).decode()}\n")
    os.chmod(kr, 0o600)
    monkeypatch.setenv("ASK_FABLE_CONTEXT_KEYRING", str(kr))
    monkeypatch.setenv("ASK_FABLE_CONTEXT_BUS", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("ASK_FABLE_CONTEXT_BUS_TOKEN_FILE", str(tmp_path / "no.token"))
    monkeypatch.delenv("ASK_FABLE_CONTEXT_MAX_BYTES", raising=False)
    monkeypatch.delenv("ASK_FABLE_CONTEXT_BUS_TIMEOUT", raising=False)
    context_store._reset_backend()
    context_store._clear_error()
    yield srv
    srv.shutdown()
    srv.server_close()
    context_store._reset_backend()


def _sealed(key="k", value="VALUE", writer="hostA", ts_ns=None):
    ts_ns = time.time_ns() if ts_ns is None else ts_ns
    envelope = context_crypto.seal(value, "desc", key_name=key, writer=writer, ts_ns=ts_ns)
    return envelope, ts_ns


# --- auth canonical string ---------------------------------------------------

def test_canonical_binds_method_path_and_body():
    a = context_bus.canonical("POST", "/v1/put", 1, "n", b"x")
    assert a != context_bus.canonical("POST", "/v1/get", 1, "n", b"x")
    assert a != context_bus.canonical("POST", "/v1/put", 1, "n", b"y")
    assert a != context_bus.canonical("POST", "/v1/put", 2, "n", b"x")


# --- put: sealing + auth header ---------------------------------------------

def test_put_seals_and_sends_signed_request(stub, tmp_path, monkeypatch):
    tokfile = tmp_path / "bus.token"
    tokfile.write_text(TOKEN.decode() + "\n")
    os.chmod(tokfile, 0o600)
    monkeypatch.setenv("ASK_FABLE_CONTEXT_BUS_TOKEN_FILE", str(tokfile))
    stub.responses["/v1/put"] = {"payload": {"proto": context_bus.PROTO, "ok": True}}

    assert context_store.put("repo:auth", "SECRET_CODE", "auth module") is True
    call = stub.calls[-1]
    sent = json.loads(call["body"])
    assert sent["key"] == "repo:auth"
    assert sent["envelope"].startswith("afctx1:")
    assert "SECRET_CODE" not in call["body"].decode()  # plaintext never leaves
    assert sent["plaintext_bytes"] == len("SECRET_CODE")
    assert call["proto"] == str(context_bus.PROTO)

    ts, nonce, sig = call["auth"].split(".")
    canon = context_bus.canonical("POST", "/v1/put", int(ts), nonce, call["body"])
    expected = hmac.new(TOKEN, canon.encode(), hashlib.sha256).hexdigest()
    assert hmac.compare_digest(expected, sig)

    _ver, _kid, head_writer, head_ts = context_crypto.envelope_meta(sent["envelope"])
    assert head_writer == sent["writer"] and head_ts == sent["ts_ns"]


# --- get: unseal + metadata cross-check -------------------------------------

def test_get_unseals_and_cross_checks_metadata(stub):
    envelope, ts_ns = _sealed("k", "VALUE")
    stub.responses["/v1/get"] = {"payload": {
        "proto": context_bus.PROTO, "found": True, "envelope": envelope,
        "writer": "hostA", "ts_ns": ts_ns, "received_ts": time.time()}}
    assert context_store.get("k") == "VALUE"


def test_get_rejects_daemon_metadata_mismatch(stub):
    envelope, ts_ns = _sealed("k", "VALUE")
    stub.responses["/v1/get"] = {"payload": {
        "proto": context_bus.PROTO, "found": True, "envelope": envelope,
        "writer": "liar", "ts_ns": ts_ns, "received_ts": 1.0}}
    context_store._clear_error()
    assert context_store.get("k") is None
    assert "metadata mismatch" in (context_store.last_error() or "")


# --- fail-closed mapping -----------------------------------------------------

def test_timeout_degrades(stub, monkeypatch):
    monkeypatch.setenv("ASK_FABLE_CONTEXT_BUS_TIMEOUT", "0.2")
    stub.responses["/v1/put"] = {"delay": 0.6, "payload": {"proto": context_bus.PROTO, "ok": True}}
    context_store._clear_error()
    assert context_store.put("k", "v") is False
    assert "unreachable" in (context_store.last_error() or "")


def test_401_degrades_naming_cause(stub):
    stub.responses["/v1/put"] = {"status": 401,
                                 "payload": {"proto": context_bus.PROTO, "error": "clock skew"}}
    context_store._clear_error()
    assert context_store.put("k", "v") is False
    assert "clock skew" in (context_store.last_error() or "")


def test_proto_mismatch_degrades(stub):
    stub.responses["/v1/put"] = {"payload": {"proto": 99, "ok": True}}
    context_store._clear_error()
    assert context_store.put("k", "v") is False
    assert "proto" in (context_store.last_error() or "")


def test_client_refuses_oversized_body(stub, monkeypatch):
    monkeypatch.setenv("ASK_FABLE_CONTEXT_MAX_BYTES", "64")
    context_store._clear_error()
    assert context_store.put("k", "x" * 500) is False
    assert "cap" in (context_store.last_error() or "")


@pytest.mark.parametrize("op", ["get", "get_meta", "get_versioned", "put", "delete", "entries"])
def test_one_bus_failure_does_not_leave_the_store_degraded(stub, op):
    # Only the local backend's connect cleared last_error(), so in bus mode one transient
    # failure stuck until restart: every later miss read as "degraded" (context_read ->
    # store_unavailable, context_ref -> store_degraded, ask_falsify -> ledger_unavailable).
    stub.responses["/v1/get"] = {"status": 503,
                                 "payload": {"proto": context_bus.PROTO, "error": "blip"}}
    assert context_store.get("k") is None
    assert "503" in (context_store.last_error() or "")
    ok = {"proto": context_bus.PROTO}
    stub.responses.update({
        "/v1/get": {"payload": {**ok, "found": False}},
        "/v1/put": {"payload": {**ok, "ok": True}},
        "/v1/delete": {"payload": {**ok, "deleted": False}},
        "/v1/list": {"payload": {**ok, "rows": []}},
    })
    {
        "get": lambda: context_store.get("missing"),
        "get_meta": lambda: context_store.get_meta("missing"),
        "get_versioned": lambda: context_store.get_versioned("missing"),
        "put": lambda: context_store.put("k", "v"),
        "delete": lambda: context_store.delete("missing"),
        "entries": context_store.entries,
    }[op]()
    assert context_store.last_error() is None  # a clean op reports a healthy store again


# --- status + unix transport -------------------------------------------------

def test_bus_status(stub):
    stub.responses["/health"] = {"payload": {"proto": context_bus.PROTO, "status": "ok", "rows": 3}}
    st = context_bus.bus_status()
    assert st["configured"] and st["reachable"] and st["proto"] == context_bus.PROTO
    assert st["keyring_kids"] == 1 and st["rows"] == 3


class _UnixStubServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = 0


def test_unix_socket_transport(tmp_path, monkeypatch):
    sock = tmp_path / "bus.sock"
    srv = _UnixStubServer(str(sock), _StubHandler)
    srv.calls = []
    srv.responses = {"/v1/put": {"payload": {"proto": context_bus.PROTO, "ok": True}}}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    kr = tmp_path / "kr"
    kr.write_text(f"1:{base64.b64encode(PSK).decode()}\n")
    os.chmod(kr, 0o600)
    monkeypatch.setenv("ASK_FABLE_CONTEXT_KEYRING", str(kr))
    monkeypatch.setenv("ASK_FABLE_CONTEXT_BUS", f"unix://{sock}")
    monkeypatch.setenv("ASK_FABLE_CONTEXT_BUS_TOKEN_FILE", str(tmp_path / "no.token"))
    context_store._reset_backend()
    context_store._clear_error()
    try:
        assert context_store.put("k", "v") is True
        assert srv.calls and srv.calls[-1]["path"] == "/v1/put"
    finally:
        srv.shutdown()
        srv.server_close()
        context_store._reset_backend()


# --- composite writer attribution (machine[/client[/model]]) -----------------


@pytest.fixture
def clean_writer_client():
    """Isolate the process-wide writer-client ContextVar so a composed client
    never leaks into another test (the server resets it every real call)."""
    context_bus.set_writer_client("")
    try:
        yield
    finally:
        context_bus.set_writer_client("")


@pytest.mark.usefixtures("clean_writer_client")
def test_writer_id_bare_machine_when_client_unset(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_MACHINE_ID", "boxA")
    monkeypatch.delenv("ASK_FABLE_MODEL_ID", raising=False)
    context_bus.set_writer_client("")
    assert context_bus.writer_id() == "boxA"


@pytest.mark.usefixtures("clean_writer_client")
def test_writer_id_appends_client(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_MACHINE_ID", "boxA")
    monkeypatch.delenv("ASK_FABLE_MODEL_ID", raising=False)
    context_bus.set_writer_client("claude-code")
    assert context_bus.writer_id() == "boxA/claude-code"


@pytest.mark.usefixtures("clean_writer_client")
def test_writer_id_omits_unknown_client(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_MACHINE_ID", "boxA")
    context_bus.set_writer_client("unknown")
    assert context_bus.writer_id() == "boxA"


@pytest.mark.usefixtures("clean_writer_client")
def test_writer_id_appends_model_only_when_client_present(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_MACHINE_ID", "boxA")
    monkeypatch.setenv("ASK_FABLE_MODEL_ID", "opus-4.8")
    context_bus.set_writer_client("opencode")
    assert context_bus.writer_id() == "boxA/opencode/opus-4.8"
    # No client -> model is not appended (positions never shift).
    context_bus.set_writer_client("")
    assert context_bus.writer_id() == "boxA"


@pytest.mark.usefixtures("clean_writer_client")
def test_writer_id_sanitizes_delimiter_and_whitespace(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_MACHINE_ID", "boxA")
    monkeypatch.delenv("ASK_FABLE_MODEL_ID", raising=False)
    context_bus.set_writer_client("Claude Code/beta")
    # spaces and the '/' delimiter collapse to '_' so the split stays 2-element
    wid = context_bus.writer_id()
    assert wid == "boxA/Claude_Code_beta"
    assert wid.split("/") == ["boxA", "Claude_Code_beta"]


@pytest.mark.usefixtures("clean_writer_client")
def test_writer_id_sanitizes_machine_component(monkeypatch):
    # A '/' in the machine id must not forge extra fields: the split stays
    # 2-element (machine, client), not 3 with a misattributed client.
    monkeypatch.setenv("ASK_FABLE_MACHINE_ID", "team/box")
    monkeypatch.delenv("ASK_FABLE_MODEL_ID", raising=False)
    context_bus.set_writer_client("claude-code")
    wid = context_bus.writer_id()
    assert wid == "team_box/claude-code"
    assert wid.split("/") == ["team_box", "claude-code"]
