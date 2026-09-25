# Remote context bus — sharing context across machines

> Part of the [ask-fable README](../README.md). Design internals and the wire
> contract live in [context_bus.md](context_bus.md); this page is the operator
> guide for standing the bus up.

The context bus kills the re-paste tax: `context(op="write", …)` a blob once,
`context_ref='<key>'` it from every later call. Out of the box that store is a
local SQLite file on one machine. The **remote context bus** makes the same
store shared across every machine on your LAN, so a blob written on machine A
resolves from machine B — without the server or the network ever holding
readable content. Remote mode is **opt-in**: with no bus URL configured,
everything stays in the local store on the one machine.

## Architecture

- One nominated host runs **`context-busd`**, the owner daemon — the only
  process that ever opens the bus database. Every ask_fable MCP instance on
  every machine (including the owner's own) talks to it as a client.
- Blobs are **sealed client-side** before they leave the writing machine, with
  a fleet pre-shared key (PSK) every client holds in a keyring file. Sealing is
  ChaCha20-Poly1305 (AEAD: encryption + integrity) with a per-blob key derived
  by HKDF from the PSK, a fresh salt, and a fresh random nonce on every write.
- The daemon is a **ciphertext shelf**: it holds no content key and stores only
  `afctx1:` envelopes plus routing metadata (key name, writer, timestamps,
  size). A compromised daemon — or a passive network observer — learns key
  names, writer labels, timestamps, and sizes, never values or descriptions.
- Requests are authenticated with a **separate bus token** (an HMAC over
  method, path, timestamp, nonce, and body hash). The bus token and the content
  PSK are two different secrets in two different files.
- Any bus failure — unreachable, auth rejected, stale data, missing keyring —
  **degrades** the store: the tool call reports the cause and tells the agent
  to retry. It never silently falls back to the local store, and sealed armor
  can never be spliced into a prompt as if it were content.

## Trust model, in plain terms

- **Who can read blob contents:** any machine holding the keyring. Who can read
  key names and metadata: the daemon and anyone who can see the LAN traffic
  (transport is plain HTTP; confidentiality comes from the sealed blobs, not
  the wire).
- **Who can write/delete:** anyone holding the bus token can put, list, and
  delete *envelopes* — but only keyring holders can produce a blob that
  clients will accept, because the AEAD tag won't verify otherwise.
- **Attribution is a claim, not authorization.** The `writer` label is
  authenticated only as "some keyring holder wrote this" — the symmetric PSK
  cannot prove *which* machine, and any holder can set the machine/model
  overrides to anything. Treat it as provenance labelling.
- **No per-node revocation.** A shared symmetric key can't be taken back from
  one machine: retiring a node means rotating the fleet key and resealing.
- **The rollback guard assumes an untrusted owner.** Each client independently
  refuses blobs that are older than what it has already seen (details below),
  so a restored backup or a replayed old blob is rejected at the reader.

## Prerequisites

- The `lan` extra everywhere (`pip install -e '.[lan]'` in a source checkout —
  the package is not on PyPI yet — or `uv sync --extra lan`). Without
  `cryptography`, bus mode refuses cleanly with a named error.
- Two secrets, generated once on the owner and distributed out of band (scp
  over ssh — never in env vars, MCP config JSON, tool arguments, or logs):

  ```sh
  # keyring: one "kid:base64(32 bytes)" line, newest first
  install -d -m 700 ~/.config/ask_fable
  { printf '1:'; python3 -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"; } \
    > ~/.config/ask_fable/context_keyring
  # bus token: any random string >= 16 chars
  python3 -c "import secrets;print(secrets.token_hex(24))" \
    > ~/.config/ask_fable/context_bus_token
  chmod 600 ~/.config/ask_fable/context_keyring ~/.config/ask_fable/context_bus_token
  ```

  Both files must be mode 0600 — a group/other-readable keyring or token is a
  hard refusal on both client and daemon.

## Owner side: run the daemon

```sh
context-busd serve --host 0.0.0.0 --port 8788 \
  --token-file ~/.config/ask_fable/context_bus_token
```

- Default transport is a unix socket (0600 in a 0700 dir, peer-uid checked) for
  same-host use; `--host` switches to TCP. A **non-loopback TCP bind without a
  token file refuses to start** — token auth is the boundary on the network,
  IP allowlists are hygiene.
- The daemon takes an exclusive `flock` on `<db>.lock`; a second instance on
  the same database refuses to start.
- Run it under your service supervisor of choice so it survives reboots.
- `GET /health` is the only unauthenticated route (proto, row count, uptime)
  so triage works before the token is fixed.

## Client side

### Automated: `scripts/setup-context-bus-client.sh`

From a checkout that has been synced with the `lan` extra:

```sh
cd /path/to/checkout && uv sync --extra lan
# copy context_keyring + context_bus_token into ~/.config/ask_fable/ first
# (scp each file as its own argument; install them 0600), then:
BUS_URL=http://<owner-host>:8788 bash scripts/setup-context-bus-client.sh /path/to/checkout
```

The script installs the keyring/token at 0600 (or verifies the copies already
in place), merges `context_bus` into `~/.config/ask_fable/config.json` (all
other keys preserved), and proves a real put/get round-trip through the
daemon. It needs a `uv` checkout (it uses `$REPO/.venv`); if you installed with
`pip` or `pipx`, use the manual steps below instead. Three things that bite:

- **Pass the checkout path explicitly** — the script uses the checkout you
  name (defaults to the current directory).
- **Always set `BUS_URL`** — the script requires it; there is no usable
  built-in default.
- **The setup venv is not necessarily the runtime.** The script verifies with
  the checkout's `.venv`, but the MCP entrypoint may run on a different
  interpreter. Re-check `bus_status()` under the interpreter that actually
  serves MCP, and confirm `cryptography` imports there — the bus degrades
  without it.

**Restart the MCP harnesses afterwards** — the bus URL is read at startup.

### Manual

1. Install `context_keyring` and `context_bus_token` at
   `~/.config/ask_fable/`, mode 0600.
2. Point the store at the bus, either in `~/.config/ask_fable/config.json`
   (`{"context_bus": "http://<owner-host>:8788"}`) or with
   `ASK_FABLE_CONTEXT_BUS`. The config file wins when both are set.
3. Verify, using the interpreter that serves MCP:

   ```sh
   python3 - <<'PY'
   from ask_fable import context_bus, context_store
   print(context_bus.bus_status())
   assert context_store.put("fleet:smoke", "hello", "smoke test")
   assert context_store.get("fleet:smoke") == "hello"
   context_store.delete("fleet:smoke")
   print("round-trip OK")
   PY
   ```

   "On the bus" means `bus_status()` is `reachable` with `proto: 1` **and** a
   real put/get round-trip succeeds — a `/health` ping alone is not proof.

## Configuration reference

| Variable (or config key) | Default | Meaning |
|---|---|---|
| `ASK_FABLE_CONTEXT_BUS` / `context_bus` | unset (local mode) | Bus URL: `unix:///abs/path` or `http://host:port`. When set, all context ops go to the bus. Config file overrides env. |
| `ASK_FABLE_CONTEXT_KEYRING` | `~/.config/ask_fable/context_keyring` | Fleet keyring file (0600 required). First line seals; every line unseals. |
| `ASK_FABLE_CONTEXT_BUS_TOKEN_FILE` | `~/.config/ask_fable/context_bus_token` | Bus request-auth token (0600, ≥16 chars). |
| `ASK_FABLE_CONTEXT_BUS_TIMEOUT` | `5` | Client socket timeout, seconds. |
| `ASK_FABLE_CONTEXT_MAX_BYTES` | 8 MiB | Request body cap, enforced on both client and daemon. |
| `ASK_FABLE_CONTEXT_BUS_DB` | `$XDG_STATE_HOME/ask_fable/context_bus.db` | Daemon database path. |
| `ASK_FABLE_CONTEXT_BUS_SOCKET` | `$XDG_RUNTIME_DIR/ask_fable/context_bus.sock` | Daemon unix socket path. |
| `ASK_FABLE_CONTEXT_MAX_ROWS` | `0` (unlimited) | Local mode: total row cap. Daemon: **per-writer** cap, so one machine can't evict another's blobs. |
| `ASK_FABLE_MACHINE_ID` / `machine_id` | hostname | Machine component of the writer id. |
| `ASK_FABLE_MODEL_ID` / `model_id` | unset | Model component of the writer id; appended only when set (never auto-detected). |
| `ASK_FABLE_CONTEXT_HWM_PATH` | `$XDG_STATE_HOME/ask_fable/context_hwm.db` | Per-client rollback-guard database. |
| `ASK_FABLE_CONTEXT_HWM` | enabled | Set `0` to disable the rollback guard (explicit override after a deliberate restore). |
| `ASK_FABLE_CONTEXT_PATH` | `$XDG_STATE_HOME/ask_fable/context.db` | Local store, used only in local mode. |

## Writer attribution

Every sealed blob carries a composite writer label —
`machine[/client[/model]]`, e.g. `workstation/claude-code` or
`workstation/claude-code/opus-5`:

- `machine` is always present (`ASK_FABLE_MACHINE_ID`, else the hostname).
- `client` is the auto-detected MCP harness (`claude-code`, `opencode`,
  `codex`, …); omitted when it can't be resolved, never written as `unknown`.
- `model` is appended only when explicitly configured *and* a client component
  is present, so field positions never shift.
- Each component is sanitised to a `/`-safe token, so readers recover the
  fields by splitting on `/` — and a pre-composite bare-machine writer parses
  as a one-element split.

The label is bound into the envelope's authenticated data, so it can't be
re-labelled without the key. It is **recorded, not surfaced**: `context_read`
returns value/description only. To see attribution, inspect
the daemon's `bus_context.writer` column or its one-line logs, or read the
cleartext envelope header (no keyring needed).

## Rollback guard

The daemon refuses a write whose timestamp is older (beyond a 60 s clock-skew
slack) than the row it would replace — a persistent floor that survives daemon
restarts. That stops old blobs being *written*; the reader-side guard stops
old blobs being *served*:

- Each client keeps a local per-key high-water mark of the newest
  authenticated envelope timestamp it has accepted, in its own
  `context_hwm.db` — never on the bus, where a rolled-back owner could roll
  the marks back too.
- A served envelope more than 60 s older than the mark is refused as a
  possible stale or rolled-back owner; the error names the key and both
  timestamps. Marks only move forward.
- Bus-mode reads also require a sealed envelope at all, so the owner can't
  bypass the guard by serving plaintext.
- **Deliberate restore:** set `ASK_FABLE_CONTEXT_HWM=0`, or delete the guard
  database, on each client.
- Keep fleet clocks in sync (NTP). A writer with a fast clock can push other
  clients' marks into the future, after which normal writes look "rolled
  back" to them until the slack passes.

## Migrating an existing local store

```sh
context-busd migrate --from ~/.local/state/ask_fable/context.db \
  --bus http://<owner-host>:8788
```

- Runs in the **client role**: it reads the local `context.db`, seals each
  plaintext row with the local keyring, and PUTs it to the bus. The daemon
  needs no special mode.
- **Idempotent:** rows that are already sealed envelopes are skipped, so a
  re-run is safe. Exits non-zero if any row failed.
- It **copies, not moves** — the local plaintext rows remain in `context.db`
  (0600) afterwards. Delete or retain them per your local policy.
- **Kill-switch:** unsetting the bus URL returns a machine to local mode; the
  read guard refuses any envelope that ends up in the local store, so armor
  can never reach a prompt.

## Key rotation and retiring a machine

Rotation has no flag day: prepend the new `kid:base64` line to every keyring
(new seals use the first line; every listed kid can still unseal), reseal old
blobs, then drop the old line once nothing references it. Note that **reseal
tooling is not yet implemented** — `migrate` only seals *plaintext* rows — so
today a rotation effectively starts the bus fresh for new writes. Retiring a
machine means rotating the fleet key; removing its bus token only stops new
requests, and anything it already copied stays copied.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `bus_status()` → `configured: false` | Bus URL not set, or set after the MCP server started — restart the harness. |
| `unreachable …` | Daemon down, wrong host/port, or firewall. Check `curl http://<owner-host>:8788/health`. |
| `401 clock skew` | Client and owner clocks differ by >60 s — sync NTP. |
| `401 replay` / `401 bad signature` | Wrong or stale bus token on the client; re-copy the token file. |
| `keyring … is group/other-accessible` | `chmod 600` the keyring (same for the token file). |
| `envelope kid N is not in the keyring` | This machine's keyring predates a rotation — re-copy the current keyring. |
| `rollback guard: '<key>' … older than the high-water mark` | The owner is serving stale data (restored backup, replay). If the restore was deliberate: `ASK_FABLE_CONTEXT_HWM=0` or delete `context_hwm.db` on that client. |
| `the owner served an unsealed value on the bus` | Wrong service on that port, or a daemon/database mix-up — never ignored, always refused. |
| `stale writer_ts (possible replay)` (409) | The write carried an older timestamp than the stored row; retry with a fresh write. |
| Daemon won't start: address in use (unix socket) | A previous daemon was killed without cleanup — delete the stale socket file and restart. |
| Store "degraded" errors that mention `sealed context` | Keyring missing/unreadable on this machine; the blob is fine, the local key is not. |
| Everything fails after `uv sync` | The MCP runtime interpreter lacks `cryptography` — install `ask-fable[lan]` there, not just in the setup venv. |

A degraded store is deliberate: a bus problem must surface as an error naming
the cause, never as "key missing" (which would make agents re-paste) and never
as a silent fallback to the local store.

## Do not

- Put any context database (or its WAL/SHM sidecars) on NFS/SMB/Syncthing/Dropbox.
- Put the PSK or bus token in an env var, MCP config JSON, tool argument, or
  log — the keyring/token **paths** in config are fine.
- Treat the `writer` label as authorization, or an AEAD/unseal failure as
  "missing".
- Trust the LAN transport for confidentiality — HTTP carries ciphertext; the
  seal is the boundary, not the wire.
- Hand-roll a cipher or KDF, use the PSK directly as a cipher key, or reuse a
  salt/nonce across writes.
