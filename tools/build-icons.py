#!/usr/bin/env python3
"""Regenerate the Nexus app icons from the new node-matrix mark.

Three SVG sources, each drawn for the surface it actually lands on rather than
one artwork exported three times:

  nexus-eye.svg           the rounded dark plate, used as the favicon and as
                          the 192/512 PNGs. Keeps the existing plate exactly:
                          inset 8, rx 104, #080d0f ground, #22383c 16px stroke.
  nexus-eye-maskable.svg  full bleed, no rounding, no stroke -- the platform
                          supplies the mask. The mark is inset to the safe
                          zone so Android's circle crop cannot clip it.
  nexus-eye-mask.svg      Safari pinned tab: bare shapes, no ground, no fill
                          attribute, so Safari can recolour it.

The 180 is rendered full bleed too. It is referenced only as apple-touch-icon,
and iOS applies its own rounded mask -- feeding it a pre-rounded plate would
round it twice and leave dark notches in the corners.

The icons take a fixed #57e0d8 rather than --eye-col: an icon is a static file
and cannot follow system status the way the in-app eye does.
"""

import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
OUT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "static/icons"
OUT.mkdir(parents=True, exist_ok=True)

GROUND, STROKE, MARK = "#080d0f", "#22383c", "#57e0d8"

# The mark in its own coordinate space: four cells, three hollow, one solid.
# Bounding box is x 0..88, y -100..-12 -- a square, 88 on a side.
CELLS = (
    '<path fill-rule="evenodd" d="M0,-100 h38 v38 h-38 Z M11,-89 h16 v16 h-16 Z"/>'
    '<path fill-rule="evenodd" d="M0,-50 h38 v38 h-38 Z M11,-39 h16 v16 h-16 Z"/>'
    '<path d="M50,-100 h38 v38 h-38 Z"/>'
    '<path fill-rule="evenodd" d="M50,-50 h38 v38 h-38 Z M61,-39 h16 v16 h-16 Z"/>'
)
BB_X0, BB_X1, BB_Y0, BB_Y1 = 0.0, 88.0, -100.0, -12.0


def place(fraction, canvas=512.0):
    """Centre the mark on the canvas at `fraction` of its width."""
    scale = canvas * fraction / (BB_X1 - BB_X0)
    tx = canvas / 2 - scale * (BB_X0 + BB_X1) / 2
    ty = canvas / 2 - scale * (BB_Y0 + BB_Y1) / 2
    return f"translate({tx:.6f} {ty:.6f}) scale({scale:.6f})"


HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'

# --- the rounded plate ----------------------------------------------------
PLATE = 0.56          # a solid block mark carries more weight than the old
                      # linework eye did at 0.76, so it is set smaller
plate = f"""{HEAD}
  <rect x="8" y="8" width="496" height="496" rx="104" fill="{GROUND}" stroke="{STROKE}" stroke-width="16"/>
  <g fill="{MARK}" transform="{place(PLATE)}">{CELLS}</g>
</svg>
"""

# --- full bleed, for the maskable and for the 180 -------------------------
# Android's safe zone is the inner 80% circle (409.6 across). A centred square
# of side S needs S * sqrt(2) <= 409.6, so S <= 289. 0.44 (225) sits well
# inside that with room for the platform's own padding.
MASKABLE = 0.44
maskable = f"""{HEAD}
  <rect width="512" height="512" fill="{GROUND}"/>
  <g fill="{MARK}" transform="{place(MASKABLE)}">{CELLS}</g>
</svg>
"""

touch = f"""{HEAD}
  <rect width="512" height="512" fill="{GROUND}"/>
  <g fill="{MARK}" transform="{place(PLATE)}">{CELLS}</g>
</svg>
"""

# --- Safari pinned tab: silhouette only -----------------------------------
mask = f"""{HEAD}
  <g transform="{place(0.88)}">{CELLS}</g>
</svg>
"""

(OUT / "nexus-eye.svg").write_text(plate)
(OUT / "nexus-eye-maskable.svg").write_text(maskable)
(OUT / "nexus-eye-mask.svg").write_text(mask)
(OUT / "_touch.svg").write_text(touch)


# --- rasterise ------------------------------------------------------------
def png_size(p):
    """Read width/height straight out of the PNG IHDR."""
    b = p.read_bytes()
    assert b[:8] == b"\x89PNG\r\n\x1a\n", f"{p.name} is not a PNG"
    return struct.unpack(">II", b[16:24])


def render(svg_path, size, out_name):
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "s.svg").write_bytes(svg_path.read_bytes())
        (td / "i.html").write_text(
            "<!doctype html><meta charset=utf-8>"
            "<style>html,body{margin:0;padding:0;overflow:hidden;background:transparent}"
            f"img{{display:block;width:{size}px;height:{size}px}}</style>"
            '<img src="s.svg">'
        )
        subprocess.run(
            ["chromium", "--headless=new", "--disable-gpu", "--no-sandbox",
             "--hide-scrollbars", "--force-device-scale-factor=1",
             "--default-background-color=00000000",
             f"--screenshot={td / 'o.png'}", f"--window-size={size},{size}",
             str(td / "i.html")],
            check=True, capture_output=True, timeout=120,
        )
        shutil.copy(td / "o.png", OUT / out_name)
    w, h = png_size(OUT / out_name)
    assert (w, h) == (size, size), f"{out_name}: got {w}x{h}, wanted {size}x{size}"
    return w, h


jobs = [
    ("_touch.svg",            180, "nexus-eye-180.png"),
    ("nexus-eye.svg",         192, "nexus-eye-192.png"),
    ("nexus-eye.svg",         512, "nexus-eye-512.png"),
    ("nexus-eye-maskable.svg", 512, "nexus-eye-maskable-512.png"),
]
for src, size, name in jobs:
    w, h = render(OUT / src, size, name)
    print(f"  {name:<28} {w}x{h}  {(OUT / name).stat().st_size:>6}B  <- {src}")

(OUT / "_touch.svg").unlink()

# the four compatibility names the test pins as byte-identical copies
for legacy, current in (
    ("apple-touch-icon-180.png", "nexus-eye-180.png"),
    ("icon-192.png",             "nexus-eye-192.png"),
    ("icon-512.png",             "nexus-eye-512.png"),
    ("icon-maskable-512.png",    "nexus-eye-maskable-512.png"),
):
    shutil.copy(OUT / current, OUT / legacy)
    print(f"  {legacy:<28} copy of {current}")

print(f"\n  plate transform: {place(PLATE)}")
