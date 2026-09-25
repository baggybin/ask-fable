"""Read the lmstudio.example.com Control panel snapshot over its JSON endpoint.

The control page (``service:controlpage`` on lmstudio.example.com) samples the whole box —
nvidia-smi, systemd services, Ollama, LM Studio, VRAM holders — into one
snapshot and renders it as an HTML fragment. ``GET /api/state.json`` exposes the
same dict for machine consumers (added 2026-09-13). Nothing here is required
for ask_fable to work: every reader is best-effort and returns None / empty on
any failure, so callers degrade to their own heuristics when the page is absent.

Used by the ``host_status`` tool (GPU/host view) and by the LM Studio bridge's
pre-load free-VRAM room check.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

DEFAULT_CONTROL_URL = "http://192.0.2.10:5000"

_opener: urllib.request.OpenerDirector | None = None

# Host-identity cache for ``monitors``: (control host, target host) -> (monotonic
# stamp, verdict). Brief, so a series of load attempts is not a DNS storm while
# a box moving on the network is still noticed quickly.
_MONITOR_TTL = 60.0
_MONITOR_CACHE: dict[tuple[str, str], tuple[float, bool]] = {}


def base_url() -> str:
    """The control page root: config ``control_page`` → ``ASK_FABLE_CONTROL_URL``
    → the fixed default (its IP)."""
    raw = config.get_str("control_page") or os.environ.get(
        "ASK_FABLE_CONTROL_URL"
    ) or DEFAULT_CONTROL_URL
    return raw.strip().rstrip("/")


def configured() -> bool:
    return bool(base_url())


def _addresses(host: str) -> frozenset[str]:
    """Every address ``host`` resolves to; empty when it does not resolve."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return frozenset()
    return frozenset(str(info[4][0]) for info in infos if info[4])


def monitors(host: str) -> bool:
    """Does the control page describe the SAME MACHINE as ``host``?

    The page has one fixed URL (lmstudio.example.com), so a reader pointed at a different
    box must not do VRAM math with this box's numbers — that is the exact
    false confidence the room check exists to prevent. Hosts are compared by
    resolution (a name and its IP are the same machine); a mismatch, an
    unresolvable host, or a resolution failure is a NO, so callers degrade to
    "unknown" rather than to a wrong verdict. Brief cache: see _MONITOR_TTL.
    """
    ctl_host = (urllib.parse.urlparse(base_url()).hostname or "").lower()
    host = (host or "").lower()
    if not host or not ctl_host:
        return False
    if ctl_host == host:
        return True
    key = (ctl_host, host)
    now = time.monotonic()
    hit = _MONITOR_CACHE.get(key)
    if hit is not None and now - hit[0] < _MONITOR_TTL:
        return hit[1]
    ok = bool(_addresses(ctl_host) & _addresses(host))
    _MONITOR_CACHE[key] = (now, ok)
    return ok


def _get_opener() -> urllib.request.OpenerDirector:
    """Proxies bypassed: the control page is a LAN host."""
    global _opener
    if _opener is None:
        _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _opener


def status(timeout: float = 5.0) -> dict | None:
    """The full snapshot dict, or None when the page is unreachable/not JSON."""
    req = urllib.request.Request(
        f"{base_url()}/api/state.json", headers={"accept": "application/json"}
    )
    try:
        with _get_opener().open(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
    except (
        urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError,
        UnicodeDecodeError,
    ):  # HTTPException: a body cut short (IncompleteRead) is "unreachable", not a crash
        return None
    return obj if isinstance(obj, dict) else None


def _int(value: object) -> int | None:
    return value if type(value) is int else None


def gpu(snapshot: dict | None = None, *, timeout: float = 2.5) -> dict:
    """Normalized GPU block: usage, VRAM used/total/free, temp, power.

    ``available`` is False (with the other fields None/0) whenever the page or
    the GPU reading is missing — callers must treat that as "unknown", never as
    "plenty of room"."""
    snap = snapshot if snapshot is not None else (status(timeout) or {})
    g = snap.get("gpu") or {}
    used = _int(g.get("vram_used_mib"))
    total = _int(g.get("vram_total_mib"))
    free = total - used if (used is not None and total) else None
    return {
        "available": bool(g.get("available")) and total is not None,
        "name": str(g.get("name") or ""),
        "util_pct": _int(g.get("util_pct")),
        "vram_used_mib": used,
        "vram_total_mib": total,
        "vram_free_mib": free,
        "temp_c": _int(g.get("temp_c")),
        "fan_pct": _int(g.get("fan_pct")),
        "power_w": g.get("power_w") if isinstance(g.get("power_w"), (int, float)) else None,
        "power_limit_w": (
            g.get("power_limit_w") if isinstance(g.get("power_limit_w"), (int, float)) else None
        ),
        "snapshot_age_s": snap.get("snapshot_age_s"),
    }


def summarize(snapshot: dict) -> dict:
    """Curated host view for the ``host_status`` tool — GPU, who holds VRAM,
    service states, the loaded LM Studio set, and the LiteLLM default-key
    warning. Raw enough to be useful, small enough to read."""
    services = snapshot.get("services") or {}
    active = sorted(k for k, v in services.items() if v == "active")
    inactive = sorted(k for k, v in services.items() if v != "active")
    ls = snapshot.get("lmstudio") or {}
    loaded = ls.get("loaded_models") or ([ls["loaded_model"]] if ls.get("loaded_model") else [])
    ollama = snapshot.get("ollama") or {}
    litellm = snapshot.get("litellm") or {}
    warnings: list[str] = []
    if litellm.get("is_default"):
        warnings.append("LiteLLM master key is the default (rotate it)")
    return {
        "gpu": gpu(snapshot),
        "vram_holders": [
            {
                "process": str(h.get("process") or ""),
                "vram_mib": _int(h.get("vram_mib")),
                "pid": _int(h.get("pid")),
            }
            for h in (snapshot.get("vram_holders") or [])
            if isinstance(h, dict)
        ],
        "services": {"active": active, "inactive": inactive},
        "lmstudio": {
            "active": ls.get("active"),
            "engine_up": ls.get("engine_up"),
            "port": ls.get("port"),
            "loaded_models": loaded,
            "models_count": len(ls.get("models") or []),
        },
        "ollama": {
            "status": ollama.get("status"),
            "installed_count": len(ollama.get("installed_models") or []),
        },
        "mountain": ((snapshot.get("mountain") or {}).get("health")) or {},
        "warnings": warnings,
        "snapshot_age_s": snapshot.get("snapshot_age_s"),
    }
