#!/usr/bin/env bash
# Render the README diagrams from the HTML sources in this directory.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$DIR/.."
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

render() {
  local src="$1" dest="$2" w="$3" h="$4"
  # 2x screenshot, then Lanczos downsample for sharp type.
  chromium --headless=new --disable-gpu --hide-scrollbars --no-first-run \
    --force-device-scale-factor=2 \
    --window-size="${w},${h}" \
    --default-background-color=00000000 \
    --screenshot="$TMP/raw.png" \
    "file://${src}"
  magick "$TMP/raw.png" -resize "${w}x${h}" "$dest"
}

render "$DIR/guard.html" "$OUT/guard_layers_modern.png" 1672 941
render "$DIR/system-map.html" "$OUT/ask-fable-system-map.png" 1672 941
render "$DIR/request-flow.html" "$OUT/ask-fable-request-flow.png" 1672 941
render "$DIR/hero.html" "$TMP/hero.png" 1672 560
magick "$TMP/hero.png" -quality 92 "$OUT/ask_fable_hero_banner.jpg"

identify \
  "$OUT/guard_layers_modern.png" \
  "$OUT/ask-fable-system-map.png" \
  "$OUT/ask-fable-request-flow.png" \
  "$OUT/ask_fable_hero_banner.jpg"
