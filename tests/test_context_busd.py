"""Owner daemon end to end: auth, metadata trust, replay floor, migration, lifecycle."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from ask_fable import context_bus, context_busd, context_crypto, context_store

TOKEN = b"0123456789abcdef0123456789abcdef"
PSK = b"\x09" * 32


@pytest.fixture
def busd(tmp_path, monkeypatch):
    db = tmp_path / "bus.db"
    token_file = tmp_path / "bus.token"
    token_file.write_text(TOKEN.decode() + "\n")
    os.chmod(token_file, 0o600)
    kr = tmp_path / "kr"
    kr.write_text(f"1:{base64.b64encode(PSK).decode()}\n")
    os.chmod(kr, 0o600)

    cfg = context_busd.ServerConfig(db, token=TOKEN)
    server = context_busd.build_server(cfg, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("ASK_FABLE_CONTEXT_BUS", url)
    monkeypatch.setenv("ASK_FABLE_CONTEXT_BUS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("ASK_FABLE_CONTEXT_KEYRING", str(kr))
    context_store._reset_backend()
    context_store._clear_error()
    yield SimpleNamespace(server=server, db=db, token_file=token_file, kr=kr,
                          cfg=cfg, url=url, token=TOKEN)
    server.shutdown()
    server.server_close()
    context_store._reset_backend()


def _raw_post(url, path, payload, token, *, ts=None, nonce="n1", sig=None, proto="1"):
    body = json.dumps(payload).encode()
    ts = int(time.time()) if ts is None else ts
    canon = context_bus.canonical("POST", path, ts, nonce, body)
    sig = sig or hmac.new(token, canon.encode(), hashlib.sha256).hexdigest()
    host, port = url[len("http://"):].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    conn.request("POST", path, body=body, headers={
        "Content-Type": "application/json", "X-AF-Proto": proto,
        "X-AF-Auth": f"{ts}.{nonce}.{sig}"})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, json.loads(data)


# --- roundtrip + ciphertext-only storage ------------------------------------

def test_roundtrip_through_store_facade(busd):
    assert context_store.put("repo:auth", "SECRET_CODE", "auth module") is True
    assert context_store.get("repo:auth") == "SECRET_CODE"
    meta = context_store.get_meta("repo:auth")
    assert meta is not None and meta[0] == "SECRET_CODE" and meta[2] == "auth module"
    ents = {e["key"]: e for e in context_store.entries()}
    assert ents["repo:auth"]["description"] == "auth module"
    assert "unreadable" not in ents["repo:auth"]

    conn = sqlite3.connect(str(busd.db))
    try:
        raw = conn.execute(
            "SELECT envelope, writer FROM bus_context WHERE key = ?", ("repo:auth",)
        ).fetchone()
    finally:
        conn.close()
    assert raw is not None and raw[0].startswith("afctx1:")
    assert "SECRET_CODE" not in raw[0]

    assert context_store.delete("repo:auth") is True
    assert context_store.get("repo:auth") is None


# --- metadata comes from the sealed header, not the request ------------------

def test_daemon_rejects_metadata_mismatch(busd):
    ts_ns = time.time_ns()
    envelope = context_crypto.seal("V", "", key_name="k", writer="hostA", ts_ns=ts_ns)
    with pytest.raises(context_bus.BusError) as ei:
        context_bus.request("POST", "/v1/put", {
            "key": "k", "envelope": envelope, "writer": "liar",
            "ts_ns": ts_ns, "plaintext_bytes": 1})
    assert "writer does not match" in str(ei.value)


def test_writer_ts_from_header_not_request(busd):
    # Correct writer, doctored ts in the JSON claim -> rejected, because the
    # daemon trusts the AAD-bound header (the replay floor's input).
    ts_ns = time.time_ns()
    envelope = context_crypto.seal("V", "", key_name="k", writer="hostA", ts_ns=ts_ns)
    with pytest.raises(context_bus.BusError) as ei:
        context_bus.request("POST", "/v1/put", {
            "key": "k", "envelope": envelope, "writer": "hostA",
            "ts_ns": ts_ns + 1, "plaintext_bytes": 1})
    assert "ts_ns does not match" in str(ei.value)


# --- request auth ------------------------------------------------------------

def test_replay_rejected(busd):
    payload = {"key": "nope"}
    s1, _ = _raw_post(busd.url, "/v1/get", payload, busd.token, nonce="fixed-nonce")
    s2, body2 = _raw_post(busd.url, "/v1/get", payload, busd.token, nonce="fixed-nonce")
    assert s1 == 200
    assert s2 == 401 and "replay" in body2["error"]


def test_clock_skew_rejected(busd):
    status, body = _raw_post(busd.url, "/v1/get", {"key": "x"}, busd.token,
                             ts=int(time.time()) - 3600)
    assert status == 401 and "clock skew" in body["error"]


def test_bad_signature_rejected(busd):
    status, body = _raw_post(busd.url, "/v1/get", {"key": "x"},
                             b"wrong-token-wrong-token!", nonce="n2")
    assert status == 401 and "bad signature" in body["error"]


def test_proto_mismatch_rejected(busd):
    status, body = _raw_post(busd.url, "/v1/get", {"key": "x"}, busd.token,
                             nonce="n3", proto="2")
    assert status == 400 and "proto" in body["error"]


# --- persistent anti-replay floor -------------------------------------------

def test_stale_writer_ts_rejected(busd):
    assert context_store.put("k", "new") is True
    old_ts = time.time_ns() - 120 * 1_000_000_000
    writer = context_bus.machine_id()
    envelope = context_crypto.seal("old", "", key_name="k", writer=writer, ts_ns=old_ts)
    with pytest.raises(context_bus.BusRejected) as ei:
        context_bus.request("POST", "/v1/put", {
            "key": "k", "envelope": envelope, "writer": writer,
            "ts_ns": old_ts, "plaintext_bytes": 3})
    assert "stale writer_ts" in str(ei.value)
    assert context_store.get("k") == "new"  # the stale write did not land


def test_within_slack_write_is_accepted(busd):
    # RC-3: the 60s skew slack is deliberate (context_hwm mirrors the same window on the
    # read side), so an in-slack OLDER write must still land — a strict monotonic guard
    # would wrongly reject it. This pins that the atomic-upsert rewrite kept the slack.
    assert context_store.put("k", "first") is True  # seals at ~now
    writer = context_bus.machine_id()
    within = time.time_ns() - 30 * 1_000_000_000  # 30s older, inside the 60s slack
    envelope = context_crypto.seal("second", "", key_name="k", writer=writer, ts_ns=within)
    context_bus.request("POST", "/v1/put", {
        "key": "k", "envelope": envelope, "writer": writer,
        "ts_ns": within, "plaintext_bytes": 6})
    assert context_store.get("k") == "second"  # in-slack write landed (last-writer-wins)


def test_concurrent_puts_no_lost_update(busd):
    # RC-3: the guarded upsert is one atomic statement, so even with the daemon's threads
    # racing, a stale (beyond-slack) write can't clobber a newer one. Timestamps are spaced
    # well beyond the slack, so the newest MUST survive regardless of commit order — the old
    # read-check-then-write could let a late lower-ts writer win.
    writer = context_bus.machine_id()
    step = 120 * 1_000_000_000  # 120s apart >> 60s slack
    n = 8
    # All in the past: the daemon refuses envelope timestamps beyond now + slack.
    base = time.time_ns() - n * step

    def put(i: int) -> None:
        ts = base + i * step
        env = context_crypto.seal(f"v{i}", "", key_name="race", writer=writer, ts_ns=ts)
        try:
            context_bus.request("POST", "/v1/put", {
                "key": "race", "envelope": env, "writer": writer,
                "ts_ns": ts, "plaintext_bytes": 2})
        except context_bus.BusRejected:
            pass  # an older write racing in after a newer one is correctly refused

    threads = [threading.Thread(target=put, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert context_store.get("race") == f"v{n - 1}"  # newest survived; no rollback


# --- health, sweep, lock -----------------------------------------------------

def test_health_unauthenticated(busd):
    h = context_bus.request("GET", "/health", None, authenticated=False)
    assert h["proto"] == context_bus.PROTO and h["status"] == "ok"
    assert isinstance(h["rows"], int) and h["rows"] >= 0


def test_sweep_is_per_writer(busd):
    conn = context_busd._open_db(busd.db)
    now = time.time()
    rows = [("a1", "A", 1, now, "afctx1:x", 1), ("a2", "A", 2, now, "afctx1:x", 1),
            ("a3", "A", 3, now, "afctx1:x", 1), ("b1", "B", 1, now, "afctx1:x", 1)]
    conn.executemany("INSERT INTO bus_context VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    context_busd._sweep(conn, 2)
    a = conn.execute("SELECT COUNT(*) FROM bus_context WHERE writer='A'").fetchone()[0]
    b = conn.execute("SELECT COUNT(*) FROM bus_context WHERE writer='B'").fetchone()[0]
    conn.close()
    assert a < 3 and b == 1  # A was trimmed to the cap; B untouched


def test_second_instance_lock_refused(tmp_path):
    db = tmp_path / "b.db"
    fd = context_busd.acquire_lock(db)
    assert fd is not None
    assert context_busd.acquire_lock(db) is None
    os.close(fd)
    fd2 = context_busd.acquire_lock(db)
    assert fd2 is not None
    os.close(fd2)


def test_daemon_rejects_oversized_body(tmp_path):
    cfg = context_busd.ServerConfig(tmp_path / "b.db", token=None, max_bytes=64)
    server = context_busd.build_server(cfg, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("POST", "/v1/put", body=b"x" * 500, headers={
            "Content-Type": "application/json", "X-AF-Proto": "1"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert resp.status == 413 and "too large" in data["error"]
    finally:
        server.shutdown()
        server.server_close()


# --- migration ---------------------------------------------------------------

def test_migrate_seals_local_rows(busd, tmp_path):
    src = tmp_path / "context.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE context (key TEXT PRIMARY KEY, value TEXT, ts REAL, description TEXT)")
    conn.execute("INSERT INTO context VALUES ('m1', 'PLAIN_ONE', 1.0, 'first')")
    conn.execute("INSERT INTO context VALUES ('m2', 'PLAIN_TWO', 2.0, 'second')")
    conn.commit()
    conn.close()

    res = context_busd.migrate(str(src), bus=busd.url, token_file=str(busd.token_file))
    assert res == {"total": 2, "migrated": 2, "skipped": 0, "failed": 0}
    assert context_store.get("m1") == "PLAIN_ONE"
    assert context_store.get("m2") == "PLAIN_TWO"
    conn = sqlite3.connect(str(busd.db))
    try:
        rows = conn.execute("SELECT envelope FROM bus_context").fetchall()
    finally:
        conn.close()
    assert rows and all(r[0].startswith("afctx1:") for r in rows)

    # A source row that is already an envelope is skipped (re-run safety).
    armored = context_crypto.seal("X", "", key_name="m3", writer="w", ts_ns=time.time_ns())
    conn = sqlite3.connect(str(src))
    conn.execute("INSERT INTO context VALUES ('m3', ?, 3.0, '')", (armored,))
    conn.commit()
    conn.close()
    res2 = context_busd.migrate(str(src), bus=busd.url, token_file=str(busd.token_file))
    assert res2["skipped"] == 1 and res2["failed"] == 0


# --- unix socket transport ---------------------------------------------------

def test_unix_socket_roundtrip(tmp_path, monkeypatch):
    db = tmp_path / "b.db"
    sock = tmp_path / "b.sock"
    kr = tmp_path / "kr"
    kr.write_text(f"1:{base64.b64encode(PSK).decode()}\n")
    os.chmod(kr, 0o600)
    cfg = context_busd.ServerConfig(db, token=None)
    server = context_busd.build_server(cfg, socket_path=sock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ASK_FABLE_CONTEXT_BUS", f"unix://{sock}")
    monkeypatch.setenv("ASK_FABLE_CONTEXT_BUS_TOKEN_FILE", str(tmp_path / "no.token"))
    monkeypatch.setenv("ASK_FABLE_CONTEXT_KEYRING", str(kr))
    context_store._reset_backend()
    context_store._clear_error()
    try:
        assert context_store.put("u", "UNIX_VALUE") is True
        assert context_store.get("u") == "UNIX_VALUE"
    finally:
        server.shutdown()
        server.server_close()
        context_store._reset_backend()


# --- reader-side rollback guard / owner-trust hardening ----------------------

def test_reader_refuses_rolled_back_blob(busd):
    assert context_store.put("rb:key", "CURRENT", "rollback test") is True
    old_ts = time.time_ns() - 600 * 1_000_000_000
    writer = context_bus.machine_id()
    stale = context_crypto.seal("STALE", "", key_name="rb:key", writer=writer, ts_ns=old_ts)
    conn = sqlite3.connect(str(busd.db))
    try:
        conn.execute(
            "UPDATE bus_context SET envelope = ?, writer_ts = ? WHERE key = ?",
            (stale, old_ts, "rb:key"),
        )
        conn.commit()
    finally:
        conn.close()
    context_store._clear_error()
    assert context_store.get("rb:key") is None  # stale-but-valid envelope refused
    err = context_store.last_error() or ""
    assert "rollback guard" in err
    context_store.delete("rb:key")


def test_bus_read_refuses_unsealed_value(busd):
    assert context_store.put("plain:key", "V", "plain test") is True
    conn = sqlite3.connect(str(busd.db))
    try:
        conn.execute("UPDATE bus_context SET envelope = 'NOT_SEALED' WHERE key = ?", ("plain:key",))
        conn.commit()
    finally:
        conn.close()
    context_store._clear_error()
    assert context_store.get("plain:key") is None  # the untrusted owner cannot opt out
    assert "envelope" in (context_store.last_error() or "")
    context_store.delete("plain:key")


# --- bug hunt 2026-09-25: unauthenticated-peer hardening ----------------------

def _tokenless_server(tmp_path):
    cfg = context_busd.ServerConfig(tmp_path / "open.db", token=None)
    server = context_busd.build_server(cfg, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _post_with_headers(port, path, payload, extra):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json", "X-AF-Proto": "1", **extra}
    conn.request("POST", path, body=json.dumps(payload).encode(), headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def test_far_future_envelope_ts_cannot_poison_the_floor(busd):
    # The daemon can't open envelopes, so a forged header ts of 2**63-1 used to become
    # the key's replay floor and 409 every honest write afterwards.
    writer = context_bus.machine_id()
    for bad_ts in (2**63 - 1, time.time_ns() + 3600 * 1_000_000_000):
        envelope = context_crypto.seal("junk", "", key_name="k", writer=writer, ts_ns=bad_ts)
        with pytest.raises(context_bus.BusError) as ei:
            context_bus.request("POST", "/v1/put", {
                "key": "k", "envelope": envelope, "writer": writer,
                "ts_ns": bad_ts, "plaintext_bytes": 4})
        assert "future" in str(ei.value)
    assert context_store.put("k", "honest") is True
    assert context_store.get("k") == "honest"


def test_tokenless_tcp_refuses_rebinding_host_and_browser_origin(tmp_path):
    server = _tokenless_server(tmp_path)
    try:
        port = server.server_port
        status, body = _post_with_headers(port, "/v1/get", {"key": "x"}, {"Host": "evil.example"})
        assert status == 403 and "loopback Host" in body["error"]
        status, body = _post_with_headers(
            port, "/v1/get", {"key": "x"}, {"Origin": "http://evil.example"})
        assert status == 403 and "Origin" in body["error"]
        for ok_host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
            status, _ = _post_with_headers(port, "/v1/get", {"key": "x"}, {"Host": ok_host})
            assert status == 200, ok_host
    finally:
        server.shutdown()
        server.server_close()


def test_handler_has_a_socket_timeout():
    # A peer that announces a body and never sends it must not park a thread forever.
    assert context_busd._Handler.timeout and context_busd._Handler.timeout <= 60


def test_listing_survives_a_bus_larger_than_the_response_cap(busd, monkeypatch):
    # /v1/list returned every full envelope, so once the bus outgrew the client's response
    # cap listing failed for good (did-you-mean suggestions with it). Scaled down here.
    monkeypatch.setattr(context_bus, "_MAX_RESPONSE", 256 * 1024)
    monkeypatch.setattr(context_bus, "LIST_INLINE_MAX", 32 * 1024, raising=False)
    monkeypatch.setattr(context_bus, "LIST_INLINE_BUDGET", 64 * 1024, raising=False)
    for i in range(6):
        assert context_store.put(f"big{i}", "x" * 60_000, f"big blob {i}") is True
    assert context_store.put("small", "tiny", "small blob") is True

    ents = {e["key"]: e for e in context_store.entries()}
    assert context_store.last_error() is None
    assert set(ents) == {"small", *(f"big{i}" for i in range(6))}
    # A small row is still opened for its sealed description...
    assert ents["small"]["description"] == "small blob" and ents["small"]["bytes"] == 4
    assert "description_omitted" not in ents["small"]
    # ...a large one lists as metadata, never shipping its blob.
    assert ents["big0"]["bytes"] == 60_000 and ents["big0"]["description_omitted"] is True


def test_listing_without_the_bounded_flag_still_carries_every_envelope(busd, monkeypatch):
    # An older client cannot use a row without its envelope, so it keeps the full shape.
    monkeypatch.setattr(context_bus, "LIST_INLINE_MAX", 1024)
    assert context_store.put("a", "y" * 4000, "large") is True
    assert context_store.put("b", "z", "small") is True
    legacy = context_bus.request("POST", "/v1/list", {})["rows"]
    assert {r["key"] for r in legacy} == {"a", "b"}
    assert all(r["envelope"].startswith("afctx1:") for r in legacy)
    bounded = {r["key"]: r for r in
               context_bus.request("POST", "/v1/list", {"bounded": True})["rows"]}
    assert "envelope" not in bounded["a"] and bounded["a"]["plaintext_bytes"] == 4000
    assert bounded["b"]["envelope"].startswith("afctx1:")


def test_stale_socket_file_is_removed_but_regular_files_are_not(tmp_path):
    import socket as _socket

    sock_path = tmp_path / "busd.sock"
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.bind(str(sock_path))
    s.close()  # left behind, like a SIGTERM'd daemon without cleanup
    context_busd._unlink_stale_socket(sock_path)
    assert not sock_path.exists()
    regular = tmp_path / "not-a-socket"
    regular.write_text("data")
    context_busd._unlink_stale_socket(regular)
    assert regular.read_text() == "data"


def test_a_live_daemons_socket_is_never_unlinked(tmp_path):
    """The DB lock is per-DB: a second daemon on another db with the same socket
    must not delete the first one's live socket and steal its clients."""
    import socket as _socket

    sock_path = tmp_path / "busd.sock"
    live = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    live.bind(str(sock_path))
    live.listen(1)
    try:
        with pytest.raises(SystemExit, match="already listening"):
            context_busd._unlink_stale_socket(sock_path)
        assert sock_path.exists()
    finally:
        live.close()
