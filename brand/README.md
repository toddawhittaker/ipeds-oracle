# Brand assets

Source masters for the **IPEDS Oracle** identity — the "Reading Room / Ink &
Vellum" system (teal fountain-pen ink `--accent`, archival ochre `--ochre`).

- `icon.svg` — the **vector master**: the Column mark (fluted teal shaft between an
  ochre capital and base). Colors are baked (`#166b62` teal, `#a66a12` ochre) so the
  standalone favicon reads on both light and dark browser tab bars.
- `icon.png` — 512-ish transparent raster of `icon.svg`, kept as the input to the
  favicon recipe below.

The **header/login wordmark is not a file** — it's an inline SVG + type lockup in
`frontend/src/Wordmark.jsx`, drawn straight from the theme tokens so light and dark
come from one source (mono "IPEDS" · ochre hairline · serif "Oracle" · the Column).
There is no longer a `wordmark.png` / `wordmark-dark.png` pair.

## Regenerating

Redraw `icon.svg`, then rebuild the raster + favicons (needs ImageMagick with the
`rsvg` SVG delegate):

    convert -background none brand/icon.svg -resize 768x768 brand/icon.png

    # Favicons (slight padding so the mark isn't edge-cramped; apple-touch
    # flattened on white since iOS shows black behind transparency).
    convert brand/icon.png -bordercolor none -border 6%x6% \
      -define icon:auto-resize=64,48,32,16 frontend/public/favicon.ico
    convert brand/icon.png -bordercolor none -border 6%x6% -resize 32x32 frontend/public/favicon-32.png
    convert brand/icon.png -bordercolor none -border 9%x9% -background white -flatten \
      -resize 180x180 -gravity center -extent 180x180 frontend/public/apple-touch-icon.png

To change the wordmark lockup itself (proportions, colors), edit
`frontend/src/Wordmark.jsx` and its `.wordmark` styles in `frontend/src/styles.css`.
