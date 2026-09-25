"""Local code+docs index for semantic search over the configured project root.

``code_search`` is the retrieval half of ask_fable's context tooling: instead of
naming files for ``context_pack``, an agent asks a question and gets the most
relevant windows of the repo back with ``file:start-end`` references.

Ranking is hybrid: FTS5 keyword (BM25) always works, embeddings add semantic
ranking when an OpenAI-compatible ``/v1/embeddings`` host answers. The embedding
path is deliberately OPT-IN and FAIL-SAFE:

- hosts come from ``ASK_FABLE_EMBED_HOSTS`` (comma-separated, tried in order).
  Unset means the LM Studio bridge's own host (``ASK_FABLE_LMSTUDIO_BASE_URL``,
  default lmstudio.example.com). A part-time box (e.g. the 8 GB eGPU at lmstudio-host) is used
  ONLY when the operator names it — nothing defaults to it.
- every embed call is best-effort: a dead host is skipped for the next, and when
  no host answers, indexing stores chunks WITHOUT vectors and search quietly
  degrades to keyword ranking with an explicit ``degraded`` note; a later
  ``code_index`` backfills the missing vectors.

Storage: one SQLite file per project under
``${XDG_STATE_HOME:-~/.local/state}/ask_fable/code_index/<root-fingerprint>.db``
— never inside the repo. WAL + busy timeout (the session-per-client process
model means concurrent readers, occasionally concurrent writers). All sqlite/OS
errors degrade to a status dict; hash checks make re-indexing incremental.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import sqlite3
import struct
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import _paths, config, lmstudio
from .safe_fs import _is_blocked, max_file_bytes, resolve_root

DEFAULT_EMBED_MODEL = "text-embedding-nomic-embed-text-v1.5"
DEFAULT_EMBED_TIMEOUT = 30.0
DEFAULT_EMBED_BATCH = 64
CHUNK_LINES = 80
CHUNK_OVERLAP = 16
CHUNK_MAX_CHARS = 4000
MAX_FILES = 4000
_RRF_K = 60  # reciprocal-rank-fusion constant
_RERANK_CANDIDATES = 12
# Directories that are never worth walking. `.git` is mandatory (objects are
# unreadable junk); the rest are dependency/build trees that would flood the
# index with vendored code.
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".tox",
    ".nox",
    "dist",
    "build",
    "site-packages",
    ".cache",
    "target",
    ".next",
    ".gradle",
    ".dart_tool",
}
_WORD_RE = re.compile(r"[A-Za-z0-9_./-]{2,}")
# Line breaks str.splitlines() honours besides "\n" / "\r\n". An index built while
# chunking used splitlines() numbered a file containing one differently from
# context_pack, so that file's digest is tagged (see index) to re-chunk it once.
_EXTRA_BREAKS_RE = re.compile("\r(?!\n)|[\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")


def _api_key() -> str | None:
    raw = (
        os.environ.get("ASK_FABLE_EMBED_API_KEY")
        or os.environ.get("ASK_FABLE_LMSTUDIO_API_KEY")
        or ""
    ).strip()
    return raw or None


def embed_model() -> str:
    """Model used for indexing and querying (``ASK_FABLE_EMBED_MODEL``)."""
    return (os.environ.get("ASK_FABLE_EMBED_MODEL") or "").strip() or DEFAULT_EMBED_MODEL


def embed_hosts() -> list[str]:
    """Ordered embedding hosts — ``ASK_FABLE_EMBED_HOSTS`` or the LM Studio host.

    An explicit list is the opt-in: e.g.
    ``ASK_FABLE_EMBED_HOSTS=http://lmstudio-host:1234,http://lmstudio.example.com:1234``
    prefers the eGPU and falls back to the always-on box.
    """
    raw = (os.environ.get("ASK_FABLE_EMBED_HOSTS") or "").strip()
    if raw:
        hosts = [h.strip().rstrip("/") for h in raw.split(",") if h.strip()]
    else:
        hosts = [lmstudio.base_url()]
    return list(dict.fromkeys(hosts))


def rerank_model() -> str:
    """Optional chat model used for the rerank stage (``ASK_FABLE_EMBED_RERANK_MODEL``).

    Empty means rerank is unavailable — an explicit opt-in, like the hosts.
    """
    return (os.environ.get("ASK_FABLE_EMBED_RERANK_MODEL") or "").strip()


_OPENER: urllib.request.OpenerDirector | None = None


def _get_opener() -> urllib.request.OpenerDirector:
    """Proxies bypassed: embed hosts are LAN boxes (same rule as the LM Studio bridge)."""
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _OPENER


def _post(url: str, payload: dict, timeout: float) -> tuple[dict | None, str | None]:
    """One JSON POST; transport failures are returned, never raised."""
    headers = {"accept": "application/json", "content-type": "application/json"}
    key = _api_key()
    if key:
        headers["authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers
    )
    try:
        with _get_opener().open(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
            return (obj if isinstance(obj, dict) else None), None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return None, f"HTTP {e.code}: {detail or e.reason}"
    # HTTPException: urllib doesn't wrap what read() raises (IncompleteRead on a cut body).
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError) as e:
        reason = getattr(e, "reason", None) or e
        return None, f"network error: {reason}"


def embed_texts(texts: list[str], *, timeout: float = DEFAULT_EMBED_TIMEOUT) -> dict:
    """Embed ``texts`` via the first answering host.

    Returns ``{"ok", "host", "model", "vectors", "error"}`` — ``ok`` False with
    an ``error`` naming every host that failed when none answers.
    """
    if not texts:
        return {"ok": True, "host": "", "model": embed_model(), "vectors": [], "error": ""}
    model = embed_model()
    errors: list[str] = []
    for host in embed_hosts():
        vectors: list[list[float]] = []
        failed = ""
        for i in range(0, len(texts), DEFAULT_EMBED_BATCH):
            batch = texts[i : i + DEFAULT_EMBED_BATCH]
            obj, err = _post(
                f"{host}/v1/embeddings",
                {"model": model, "input": batch},
                timeout,
            )
            # A 200 whose JSON is null/[] (a proxy, a misconfigured host) comes back as
            # obj=None with no error: a bad shape degrades to keyword, never crashes.
            if err or not isinstance(obj, dict) or not isinstance(obj.get("data"), list):
                failed = err or "unexpected response shape"
                break
            try:
                vectors.extend([[float(x) for x in d["embedding"]] for d in obj["data"]])
            except (KeyError, TypeError, ValueError):
                failed = "unexpected embedding entries"
                break
        if not failed:
            return {"ok": True, "host": host, "model": model, "vectors": vectors, "error": ""}
        errors.append(f"{host}: {failed}")
    return {
        "ok": False,
        "host": "",
        "model": model,
        "vectors": [],
        "error": "no embed host answered — " + "; ".join(errors),
    }


def _db_path(root: str) -> Path:
    fingerprint = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:16]
    return _paths.xdg_state_dir() / "ask_fable" / "code_index" / f"{fingerprint}.db"


def _connect(root: str) -> sqlite3.Connection:
    path = _db_path(root)
    if not _paths.ensure_dir_secure(path.parent):
        raise OSError(f"cannot create index dir {path.parent}")
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS files(
            path TEXT PRIMARY KEY, sha TEXT NOT NULL, size INTEGER, mtime REAL,
            indexed_at REAL
        );
        CREATE TABLE IF NOT EXISTS chunks(
            id INTEGER PRIMARY KEY, path TEXT NOT NULL, start INTEGER NOT NULL,
            end INTEGER NOT NULL, text TEXT NOT NULL, sha TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
        CREATE TABLE IF NOT EXISTS vectors(
            chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
            model TEXT NOT NULL, dim INTEGER NOT NULL, v BLOB NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            text, content='chunks', content_rowid='id'
        );
        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text)
            VALUES ('delete', old.id, old.text);
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text)
            VALUES ('delete', old.id, old.text);
            INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
        END;
        """
    )
    return conn


