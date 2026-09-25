"""ask_fable code index — chunking, incremental indexing, hybrid search, fail-safe."""

from __future__ import annotations

import pytest

import ask_fable.codeindex as ci


def _vector(text: str) -> list[float]:
    low = text.lower()
    return [
        1.0 if "alpha" in low else 0.0,
        1.0 if "beta" in low else 0.0,
        1.0 if "gamma" in low else 0.0,
        0.25,
    ]


def _fake_post(*, embeddings: bool = True, chat_order=None):
    calls = {"embeddings": 0, "chat": 0}

    def post(url, payload, timeout):
        if url.endswith("/v1/embeddings"):
            calls["embeddings"] += 1
            if not embeddings:
                return None, "network error: connection refused"
            return {"data": [{"embedding": _vector(t)} for t in payload["input"]]}, None
        if url.endswith("/v1/chat/completions"):
            calls["chat"] += 1
            if chat_order is None:
                return None, "network error: connection refused"
            body = "[" + ",".join(str(i) for i in chat_order) + "]"
            return {"choices": [{"message": {"content": body}}]}, None
        raise AssertionError(f"unexpected url {url}")

    return post, calls


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ASK_FABLE_EMBED_HOSTS", "http://fake:1234")
    monkeypatch.delenv("ASK_FABLE_EMBED_MODEL", raising=False)
    monkeypatch.delenv("ASK_FABLE_EMBED_RERANK_MODEL", raising=False)
    monkeypatch.delenv("ASK_FABLE_EMBED_API_KEY", raising=False)
    monkeypatch.delenv("ASK_FABLE_LMSTUDIO_API_KEY", raising=False)


@pytest.fixture()
def proj(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src" / "alpha.py").write_text(
        "\n".join(f"def alpha_handler_{i}(): return alpha_value_{i}" for i in range(120)) + "\n"
    )
    (root / "src" / "beta.py").write_text(
        "\n".join(f"def beta_worker_{i}(): pass" for i in range(30)) + "\n"
    )
    (root / "docs" / "notes.md").write_text("# Notes\ngamma channel documentation\n" * 5)
    (root / ".env").write_text("API_KEY=sekrit\n")
    (root / "bin").mkdir()
    (root / "bin" / "blob.dat").write_bytes(b"\x00\x01\x02binary junk")
    return root


def test_index_counts_and_db_outside_the_repo(monkeypatch, proj):
    monkeypatch.setattr(ci, "_post", _fake_post()[0])
    res = ci.index(str(proj))
    assert res["status"] == "ok"
    assert res["files"]["indexed"] == 3  # alpha.py, beta.py, notes.md
    assert res["files"]["refused"] >= 2  # .env (blocked) + blob.dat (binary)
    assert res["chunks"] > 0
    assert res["embedded"] == res["chunks"] and res["pending"] == 0
    assert res["mode"] == "semantic"
    db = ci.status(str(proj))["db"]
    assert str(proj) not in db  # state dir, never inside the repo


def test_iter_files_refuses_symlinks(tmp_path):
    # TS-1: a symlink inside the root must not be followed and read — it could point at a
    # secret OUTSIDE the tree (e.g. keys.txt -> ~/.ssh/id_rsa), leaking it into the index.
    root = tmp_path / "repo"
    root.mkdir()
    (root / "real.py").write_text("def real(): pass\n")
    secret = tmp_path / "outside_secret.txt"
    secret.write_text("PRIVATE KEY MATERIAL\n")
    (root / "keys.txt").symlink_to(secret)
    found, refused = ci._iter_files(str(root))
    assert "real.py" in found
    assert "keys.txt" not in found  # the symlink was refused, not indexed
    assert refused >= 1


