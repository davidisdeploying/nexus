# Runic font slot — Nexus

The dashboard reserves a **runic-font slot** for the decorative Fleet lettering
on section eyebrows and small labels (the `.rune` class). **This build ships no
font and no glyphs** — the reskin fetched, generated, and reproduced nothing. The
font is a **user-supplied community asset**; David drops it in.

## How to enable the runic labels

1. Obtain a community "Fleet" / "rune" font as a **`.woff2`** file (your
   choice of source — licensing is yours to honor).
2. Save it at exactly:

   ```
   ~/nexus/static/fonts/nexus.woff2
   ```

3. Reload the page. The eyebrows/labels render in the runic face on next load.
   No restart needed — `/static` serves the file live.

## Wiring (already in place)

`templates/dashboard.html`:

```css
@font-face{font-family:'nexus';src:url('/static/fonts/nexus.woff2') format('woff2');font-display:swap}
.rune{font-family:'nexus','JetBrains Mono',monospace;letter-spacing:.05em}
```

- `/static/` is mounted in `app/main.py` from `~/nexus/static/`.
- `font-display:swap` + the **`JetBrains Mono` fallback** mean every `.rune`
  label stays fully legible until the `.woff2` exists — the page never shows
  blank/tofu text while the slot is empty.
- The public hostname is protected by Cloudflare Access, so the font follows
  the same authenticated edge path as the rest of Nexus. Adding the slot opened
  no separate public read surface.

## What `.rune` is applied to

Section eyebrows and small decorative labels **only** — never data values or node
names (those stay in JetBrains Mono / Fraunces so the data is always readable
regardless of the runic font).