def _read_text(path: Path, cap: int) -> str | None:
    """Whole-file text, or None when binary / too large / unreadable."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > cap or b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _chunks_for(text: str) -> list[tuple[int, int, str]]:
    """``(start_line, end_line, text)`` windows, overlapping, char-capped.

    Lines are numbered the way ``safe_fs`` counts them (only ``\\n`` ends a line),
    so a hit's ``start-end`` is a valid ``context_pack`` range; ``splitlines()``
    also broke on ``\\f``, ``\\v``, a lone ``\\r``, ``\\x85``, ``\\u2028`` … and
    the numbers drifted."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()  # a trailing newline ends the last line; it does not start one
    lines = [line.removesuffix("\r") for line in lines]  # CRLF text chunks as before
    out: list[tuple[int, int, str]] = []
    i = 0
    while i < len(lines):
        end = i
        chars = 0
        while end < len(lines) and end - i < CHUNK_LINES and chars < CHUNK_MAX_CHARS:
            chars += len(lines[end]) + 1
            end += 1
        body = "\n".join(lines[i:end]).strip()
        if body:
            for piece_start in range(0, len(body), CHUNK_MAX_CHARS):
                piece = body[piece_start : piece_start + CHUNK_MAX_CHARS]
                if piece.strip():
                    out.append((i + 1, end, piece))
        if end >= len(lines):
            break
        i = max(i + 1, end - CHUNK_OVERLAP)
    return out


