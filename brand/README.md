# Brand assets

Source masters for the IPEDS Query logo (transparent PNG, no built-in padding).

- `icon.png` — 764×757 mark (chat bubble + bar chart + magnifier).
- `wordmark.png` — 1889×409 mark + "IPEDS Query" lockup.

## Derived assets (regenerate with ImageMagick)

Web wordmark (light = original, dark = navy lettering recolored light so it reads
on the dark theme's `--panel` background):

    convert brand/wordmark.png -resize x200 frontend/src/assets/wordmark.png
    convert brand/wordmark.png -fuzz 16% -fill '#e7edf6' -opaque '#1e3246' \
      -resize x200 frontend/src/assets/wordmark-dark.png

Favicons (slight padding so the busy mark isn't edge-cramped; apple-touch
flattened on white since iOS shows black behind transparency):

    convert brand/icon.png -bordercolor none -border 6%x6% \
      -define icon:auto-resize=64,48,32,16 frontend/public/favicon.ico
    convert brand/icon.png -bordercolor none -border 6%x6% -resize 32x32 frontend/public/favicon-32.png
    convert brand/icon.png -bordercolor none -border 9%x9% -background white -flatten \
      -resize 180x180 -gravity center -extent 180x180 frontend/public/apple-touch-icon.png