def test_index_and_search_honour_pack_blocklist(monkeypatch, tmp_path, proj):
    # The index must apply the operator's pack_blocklist exactly as context_pack does:
    # a blocklisted file is never chunked/embedded, and a stale index built before
    # the path was blocklisted never serves its chunks.
    import json

    (proj / "secrets.yaml").write_text("prod_db_password: Sup3rS3cretValue\n")
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("ASK_FABLE_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(ci, "_post", _fake_post(embeddings=False)[0])

    ci.index(str(proj))  # no blocklist yet: the file is indexed
    hits = ci.search(str(proj), "prod_db_password")["hits"]
    assert any(h["path"] == "secrets.yaml" for h in hits)

    cfg.write_text(json.dumps({"pack_blocklist": ["secrets.yaml"]}))
    # stale index, before any re-index: search already refuses the blocked path
    stale = ci.search(str(proj), "prod_db_password")
    assert all(h["path"] != "secrets.yaml" for h in stale.get("hits", []))
    # re-index drops it from the index entirely
    ci.index(str(proj))
    found, _refused = ci._iter_files(str(proj), ci._pack_blocklist())
    assert "secrets.yaml" not in found
    res = ci.search(str(proj), "prod_db_password")
    assert all(h["path"] != "secrets.yaml" for h in res.get("hits", []))
    assert "Sup3rS3cretValue" not in json.dumps(res)

def test_index_degrades_on_vector_count_mismatch(monkeypatch, proj):
    # ED-3: an embed host that returns ok but the WRONG vector count must not crash the
    # strict zip (uncaught ValueError) — degrade to keyword search, the module's fail-safe.
    monkeypatch.setattr(
        ci, "embed_texts",
        lambda texts: {"ok": True, "vectors": [[0.0, 0.0, 0.0, 0.25]], "host": "fake"},
    )
    res = ci.index(str(proj))
    assert res["status"] == "ok"  # did not raise
    assert res["mode"] == "keyword"
    assert "vectors" in res["degraded"]


def test_keyword_fallback_when_no_host_answers(monkeypatch, proj):
    monkeypatch.setattr(ci, "_post", _fake_post(embeddings=False)[0])
    res = ci.index(str(proj))
    assert res["mode"] == "keyword" and res["pending"] == res["chunks"]
    assert "no embed host answered" in res["degraded"]

    out = ci.search(str(proj), "alpha_handler_7")
    assert out["status"] == "ok" and out["mode"] == "keyword"
    assert out["degraded"]
    assert out["hits"] and out["hits"][0]["path"] == "src/alpha.py"

    # blocked and binary files never make it into the index at all
    assert ci.search(str(proj), "sekrit")["hits"] == []


def test_semantic_search_prefers_the_matching_file(monkeypatch, proj):
    monkeypatch.setattr(ci, "_post", _fake_post()[0])
    ci.index(str(proj))
    out = ci.search(str(proj), "alpha", k=3)
    assert out["mode"] == "semantic+keyword"
    assert out["hits"][0]["path"] == "src/alpha.py"
    assert "semantic" in out["hits"][0]["why"]


def test_incremental_reindex_keeps_embeddings_for_unchanged_files(monkeypatch, proj):
    monkeypatch.setattr(ci, "_post", _fake_post()[0])
    first = ci.index(str(proj))
    assert first["embedded"] == first["chunks"]

    again = ci.index(str(proj))
    assert again["files"]["unchanged"] == 3 and again["files"]["added"] == 0
    assert again["embedded"] == 0 and again["pending"] == 0

    (proj / "src" / "beta.py").write_text("def beta_worker(): return 'beta prime'\n")
    third = ci.index(str(proj))
    assert third["files"]["changed"] == 1 and third["files"]["unchanged"] == 2
    assert 0 < third["embedded"] < third["chunks"]


def test_removed_files_leave_the_index(monkeypatch, proj):
    monkeypatch.setattr(ci, "_post", _fake_post()[0])
    ci.index(str(proj))
    (proj / "src" / "beta.py").unlink()
    res = ci.index(str(proj))
    assert res["files"]["removed"] == 1
    out = ci.search(str(proj), "beta_worker")
    assert all(h["path"] != "src/beta.py" for h in out["hits"])


def test_search_before_index_is_a_typed_error(tmp_path):
    out = ci.search(str(tmp_path), "anything")
    assert out["status"] == "error" and out["kind"] == "not_indexed"


def test_rerank_is_opt_in_and_degrades(monkeypatch, proj):
    post, calls = _fake_post(chat_order=[2, 1, 0])
    monkeypatch.setattr(ci, "_post", post)
    ci.index(str(proj))

    monkeypatch.setenv("ASK_FABLE_EMBED_RERANK_MODEL", "small-model")
    out = ci.search(str(proj), "alpha", k=3, rerank=True)
    assert out["rerank"] == "applied" and calls["chat"] == 1

    post_down, _ = _fake_post(chat_order=None)
    monkeypatch.setattr(ci, "_post", post_down)
    down = ci.search(str(proj), "alpha", k=3, rerank=True)
    assert down["rerank"].startswith("unavailable") and down["hits"]

    monkeypatch.delenv("ASK_FABLE_EMBED_RERANK_MODEL")
    unset = ci.search(str(proj), "alpha", k=3, rerank=True)
    assert unset["rerank"].startswith("skipped")


def test_chunks_overlap_on_window_boundaries():
    chunks = ci._chunks_for("\n".join(f"line {i}" for i in range(200)))
    assert len(chunks) >= 3
    assert chunks[0][0] == 1 and chunks[0][1] == ci.CHUNK_LINES
    assert chunks[1][0] == ci.CHUNK_LINES - ci.CHUNK_OVERLAP + 1


def test_chunk_lines_are_the_context_pack_range(tmp_path):
    # Chunks were numbered by str.splitlines(), which also breaks on \f, \v, a lone \r,
    # \x85, \u2028 ...; context_pack counts only "\n", so a hit's file:start-end pointed
    # at other lines. Each chunk must be exactly the text of the range it cites.
    from ask_fable import safe_fs

    src = "".join(
        ("\f\n" if i % 10 == 0 else "") + f"line_{i} = {i}  # a\x0bb\rc\x85d\u2028e\r\n"
        for i in range(1, 201)
    )
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mod.py").write_bytes(src.encode("utf-8"))
    chunks = ci._chunks_for(src)
    assert chunks[-1][1] == src.count("\n")
    for start, end, body in chunks:
        r = safe_fs.resolve_within(str(root), f"mod.py:{start}-{end}")
        text, _ = safe_fs.read_content(r, max_bytes=10**6)
        assert text.replace("\r\n", "\n").strip() == body


def test_index_built_with_splitlines_numbering_is_rechunked_once(monkeypatch, proj):
    # An incremental index skips unchanged files, so a file an older version chunked
    # with drifted numbers must be re-chunked once rather than kept as it was.
    import hashlib

    monkeypatch.setattr(ci, "_post", _fake_post(embeddings=False)[0])
    text = "a = 1\n\f\nb = 2\n"
    (proj / "src" / "paged.py").write_text(text)
    ci.index(str(proj))
    conn = ci._connect(str(proj))
    conn.execute(  # the digest an older version stored for it
        "UPDATE files SET sha = ? WHERE path = 'src/paged.py'",
        (hashlib.sha1(text.encode("utf-8")).hexdigest(),),
    )
    conn.commit()
    conn.close()
    assert ci.index(str(proj))["files"]["changed"] == 1
    assert ci.index(str(proj))["files"]["changed"] == 0


@pytest.mark.parametrize("shape", ["null_body", "null_vectors"])
def test_embed_host_answering_a_bad_shape_degrades_to_keyword(monkeypatch, proj, shape):
    # A 200 whose JSON is null or [] reaches embed_texts as obj=None, and a host can send
    # null vectors: code_index and code_search crashed (AttributeError / TypeError)
    # instead of degrading to keyword search.
    def post(url, payload, timeout):
        if shape == "null_body":
            return None, None
        return {"data": [{"embedding": None} for _ in payload["input"]]}, None

    monkeypatch.setattr(ci, "_post", post)
    res = ci.index(str(proj))
    assert res["status"] == "ok" and res["mode"] == "keyword" and res["degraded"]
    out = ci.search(str(proj), "alpha_handler_7")
    assert out["status"] == "ok" and out["mode"] == "keyword"
    assert out["hits"][0]["path"] == "src/alpha.py"


def test_handlers_require_root_and_clamp_k(monkeypatch, proj):
    import ask_fable.server as server

    monkeypatch.setattr(ci, "project_root", lambda: str(proj))
    monkeypatch.setattr(ci, "index", lambda root, force=False: {"status": "ok", "force": force})
    assert server._handle_code_index({"rebuild": True}) == {"status": "ok", "force": True}

    monkeypatch.setattr(
        ci, "search", lambda root, q, k=8, rerank=False: {"q": q, "k": k, "rerank": rerank}
    )
    assert server._handle_code_search({"query": "x", "k": 99})["k"] == 50
    bad = server._handle_code_search({"k": 1})
    assert bad["status"] == "error" and bad["kind"] == "bad_args"

    monkeypatch.setattr(ci, "project_root", lambda: None)
    missing = server._handle_code_index({})
    assert missing["status"] == "error" and missing["kind"] == "not_configured"