def _pack_blocklist() -> tuple[str, ...]:
    """The operator's ``pack_blocklist`` — the index honours the same secret
    blocklist ``context_pack`` does, so a blocked file is never chunked, embedded
    (shipped to the embed/rerank hosts) or returned by ``code_search``."""
    return tuple(config.get_list("pack_blocklist") or ())


def _iter_files(root: str, extra: tuple[str, ...] = ()) -> tuple[list[str], int]:
    """Root-relative text-file paths (sorted) and how many were refused."""
    base = Path(root)
    found: list[str] = []
    refused = 0
    cap = max_file_bytes()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            if len(found) >= MAX_FILES:
                return found, refused
            fp = Path(dirpath) / name
            try:
                rel = str(fp.relative_to(base))
            except ValueError:
                continue
            if _is_blocked(rel.replace(os.sep, "/"), extra):
                refused += 1
                continue
            if fp.is_symlink():
                # os.walk(followlinks=False) already refuses symlinked DIRs, but a symlinked
                # FILE would still be stat'd and read (e.g. notes.md -> ~/.ssh/id_rsa),
                # leaking a file OUTSIDE the root into the index. Refuse it — a real file
                # inside the root is indexed on its own path.
                refused += 1
                continue
            try:
                st = fp.stat()
            except OSError:
                refused += 1
                continue
            if not fp.is_file() or st.st_size > cap:
                refused += 1
                continue
            if _read_text(fp, cap) is None:
                refused += 1
                continue
            found.append(rel.replace(os.sep, "/"))
    return found, refused


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes, dim: int) -> list[float]:
    return [v[0] for v in struct.iter_unpack("<f", blob[: dim * 4])]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = na = nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if not na or not nb:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def index(root: str, *, force: bool = False) -> dict:
    """Incrementally index ``root``; embeddings are best-effort (see module doc)."""
    model = embed_model()
    try:
        conn = _connect(root)
    except (OSError, sqlite3.Error) as e:
        return {"status": "error", "kind": "io", "detail": f"cannot open index: {e}"}
    try:
        on_disk, refused = _iter_files(root, _pack_blocklist())
        known = {
            str(r[0]): (str(r[1]), int(r[2] or 0))
            for r in conn.execute("SELECT path, sha, mtime FROM files")
        }
        added = changed = removed = unchanged = 0
        for rel in on_disk:
            fp = Path(root) / rel
            try:
                mtime = fp.stat().st_mtime
            except OSError:
                mtime = 0.0
            text = _read_text(fp, max_file_bytes())
            if text is None:
                refused += 1
                continue
            sha = hashlib.sha1(text.encode("utf-8")).hexdigest()
            if _EXTRA_BREAKS_RE.search(text):
                sha += "+nl"  # see _EXTRA_BREAKS_RE: re-chunk an old index's copy once
            previous = known.pop(rel, None)
            if previous and not force and previous[0] == sha:
                unchanged += 1
                continue
            conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
            for start, end, body in _chunks_for(text):
                conn.execute(
                    "INSERT INTO chunks(path, start, end, text, sha) VALUES (?, ?, ?, ?, ?)",
                    (rel, start, end, body, hashlib.sha1(body.encode()).hexdigest()[:16]),
                )
            conn.execute(
                "INSERT OR REPLACE INTO files(path, sha, size, mtime, indexed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (rel, sha, fp.stat().st_size, mtime, time.time()),
            )
            if previous:
                changed += 1
            else:
                added += 1
        for rel in known:
            conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
            conn.execute("DELETE FROM files WHERE path = ?", (rel,))
            removed += 1
        conn.commit()

        total_chunks = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        pending_rows = list(
            conn.execute(
                "SELECT c.id, c.text FROM chunks c "
                "LEFT JOIN vectors v ON v.chunk_id = c.id "
                "WHERE v.chunk_id IS NULL OR v.model != ?",
                (model,),
            )
        )
        embedded = 0
        embed_error = ""
        host_used = ""
        if pending_rows:
            result = embed_texts([str(r[1]) for r in pending_rows])
            if result["ok"] and len(result["vectors"]) != len(pending_rows):
                # An embedding host that returns ok but the wrong vector count would blow up
                # the strict zip below (ValueError, uncaught). Degrade to keyword search —
                # the module's fail-safe — instead of crashing the whole index call.
                result = {
                    "ok": False,
                    "error": f"embedding host returned {len(result['vectors'])} vectors "
                    f"for {len(pending_rows)} chunks",
                }
            if result["ok"]:
                host_used = result["host"]
                for row, vec in zip(pending_rows, result["vectors"], strict=True):
                    conn.execute(
                        "INSERT OR REPLACE INTO vectors(chunk_id, model, dim, v) "
                        "VALUES (?, ?, ?, ?)",
                        (int(row[0]), model, len(vec), _pack(vec)),
                    )
                conn.commit()
                embedded = len(pending_rows)
            else:
                embed_error = result["error"]
        status = {
            "version": 1,
            "status": "ok",
            "root_fingerprint": _db_path(root).stem,
            "model": model,
            "host": host_used,
            "files": {
                "indexed": len(on_disk),
                "added": added,
                "changed": changed,
                "unchanged": unchanged,
                "removed": removed,
                "refused": refused,
                "truncated": len(on_disk) >= MAX_FILES,
            },
            "chunks": total_chunks,
            "embedded": embedded,
            "pending": len(pending_rows) - embedded,
            "mode": "semantic" if not embed_error else "keyword",
        }
        if embed_error:
            status["degraded"] = embed_error
        return status
    except sqlite3.Error as e:
        return {"status": "error", "kind": "sqlite", "detail": str(e)}
    finally:
        conn.close()


