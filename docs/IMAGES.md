# README diagrams — how they were made

A short note so future regeneration is reproducible.

> **Note (2026-09-13):** the four README diagrams (hero, system map,
> request flow, guard) are HTML, not generated infographics. Sources
> live in `images/src/`; render with `images/src/render.sh`. Image
> models invent labels (JWT, rate limits, "ethics council"), so any
> diagram whose boxes have to be *true* is drawn in HTML/CSS and
> screenshotted.
>
> In use today: `ask_fable_hero_banner.jpg`, `ask-fable-system-map.png`,
> `ask-fable-request-flow.png`, `guard_layers_modern.png`,
> `ask_atlas_new.jpg`, and the mermaid mode charts
> (`ask_core`/`ask_council`/`ask_chain`/`ask_debate`/`guard_layers`, built by
> `generate_diagrams.py`).

## README hero and guard (HTML)

- `images/src/hero.html` is a short product banner (1672×560): wordmark,
  subtitle, and the six real modes around a Fable hub. No generated
  atmosphere plate.
- `images/src/guard.html` is the three-layer guard as it actually works:
  (1) sanity floor and (2) prohibited-use denylist run locally before
  any model call; (3) the model scope contract is Fable's system prompt
  at inference. `check()` in `src/ask_fable/guard.py` is layers 1–2 only.

```bash
images/src/render.sh
```

That writes the hero (1672×560) and the three diagrams (1672×941)
from a 2× screenshot, Lanczos downsample. Needs `chromium` and
ImageMagick `magick`.

## README system map and request flow (HTML)

Same pipeline as the hero and guard (`images/src/render.sh`). Compare
topology with `build_server()` in `src/ask_fable/server.py` before editing.

- `images/src/system-map.html` → `ask-fable-system-map.png`: MCP client →
  context bus → guard → router → Ask / Council / Chain / Debate /
  Falsify / Conference → Fable, Opus 5, MiniMax, Gemini, Codex, Grok, GLM,
  DeepSeek, Kimi, Ollama, Atlas, OpenRouter → sidecar + answer.
  Audit / cache / traces / sessions sit under the server.
- `images/src/request-flow.html` → `ask-fable-request-flow.png`: receive →
  resolve context → guard (blocked exit) → cache lookup (hit shortcut
  to return) → run mode (all six) → normalize answer+sidecar →
  persist → return with `trace_id`.

## Pipeline (historical — `docs/img/` leftovers)

Do not use this path for the README diagrams; those are HTML
(see above). The leftover `docs/img/` artwork (old
`guard-layers.png`) was generated with **Seedream v5.0 Lite** on
**Atlas Cloud** (`bytedance/seedream-v5.0-lite`, ~$0.003 each on the
90% discount). The model was chosen because it is explicitly tuned
for typography and poster design — every diagram here is mostly
labeled boxes and arrows, so label fidelity matters more than
photorealism.

Generation was done in two passes:

1. Submit with `atlas_generate_image` (or `atlas_quick_generate`
   when you don't know the model id).
2. Poll `atlas_get_prediction` with the returned prediction id
   until `status == "completed"`; the response carries a URL that
   `curl -sSL` downloads.

Seedream returned every prompt at **3072×3072** regardless of the
requested `size` (16:5 hints were ignored). All four images were then
post-processed with Pillow in `.venv`:

- Resized to web-friendly widths (`1400px` for the square diagrams) with `Image.LANCZOS`.
- Re-saved as **PNG, `optimize=True`** (lossless, ~1MB each).

## Prompts

The prompts are reproduced below so any of them can be
re-rendered verbatim, tweaked, or replaced.

### `guard-layers.png` — three-layer guard

```
A clean, minimal, flat-vector technical infographic. Style: modern
flat illustration with a very dark background (near-black #0b1220),
subtle grid, soft glow accents.

Title at top: "The guard — three layers before any model call".
Subtitle: "Each layer is independent. All three run, every call, in
this order."

Three stacked horizontal bands:
1. Sanity floor  (cyan) — empty / too-short / too-long; context
   unbounded, floored to 512,000 chars.
2. Prohibited-use denylist (amber) — bundled fallback or
   salient-core's check_prompt_intent; extend via
   ASK_FABLE_DENYLIST_FILE.
3. Model scope contract (green) — Fable system prompt, REFUSED for
   cyber/attack and non-software domains.

Footer: audit log note (hashed by default, ASK_FABLE_AUDIT_RAW=1 to
store raw).
```

## Re-rendering

To regenerate any image:

```bash
# 1. Submit (one shot per image)
atlas_generate_image model="bytedance/seedream-v5.0-lite" \
  params={ "prompt": "<paste above>", "size": "3072*3072",
           "output_format": "png" }

# 2. Poll until status == "completed", then curl the output URL to
#    docs/img/<name>.png.

# 3. Resize + optimize
python -c "
from PIL import Image
im = Image.open('docs/img/<name>.png')
nw = 1600 if 'hero' in '<name>' else 1400
nh = int(im.size[1] * (nw / im.size[0]))
im.resize((nw, nh), Image.LANCZOS).save(
    'docs/img/<name>.png', 'PNG', optimize=True)
"
```

Tip: the same prompt usually produces slightly different label
positions each run. If a label lands awkwardly, regenerate once or
two more times before tweaking the prompt — re-rendering is cheap.

## Future ideas (not done yet)

- A short 16:9 diagram for the GitHub social preview card
  (`docs/img/social.png`, 1280×640) using the same palette as the
  hero.
- An SVG version of the hero for crisp rendering at any zoom —
  Seedream's output is raster, so very high-DPI displays will
  upscale; an SVG hand-built from the same labels would be sharper
  and infinitely smaller.
- An animated `docs/img/hero-council.svg` (just a CSS animation on
  the connector arrows) for the opencode homepage.
