# README diagrams — how they were made

A short note on the three technical diagrams (`hero-council.png`,
`tools-overview.png`, `guard-layers.png`) under `docs/img/`, so future regeneration is
reproducible.

## Current README explainers

The README's two wide explainers were generated with OpenAI's built-in image
generator and saved under `images/`:

- `ask-fable-system-map.png` shows the verified architecture: MCP client →
  context bus → guard → router → single/council/chain/debate → model routes →
  sidecar and answer, with audit/cache/traces/sessions below.
- `ask-fable-request-flow.png` shows one call: receive → resolve context → guard
  → cache lookup → run mode → normalize → persist → return. It includes the
  blocked guard exit and cache-hit shortcut.

Both use a 16:9 dark technical-infographic prompt, exact labels, unambiguous
arrows, and explicit instructions not to invent services or steps. Before
regenerating them, compare the topology with `build_server()` and its handlers
in `src/ask_fable/server.py`; the implementation is the source of truth.

## Pipeline

All four images were generated with **Seedream v5.0 Lite** on
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

- Resized to web-friendly widths (`1600px` for the hero banner,
  `1400px` for the square diagrams) with `Image.LANCZOS`.
- Re-saved as **PNG, `optimize=True`** (lossless, ~1MB each).
- The hero was cropped to its content band before resize; the
  square posters were not cropped.

## Prompts

The four prompts are reproduced below so any of them can be
re-rendered verbatim, tweaked, or replaced.

### `hero-council.png` — council fan-out banner

```
A wide, clean, flat-vector technical infographic for a software tool
called "ask-fable". Style: minimal modern flat illustration with subtle
gradient background (very dark navy #0f172a), soft glow accents in cyan
#22d3ee and violet #a78bfa. No photographic content, no characters, no
AI-style imagery.

Layout (left to right):

1. Left side: bold sans-serif wordmark "ask-fable" in white, large.
   Subtitle in light cyan: "Gated MCP server for external reasoning".
   Small caption beneath: "Fable · MiniMax · Gemini · GLM · DeepSeek ·
   Ollama".

2. Middle: a horizontal fan-out diagram. A single rounded rectangle on
   the left labeled "Your agent's question". Six small rounded
   rectangles branching out to the right, each with a distinct accent
   color and labeled: "claude-fable-5-1" (cyan), "MiniMax-M3" (violet),
   "Gemini 3.1 Pro" (amber), "GLM-5.2" (emerald), "deepseek-v4-pro"
   (rose), "Ollama Cloud" (sky blue). Thin connector lines from the
   source box to each oracle, with small arrows.

3. Right side: a single rounded rectangle labeled "Fable synthesizes"
   in white on a cyan-tinted card, with a downward arrow to a final
   rounded rectangle labeled "One merged answer". Small note in dim
   text below: "quorum · degraded · confidence".

Add small subtle dot-grid background pattern. No icons, no people, no
stock imagery. Crisp typography, readable labels, vector aesthetic.
```

### `tools-overview.png` — historical tool-inventory artwork (not the current inventory)

This documentation preserves the original 8-tool prompt and a later 19-tool
HTML source variant. Neither tracks the current 37-tool MCP surface; use
`README.md` or `CLAUDE.md` for the live inventory.

```
A clean, minimal, flat-vector technical infographic for a software tool
called "ask-fable". Style: modern flat illustration with a subtle dark
background (deep slate #1e293b), thin grid pattern, soft glow accents
in cyan #22d3ee and green #34d399.

Title at top in white sans-serif: "The tools". Subtitle in muted gray:
"8 tools, three families" (historical artwork; not a live tool count).

Layout: a 3-column x 3-row grid of rounded rectangle cards. Each card
has a bold 2-3 word label, a one-line description, and a small
accent-colored top border indicating family.

Column 1 (cyan, "single oracle"): ask · ask_m3 · ask_glm
Column 2 (violet, "council"):    ask_council · ask_ollama · ask_ollama_council
Column 3 (amber, "setup"):       ask_gemini · list_ollama_models · configure_ollama_council

Bottom: legend with 3 color swatches (cyan "single oracle", violet
"council", amber "setup") and a tiny footer.
```

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