def _fts_query(query: str) -> str:
    terms = _WORD_RE.findall(query)
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


def _snippet(text: str, limit: int = 240) -> str:
    flat = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return flat[: limit - 3] + "..." if len(flat) > limit else flat


def _rerank(query: str, hits: list[dict], model: str) -> tuple[list[dict], str]:
    """Ask a chat model to reorder ``hits``; returns (hits, note)."""
    if not model or len(hits) < 2:
        return hits, "skipped"
    listing = "\n\n".join(
        f"[{i}] {h['path']}:{h['start']}-{h['end']}\n{_snippet(h['text'])}"
        for i, h in enumerate(hits)
    )
    prompt = (
        "Rank the snippets by relevance to the query. Reply with ONLY a JSON array "
        f"of their indices, best first, no prose.\n\nQuery: {query}\n\n{listing}"
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        # Reasoning models spend most of a small budget thinking and can return an
        # EMPTY content with finish_reason=length (observed live on nemotron-nano
        # at 200); 800 leaves room for the ranking itself. Still bounded.
        "max_tokens": 800,
        "temperature": 0,
    }
    errors: list[str] = []
    for host in embed_hosts():
        obj, err = _post(f"{host}/v1/chat/completions", payload, 60.0)
        if err or not obj:
            errors.append(f"{host}: {err or 'empty'}")
            continue
        try:
            content = obj["choices"][0]["message"]["content"]
            order = json.loads(re.search(r"\[[^\]]*\]", content).group(0))
            seen = [hits[int(i)] for i in order if 0 <= int(i) < len(hits)]
            rest = [h for h in hits if h not in seen]
            return seen + rest, "applied"
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            errors.append(f"{host}: unparseable ranking")
    return hits, "unavailable: " + "; ".join(errors)


