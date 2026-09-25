#!/usr/bin/env bash
# Set up this machine as a client of the ask_fable LAN context bus.
#
# Run from a checkout:  bash scripts/setup-context-bus-client.sh [repo-path]
#
# Expects the fleet keyring + bus token either already installed at
# ~/.config/ask_fable/ (0600) or present next to this script as
# context_keyring / context_bus_token (0600) — e.g. an extracted client bundle.
# Merges `context_bus` into ~/.config/ask_fable/config.json (all other keys
# preserved) and verifies a real round-trip through the daemon.
set -euo pipefail

REPO="${1:-$PWD}"
CFG="$HOME/.config/ask_fable"
BUS="${BUS_URL:?set BUS_URL to the owner host, e.g. http://owner-host:8788}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ ! -x "$REPO/.venv/bin/python" ]; then
  echo "ERROR: no venv at $REPO/.venv — run 'cd $REPO && uv sync --extra lan' first" >&2
  exit 1
fi
if ! "$REPO/.venv/bin/python" -c "import ask_fable.context_bus" 2>/dev/null; then
  echo "ERROR: $REPO checkout is too old (needs the LAN bus code)" >&2
  exit 1
fi

install -d -m 700 "$CFG"
for f in context_keyring context_bus_token; do
  if [ -f "$HERE/$f" ]; then
    install -m 600 "$HERE/$f" "$CFG/$f"
  fi
  if [ ! -f "$CFG/$f" ]; then
    echo "ERROR: missing $CFG/$f (and no $f next to this script)" >&2
    exit 1
  fi
  chmod 600 "$CFG/$f"
done
stat -c '%n mode=%a' "$CFG/context_keyring" "$CFG/context_bus_token"

"$REPO/.venv/bin/python" - <<PY
import json, os
p = os.path.expanduser("~/.config/ask_fable/config.json")
cfg = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
cfg["context_bus"] = "$BUS"
tmp = p + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, p)
print("config context_bus set; keys:", sorted(cfg))
PY

"$REPO/.venv/bin/python" - <<'PY'
from ask_fable import context_bus, context_store
st = context_bus.bus_status()
print("bus status:", st)
assert st.get("reachable") and st.get("proto") == 1, "bus unreachable — check network/tailscale"
assert context_store.put("fleet:smoke", "HELLO_FROM_NEW_CLIENT", "client setup smoke")
assert context_store.get("fleet:smoke") == "HELLO_FROM_NEW_CLIENT"
print("round-trip through the bus: OK")
PY
echo
echo "Done. Restart your MCP harnesses (opencode / Claude Code) to pick up the bus."
