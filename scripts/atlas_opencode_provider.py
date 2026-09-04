#!/usr/bin/env python3
"""Emit the opencode custom-provider block for Atlas Cloud coding models.

This is the **Option B / curated-subset** tool from docs/OPENCODE.md §9 — for
when you want only specific models in opencode's picker rather than the full
catalog. For most users the recommended path is the `@atlascloudai/opencode`
plugin (Option A), which auto-registers all models; this generator exists for
the curated case.

Atlas Cloud speaks the standard OpenAI-compatible ``/v1/chat/completions``
shape (see ``src/ask_fable/atlas.py``), so opencode can register it as a native
``@ai-sdk/openai-compatible`` provider. opencode does NOT auto-discover models
for a *manually-defined* custom provider (and Atlas's catalog lives at
``/api/v1/models`` — note the ``/api`` prefix — not the ``/v1/models`` opencode
would probe), so an explicit ``models`` map is required. This script builds that
map from the live catalog, keeping the curated names stable and refreshing
ctx/cost metadata as JSONC comments so the block stays accurate as Atlas updates
its offerings.

Run from the repo (the package is installed editable):

    .venv/bin/python scripts/atlas_opencode_provider.py            # print the block
    .venv/bin/python scripts/atlas_opencode_provider.py --check    # CI drift check

The ``ALLOWLIST`` below is the single source of truth for which models appear.
Edit it (and the tier ordering) to change the menu. Re-running never reorders
existing entries, so diffs stay minimal and reviewable.
"""

from __future__ import annotations

import argparse
import sys

from ask_fable import atlas

# Curated allowlist: (model_id, display_name, tier). Ordered S → A → B so the
# output is stable and the best agentic-coding models surface first. Only models
# good enough to *drive the opencode agent* belong here — chat quality is not
# the filter, tool-call reliability is (see docs/OPENCODE.md §9). Expect to trim
# this after smoke-testing each model's streamed tool loop.
ALLOWLIST: list[tuple[str, str, str]] = [
    # Tier S — agentic coding specialists (best bet as the primary opencode model)
    ("minimaxai/minimax-m3", "MiniMax M3", "S"),
    ("moonshotai/kimi-k2.7-code", "Kimi K2.7 Code", "S"),
    ("zai-org/glm-5.2", "GLM 5.2", "S"),
    ("kwaipilot/kat-coder-pro-v2.5", "KAT Coder Pro V2.5", "S"),
    ("xai/grok-build-0.1", "Grok Build 0.1", "S"),
    # Tier A — strong flagship reasoners known for coding
    ("xai/grok-4.5", "Grok 4.5", "A"),
    ("qwen/qwen3.7-max", "Qwen3.7 Max", "A"),
    ("openai/gpt-5.6-sol", "GPT 5.6 Sol", "A"),
    ("deepseek-ai/deepseek-v4-pro", "DeepSeek V4 Pro", "A"),
    ("anthropic/claude-sonnet-4.6", "Claude Sonnet 4.6", "A"),
    # Tier B — cheap/fast workhorses (good small_model candidates)
    ("kwaipilot/kat-coder-air-v2.5", "KAT Coder Air V2.5", "B"),
    ("deepseek-ai/deepseek-v4-flash", "DeepSeek V4 Flash", "B"),
    ("qwen/qwen3.7-plus", "Qwen3.7 Plus", "B"),
    ("bytedance/doubao-seed-2.0-code-preview-260215", "Doubao Seed 2.0 Code", "B"),
    ("xiaomi/mimo-v2.5-pro", "MiMo V2.5 Pro", "B"),
]

# The static provider options. ``apiKey`` uses opencode's ``{env:VAR}``
# substitution so it reuses the same key ask_fable already reads. The custom
# ``User-Agent`` mirrors ``atlas.py``: Atlas's edge WAF returns HTTP 403 to some
# default SDK user-agents.
OPTIONS_BLOCK = """      "options": {
        "baseURL": "https://api.atlascloud.ai/v1",
        "apiKey": "{env:ATLASCLOUD_API_KEY}",
        "headers": { "User-Agent": "opencode-atlas/1.0 (+https://github.com/baggybin/ask-fable)" }
      },"""


def _model_lines(catalog_ok: bool, by_id: dict[str, dict]) -> list[str]:
    """One JSONC entry per allowlisted model: a metadata comment + the entry."""
    lines: list[str] = []
    for model_id, display_name, tier in ALLOWLIST:
        row = by_id.get(model_id)
        meta = _meta_comment(row) if row else "not in live catalog — may have been renamed/removed"
        lines.append(f"      // [tier {tier}] {meta}".rstrip())
        lines.append(f'      "{model_id}": {{ "name": "{display_name}" }},')
    if lines:
        # Strip the trailing comma on the final model entry — safe JSONC.
        lines[-1] = lines[-1].rstrip(",")
    return lines


def _meta_comment(row: dict) -> str:
    """Concise metadata tail from a catalog menu item: tags · context · price.

    Uses only structured fields (the ones ``atlas._model_item`` normalized) — the
    free-text profile is marketing prose and bloats the comment, so it's omitted."""
    tags = [t for t in row.get("tags") or [] if t != "LLM"]
    tag_str = f"[{', '.join(tags)}] · " if tags else ""
    ctx = row.get("context_length")
    ctx_str = f"{ctx // 1000}k ctx" if isinstance(ctx, int) else "? ctx"
    inp, out = row.get("input_price"), row.get("output_price")
    price_str = f" · ${inp}/${out} per M" if (inp and out) else ""
    return f"{tag_str}{ctx_str}{price_str}"


def emit(catalog_ok: bool, by_id: dict[str, dict]) -> str:
    lines = [
        "{",
        '  "$schema": "https://opencode.ai/config.json",',
        '  "provider": {',
        '    "atlas": {',
        '      "npm": "@ai-sdk/openai-compatible",',
        '      "name": "Atlas Cloud",',
        OPTIONS_BLOCK,
        '      "models": {',
    ]
    lines.extend(_model_lines(catalog_ok, by_id))
    lines.append("      }")
    lines.append("    }")
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Drift check: exit 1 if any allowlisted model_id is missing from the "
        "live catalog (use in CI to catch rename/removal).",
    )
    args = parser.parse_args()

    cat = atlas.catalog()
    catalog_ok = bool(cat.get("cloud_ok"))
    menu = cat.get("menu") or []
    by_id = {
        item["model_id"]: item for item in menu if isinstance(item, dict) and item.get("model_id")
    }

    if not catalog_ok:
        print(
            "warning: Atlas catalog unreachable — emitting block with stale/empty metadata",
            file=sys.stderr,
        )

    missing = [mid for mid, _, _ in ALLOWLIST if mid not in by_id]
    if args.check:
        if missing:
            print(
                f"drift: {len(missing)} allowlisted model(s) not in live catalog:", file=sys.stderr
            )
            for mid in missing:
                print(f"  - {mid}", file=sys.stderr)
            return 1
        print(f"ok: all {len(ALLOWLIST)} allowlisted models present in live catalog")
        return 0

    if missing:
        print(
            f"note: {len(missing)} allowlisted model(s) not in live catalog: {', '.join(missing)}",
            file=sys.stderr,
        )

    print(emit(catalog_ok, by_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