def search(root: str, query: str, *, k: int = 8, rerank: bool = False) -> dict:
    """Hybrid search over the indexed root; degrades to keyword when no host."""
    try:
        conn = _connect(root)
    except (OSError, sqlite3.Error) as e:
        return {"status": "error", "kind": "io", "detail": f"cannot open index: {e}"}
    try:
        count = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        if not count:
            return {"status": "error", "kind": "not_indexed", "detail": "run code_index first"}
        model = embed_model()
        query_vec: list[float] = []
        host = ""
        degraded = ""
        # Short timeout: a dead host must degrade to keyword quickly, not stall the
        # search for the indexing-scale budget.
        embed_result = embed_texts([query], timeout=10.0)
        if embed_result["ok"]:
            query_vec = embed_result["vectors"][0] if embed_result["vectors"] else []
            host = embed_result["host"]
        else:
            degraded = embed_result["error"]

        merged: dict[int, dict] = {}
        fts = _fts_query(query)
        if fts:
            for rank, row in enumerate(
                conn.execute(
                    "SELECT c.id, c.path, c.start, c.end, c.text FROM chunks_fts f "
                    "JOIN chunks c ON c.id = f.rowid WHERE chunks_fts MATCH ? "
                    "ORDER BY bm25(chunks_fts) LIMIT 50",
                    (fts,),
                )
            ):
                merged[int(row[0])] = {
                    "path": row[1],
                    "start": int(row[2]),
                    "end": int(row[3]),
                    "text": str(row[4]),
                    "score": 1.0 / (_RRF_K + rank + 1),
                    "why": "keyword",
                }
        if query_vec:
            rows = conn.execute(
                "SELECT c.id, c.path, c.start, c.end, c.text, v.v, v.dim FROM vectors v "
                "JOIN chunks c ON c.id = v.chunk_id WHERE v.model = ?",
                (model,),
            ).fetchall()
            scored = []
            for row in rows:
                vec = _unpack(bytes(row[5]), int(row[6]))
                if len(vec) != len(query_vec):
                    continue
                scored.append((_cosine(query_vec, vec), row))
            scored.sort(key=lambda pair: pair[0], reverse=True)
            for rank, (_sim, row) in enumerate(scored[:50]):
                entry = merged.setdefault(
                    int(row[0]),
                    {
                        "path": row[1],
                        "start": int(row[2]),
                        "end": int(row[3]),
                        "text": str(row[4]),
                        "score": 0.0,
                        "why": "semantic",
                    },
                )
                entry["score"] += 1.0 / (_RRF_K + rank + 1)
                if entry["why"] == "keyword":
                    entry["why"] = "keyword+semantic"
        # An index built before a path was blocklisted still holds its chunks until
        # the next code_index; never serve them in the meantime.
        extra = _pack_blocklist()
        live = [h for h in merged.values() if not _is_blocked(str(h["path"]), extra)]
        hits = sorted(live, key=lambda h: h["score"], reverse=True)[: max(1, k)]
        mode = "semantic+keyword" if query_vec and fts else ("semantic" if query_vec else "keyword")
        rerank_note = ""
        if rerank:
            model_hits = hits[: _RERANK_CANDIDATES]
            model_hits, rerank_note = _rerank(query, model_hits, rerank_model())
            hits = model_hits + hits[len(model_hits) :]
            if rerank_note == "skipped" and not rerank_model():
                rerank_note = "skipped: set ASK_FABLE_EMBED_RERANK_MODEL to enable"
        payload = {
            "version": 1,
            "status": "ok",
            "mode": mode,
            "model": model,
            "host": host,
            "hits": [
                {
                    "path": h["path"],
                    "start": h["start"],
                    "end": h["end"],
                    "score": round(float(h["score"]), 6),
                    "why": h["why"],
                    "snippet": _snippet(h["text"]),
                }
                for h in hits
            ],
        }
        if degraded:
            payload["degraded"] = degraded
        if rerank_note:
            payload["rerank"] = rerank_note
        return payload
    except sqlite3.Error as e:
        return {"status": "error", "kind": "sqlite", "detail": str(e)}
    finally:
        conn.close()


def status(root: str) -> dict:
    """Index counts without touching the network."""
    try:
        conn = _connect(root)
    except (OSError, sqlite3.Error) as e:
        return {"status": "error", "kind": "io", "detail": f"cannot open index: {e}"}
    try:
        chunks = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        files = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        model = embed_model()
        embedded = int(
            conn.execute("SELECT COUNT(*) FROM vectors WHERE model = ?", (model,)).fetchone()[0]
        )
        hosts = embed_hosts()
        return {
            "version": 1,
            "status": "ok",
            "indexed": bool(chunks),
            "files": files,
            "chunks": chunks,
            "embedded": embedded,
            "pending": max(0, chunks - embedded),
            "model": model,
            "hosts": hosts,
            "db": str(_db_path(root)),
        }
    except sqlite3.Error as e:
        return {"status": "error", "kind": "sqlite", "detail": str(e)}
    finally:
        conn.close()


def project_root() -> str | None:
    """The configured project root, or None when unset (same source as context_pack)."""
    return resolve_root()
