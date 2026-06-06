"""
assets/generate_icon.py
=======================
Generates assets/icon.ico — the PhoneTransfer Windows application icon.

Design:
  Dark background (#0D1117) with a blue phone-frame outline (#4FC3F7),
  matching the customtkinter blue theme and the Android companion app icon.

Run once before packaging:
    python assets/generate_icon.py

Requires Pillow:
    pip install pillow
"""

from __future__ import annotations

import sys
from pathlib import Path


def _draw_frame(size: int):
    """Return a PIL Image of the icon at the given square pixel size."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── Background: dark rounded square ──────────────────────────────────────
    bg_radius = max(4, size // 6)
    bg_color  = (13, 17, 23, 255)  # #0D1117
    draw.rounded_rectangle(
        [0, 0, size - 1, size - 1],
        radius=bg_radius,
        fill=bg_color,
    )

    # ── Phone-frame outline ───────────────────────────────────────────────────
    # The frame is a portrait rounded-rectangle centred in the icon.
    # Proportions  (as a fraction of `size`):
    #   horizontal margin  ~25 %  → frame width  = 50 % of canvas
    #   top margin         ~10 %  → frame height = 80 % of canvas
    # This gives an aspect ratio of 50:80 = 0.625, close to a smartphone.
    blue          = (79, 195, 247, 255)   # #4FC3F7
    h_margin      = round(size * 0.25)
    v_margin      = round(size * 0.10)
    frame_radius  = max(3, round(size * 0.10))
    stroke_width  = max(2, round(size * 0.055))

    x0, y0 = h_margin,          v_margin
    x1, y1 = size - h_margin,   size - v_margin

    draw.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=frame_radius,
        outline=blue,
        width=stroke_width,
    )

    return img


def generate(output_path: str | Path = "assets/icon.ico") -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("ERROR: Pillow is not installed.  Run:  pip install pillow")
        sys.exit(1)

    # Render at all standard ICO sizes; 256 is the master.
    sizes   = [16, 32, 48, 256]
    images  = [_draw_frame(s) for s in sizes]

    # PIL saves a multi-size .ico when append_images is provided.
    images[0].save(
        output_path,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[1:],
    )
    print(f"✓  Icon saved → {output_path.resolve()}")


if __name__ == "__main__":
    # Allow overriding output path from command line
    out = sys.argv[1] if len(sys.argv) > 1 else "assets/icon.ico"
    generate(out)
