# LAN context bus

Status: **shipped.** The blob codec (`context_crypto.py`), the store backend
seam + rollback guard, the client with request auth (`context_bus.py`), the
daemon with migration (`context_busd.py`), and packaging (`ask-fable[lan]` +
the `context-busd` script). Any failure on the bus degrades the store — it
never silently falls back to local.

## Why

The context bus kills the re-paste tax on one machine: `context(op="write", …)` a blob
once, `context_ref` it from every later call. The LAN extension makes the same
store shared across machines, so a blob written on machine A resolves from
machine B — without the daemon or the network ever holding readable content.

## Architecture (owner daemon)

- One nominated host runs `context-busd`, the **only** process that ever opens
  the bus database. Every MCP instance on every machine (including the owner's)
  is a client.
- Blobs are sealed **client-side** with a fleet pre-shared key (PSK) before they
  leave the writing machine. The daemon holds **no content key** and stores only
  `afctx1:` ciphertext plus routing metadata.
- The codec sits **above** the storage seam: `context_store`'s public functions
  shim over a `_Backend` (local SQLite today; remote client later). One code
  path decodes sealed values, so local, daemon, and any future cache all behave
  identically.
- The daemon owns its **own database and table** (`bus_context`), so the local
  `context.db` schema is untouched and local mode can never read envelopes in
  normal operation.

## Blob format (`afctx1:` armor)

```
armor  = "afctx1:" + base64( envelope )
envelope = ver(1) || kid(1) || wlen(2 BE) || writer || ts_ns(8 BE)
           || salt(16) || nonce(12) || ct+tag
plaintext = JSON {"v": <value>, "d": <description>}
AAD    = lp(ver) || lp(kid) || lp(key_name) || lp(writer) || lp(str(ts_ns))
K      = HKDF-SHA256(psk, salt, info=b"ask-fable/ctx/v1/blob")
```

- **Writer and ts live in the envelope header.** They are cleartext but
  authenticated (in the AAD), so an envelope is self-describing: anyone holding
  the keyring can open it knowing only the storage key name. That is what makes
  the rollback guard work with no side metadata.
- **`writer` is a composite attribution** — `machine[/client[/model]]`, e.g.
  `workstation/claude-code` (or `workstation/claude-code/opus-4.8` when a model id is
  configured). `machine` is `machine_id()` and is always present; `client` is
  the auto-detected MCP harness (`claude-code`/`opencode`/`codex`/…), omitted
  rather than written as `unknown`; `model` is appended only when explicitly set
  (`ASK_FABLE_MODEL_ID` / config `model_id`), since no source can auto-detect the
  driving model. Each component is sanitised to a `/`-safe token, so readers
  recover the fields by splitting on `/`, and a pre-composite bare-machine writer
  parses as a one-element split. The whole string is in the AAD, so every
  component is authenticated together. Attribution is recorded, not
  surfaced to readers: `context_read` returns only the value and
  description, so a blob's writer is observed in the daemon's `bus_context.writer`
  column and its one-line logs, or read straight off the armor with
  `envelope_meta` (no keyring needed — the header is cleartext).
- **Fresh salt and 12-byte random nonce on every seal** (including overwrites):
  each blob gets its own key, so nonce collision across blobs is impossible by
  construction. Counters and time-derived nonces are forbidden.
- **`key_name` is bound at seal time** and must be supplied at unseal; moving
  ciphertext to a different key fails the tag rather than returning wrong data.
- **Descriptions stay inside the plaintext** — the keyless daemon must not learn
  content summaries.

## Keyring

- `ASK_FABLE_CONTEXT_KEYRING` → default `~/.config/ask_fable/context_keyring`.
- Format: one `kid:base64(32 bytes)` per line, **newest first**. The first line
  seals; every listed kid can unseal.
- Mode must be 0600 (0600-ish directory): a group/other-readable keyring is a
  hard refusal.
- **Rotation, no flag day:** prepend the new kid line everywhere → new seals use
  it → reseal old blobs (`context-busd migrate`/reseal, later) → drop the old
  line once nothing references it.
- **Retiring a machine:** rotate the fleet key and reseal; a shared symmetric
  key has no per-node revocation. Removing its bus token only stops new reads of
  new secrets — everything it already copied is out.
- **Never** put the key in an env var, MCP config JSON, tool argument, or tool
  result. Distribute the keyring out-of-band (scp over ssh).

## Environment

| Variable | Meaning | Default |
|---|---|---|
| `ASK_FABLE_CONTEXT_PATH` | Local SQLite file | `~/.local/state/ask_fable/context.db` |
| `ASK_FABLE_CONTEXT_BUS` | Bus URL (`unix:///path` or `http://host:port`). When set, all context ops go to the bus | unset (local mode) |
| `ASK_FABLE_CONTEXT_KEYRING` | Keyring file | `~/.config/ask_fable/context_keyring` |
| `ASK_FABLE_CONTEXT_MAX_ROWS` | Row cap (local mode); **per-writer** cap on the daemon (a writer is now the `machine/client[/model]` tuple, so total DB size is bounded by ~machines × harnesses × cap — the client is the harness label, not a per-window id) | `0` = unlimited |
| `ASK_FABLE_CONTEXT_BUS_TOKEN_FILE` | Bus request-auth token (0600, ≥16 chars) | `~/.config/ask_fable/context_bus_token` |
| `ASK_FABLE_CONTEXT_BUS_TIMEOUT` | Client socket timeout seconds | `5` |
| `ASK_FABLE_CONTEXT_MAX_BYTES` | Request body cap, enforced on both ends | 8 MiB |
| `ASK_FABLE_CONTEXT_BUS_DB` | Daemon database path | `~/.local/state/ask_fable/context_bus.db` |
| `ASK_FABLE_CONTEXT_BUS_SOCKET` | Daemon unix socket path | `$XDG_RUNTIME_DIR/ask_fable/context_bus.sock` |
| `ASK_FABLE_MACHINE_ID` | Machine component of the writer id | hostname |
| `ASK_FABLE_MODEL_ID` | Model component of the writer id (config key `model_id`); appended only when set — never auto-detected | unset (no model component) |
| `ASK_FABLE_CONTEXT_HWM_PATH` | Per-client rollback high-water-mark DB | `~/.local/state/ask_fable/context_hwm.db` |
| `ASK_FABLE_CONTEXT_HWM` | Set `0` to disable the rollback guard (explicit operator override after a restore) | enabled |

Config-file keys (`config.json`) override env for `context_bus` (the
`setting()` precedence rule).

## Failure matrix

| Condition | Behavior |
|---|---|
| Key absent | `get() -> None`, `last_error() == None` → callers treat as **missing** |
| Store locked/corrupt/unwritable | `get() -> None`, `last_error()` set → **degraded**, hard retry |
| Sealed blob, keyring present, tag OK | Unsealed transparently (value + description) |
| Sealed blob, keyring absent/loose/malformed | `get() -> None`, `last_error() = "sealed context at '<key>': KeyringError: …"` → degraded; `entries()` marks that row `unreadable`, never fails the list |
| Sealed blob, unknown kid / tampered / wrong key name | Same as above with `UnknownKidError` / `CorruptEnvelopeError` |
| `cryptography` not installed | `CryptoUnavailable` → degraded, named installation hint |
| Bus configured but unreachable / 401 / 409 / proto mismatch | Operation fails with `last_error()` naming the cause ("unreachable …", "clock skew", "replay", "stale writer_ts", "proto …") → degraded; never a silent fallback to local |
| Plain value that merely starts with `afctx1:` but is not valid armor | Treated as plaintext in local mode; on the bus it is refused as an unparseable envelope |
| A real value that *is* valid armor shape (contrived) | Interpreted as sealed; escape is not implemented — documented edge |
| Bus value older (beyond 60 s slack) than this client's high-water mark | Refused as a possible stale/rolled-back owner — `last_error` names the key and both timestamps; never returned |
| Bus value is not a sealed envelope (owner serving plaintext) | Refused at the client — plaintext from the owner is never returned |

## Wire contract

- `POST /v1/{put,get,delete,list}`, `GET /health` (unauthenticated: proto,
  rows, uptime).
- Header `X-AF-Proto: 1`. Client refuses unknown protocol versions.
- Auth: `X-AF-Auth: <ts>.<nonce>.<hex HMAC-SHA256(token,
  method|path|ts|nonce|sha256(body))>`. The bus token is a separate 0600 file —
  never the content PSK. `hmac.compare_digest`, ±60 s skew (401 says "clock
  skew" explicitly), nonce set evicted **by age**, per-key `writer_ts` floor so
  a replayed old PUT is rejected even after a daemon restart.
- `put` body: `{key, envelope, writer, ts_ns, plaintext_bytes}` (envelope is
  already sealed client-side; the daemon does not inspect its contents).
- `list` body `{"bounded": true}` returns every row's metadata, but a row's
  envelope only while it is ≤ 256 KiB and the running total stays within 16 MiB;
  a row without one lists with its plaintext size and `description_omitted`, so
  the listing never outgrows the client's response cap. Without the flag (an
  older client) every envelope is returned, as before.
- **Metadata trust:** the daemon parses the cleartext envelope header with
  `envelope_meta` and rejects (400) a request whose separate `writer`/`ts_ns`
  fields disagree, storing only header-derived values. The per-writer cap and
  the replay floor therefore run on AAD-bound metadata, not on a client's word.
  A header `ts_ns` more than 60 s in the future is refused (400): the daemon
  cannot authenticate the envelope, so an unbounded ts would let any peer set a
  key's replay floor out of reach of every honest writer.
- Daemon table: `bus_context(key PK, writer, writer_ts INTEGER, received_ts
  REAL, envelope TEXT, plaintext_bytes INTEGER)`, 0600 + WAL.
- Request body cap; 30 s per-connection socket timeout; structured one-line
  logs (op, key, writer, status — never bodies).
- Default bind: unix socket (`SO_PEERCRED` uid check) inside a 0700 dir; TCP
  non-loopback requires the token file and logs a warning. IP allowlists are
  hygiene, not a boundary.
- Browser requests are refused: any request carrying an `Origin` header gets
  403, and a tokenless TCP bind also requires a loopback `Host` header
  (`localhost` or a loopback literal), which closes DNS rebinding.
- `SIGTERM` exits cleanly (socket removed, lock released), and a stale socket
  file left by a crashed daemon is removed at startup once the DB lock is held.
- Daemon applies `ASK_FABLE_CONTEXT_MAX_ROWS` **per writer**; local mode
  unchanged.

## Rollout and kill-switch

1. Ship the guard release (this change) to every machine.
2. Start `context-busd` on the owner host.
3. `context-busd migrate` seals existing local rows onto the bus (client-side).
4. Set `ASK_FABLE_CONTEXT_BUS` per machine — or run
   `scripts/setup-context-bus-client.sh` (see [Onboarding a
   client](#onboarding-a-client)), which sets the `context_bus` config key
   instead; either way verify with `bus_status()`.
5. **Kill-switch:** unset the env → local mode; the read guard refuses any
   envelope that ends up in the local store, so armor can never reach a prompt.

## Threat model notes

- Confidentiality/integrity come from the sealed blobs, not from ssh/TLS. The
  daemon is a ciphertext shelf; a compromised daemon learns key names, writers,
  timestamps, and sizes — not values or descriptions.
- **Reader-side rollback guard:** each client keeps a local per-key high-water
  mark of the authenticated envelope timestamps it has accepted, in its own
  `context_hwm.db` (never on the bus — a rolled-back owner could roll that back
  too). A served envelope more than 60 s older than the mark is refused as a
  possible stale/rolled-back owner, and bus-mode reads require a sealed envelope
  at all, so the owner cannot bypass the guard by serving plaintext. Deliberate
  restores: set `ASK_FABLE_CONTEXT_HWM=0` or delete the guard database.
- The AEAD tag proves "a PSK holder wrote this", not **which** holder. The whole
  composite `writer` (`machine/client[/model]`) is an authenticated *claim* — any
  keyring holder can set `ASK_FABLE_MACHINE_ID`/`ASK_FABLE_MODEL_ID` to anything,
  and the client component is only as trustworthy as the harness's `clientInfo`.
  Treat it as provenance labelling, never authorization; per-machine signatures
  are a v2 option if attribution ever needs to be trustworthy.
- Local mode stores plaintext by design; the 0600 filesystem is the local
  boundary, and disk theft is full-disk encryption's job. Sealing happens only
  at the export boundary.
- No read cache in v1: a bus outage degrades (hard retry) rather than serving a
  stale local copy. A ciphertext read-through cache with explicit `stale_s`
  would sit behind the codec later.

## Do not

- Put `context.db` (or WAL/SHM) on NFS/SMB/Syncthing/Dropbox/iCloud.
- Hand-roll a cipher or KDF; use the PSK directly as a cipher key; use counter
  or time-derived nonces; reuse a salt on overwrite.
- Put the key in env/config/tool args/logs (the keyring **path** is fine).
- Let a remote failure surface as `missing` (it must stay `degraded`).
- Treat `writer` as authorization, or an AEAD failure as "missing".
- Trust ssh/TLS as the confidentiality guarantee — it is a carrier.
