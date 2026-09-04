"""Budget policy for server-side context packing — the layer over ``safe_fs``.

``pack(specs, root)`` turns a list of path / ``path:START-END`` specs into ONE
labelled bundle ready for the context bus, plus a machine-parseable account of what
was included and what was skipped (and why). ``safe_fs`` owns "is this read safe";
this module owns "does it fit, in what order, and how is it framed".

Contract decisions the council pinned (breaking to change later, so fixed now):
- **Provenance header per block** (``=== path:start-end ===``); its chars count
  against ``max_chars`` — otherwise the budget lies.
- **Bundle order = input order**; admission *priority* (which specs win a tight
  budget) is separate: ranges before whole files, tiebreak by original index.
- **Partial packs are legible, not silent.** On budget overflow the offending spec
  and every later one land in ``skipped`` with ``budget_exhausted`` and
  ``complete=False``. The caller decides whether to write the partial bundle; a
  ``pack`` with zero admitted blocks yields an empty bundle and ``complete=False``
  so the handler can refuse to write it.
- **Subset-aware dedupe:** a range spec subsumed by a whole-file spec of the same
  file is dropped (its content is already present); exact-duplicate specs collapse.
  Overlapping-but-distinct ranges are kept (merging is a deferred decision).

``scrub`` and ``quality`` are identity/no-op seams called at pack time so the
phase-2 secret scrubber and context-quality signal drop in here without touching
the read path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import safe_fs
from .safe_fs import Reason, Resolved

DEFAULT_MAX_CHARS = 24_000
DEFAULT_MAX_FILES = 32


def max_chars_default() -> int:
    return _int_env("ASK_FABLE_PACK_MAX_CHARS", DEFAULT_MAX_CHARS)


def max_files_default() -> int:
    return _int_env("ASK_FABLE_PACK_MAX_FILES", DEFAULT_MAX_FILES)


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name) or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def scrub(text: str) -> str:
    """Seam for the phase-2 secret scrubber. Identity for now."""
    return text


def quality(bundle: str) -> dict:
    """Seam for the phase-2 context-quality signal. No signal for now."""
    return {}


@dataclass
class PackResult:
    bundle: str
    included: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    complete: bool = True
    chars: int = 0
    quality: dict = field(default_factory=dict)


def _label(r: Resolved) -> str:
    return f"{r.rel_path}:{r.start}-{r.end}" if r.start is not None else r.rel_path


def _dedupe(items: list[tuple[int, Resolved, str]]) -> list[tuple[int, Resolved, str]]:
    """Drop range specs subsumed by a whole-file spec of the same file, and collapse
    exact duplicates (keeping the first). Silent — the content is still present via
    the surviving spec, so nothing is 'skipped'."""
    whole = {r.abs_path for _, r, _ in items if r.start is None}
    seen: set[tuple[str, int | None, int | None]] = set()
    out: list[tuple[int, Resolved, str]] = []
    for idx, r, spec in items:
        if r.start is not None and r.abs_path in whole:
            continue
        sig = (r.abs_path, r.start, r.end)
        if sig in seen:
            continue
        seen.add(sig)
        out.append((idx, r, spec))
    return out


def pack(
    specs: list,
    root: str,
    *,
    max_chars: int | None = None,
    max_files: int | None = None,
    extra_blocklist: tuple[str, ...] = (),
) -> PackResult:
    """Resolve, dedupe, budget, and frame ``specs`` into one bundle. Never raises."""
    max_chars = max_chars if max_chars is not None else max_chars_default()
    max_files = max_files if max_files is not None else max_files_default()
    cap = safe_fs.max_file_bytes()

    skipped: list[dict] = []
    resolved: list[tuple[int, Resolved, str]] = []
    for idx, raw in enumerate(specs):
        spec = str(raw).strip()
        r = safe_fs.resolve_within(root, spec, extra_blocklist=extra_blocklist)
        if isinstance(r, Reason):
            skipped.append({"spec": spec, "reason": r.value})
        else:
            resolved.append((idx, r, spec))

    deduped = _dedupe(resolved)
    # Admission priority: ranges before whole files, tiebreak by original index.
    order = sorted(deduped, key=lambda t: (0 if t[1].start is not None else 1, t[0]))

    admitted: list[tuple[int, str, dict]] = []
    total = 0
    stopped = False
    for idx, r, spec in order:
        if stopped:
            skipped.append({"spec": spec, "reason": Reason.BUDGET_EXHAUSTED.value})
            continue
        if len(admitted) >= max_files:
            skipped.append({"spec": spec, "reason": Reason.MAX_FILES.value})
            continue
        out = safe_fs.read_content(r, max_bytes=cap)
        if isinstance(out, Reason):
            skipped.append({"spec": spec, "reason": out.value})
            continue
        content, _ = out
        block = f"=== {_label(r)} ===\n{scrub(content)}"
        if total + len(block) > max_chars:
            skipped.append({"spec": spec, "reason": Reason.BUDGET_EXHAUSTED.value})
            stopped = True
            continue
        total += len(block)
        admitted.append((idx, block, {"spec": spec, "path": r.rel_path, "chars": len(block)}))

    # Bundle order = input order (not admission order).
    admitted.sort(key=lambda t: t[0])
    bundle = "\n\n".join(block for _, block, _ in admitted)
    included = [meta for _, _, meta in admitted]
    return PackResult(
        bundle=bundle,
        included=included,
        skipped=skipped,
        complete=not skipped,
        chars=len(bundle),
        quality=quality(bundle),
    )
