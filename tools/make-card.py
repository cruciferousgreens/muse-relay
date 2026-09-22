#!/usr/bin/env python3
"""Generate public/card.png — the generic share card for Muse Relay.

Hand-made with PIL primitives only (no AI imagery, per project rule).
Generic by design: brand mark + name + tagline. Never contains space keys,
link fragments, codes, member names, or message content.
"""
import os
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "public", "card.png")

W, H = 1200, 630
BG = "#faf4ed"
INK = "#1c1c1e"
MUTED = "#6c6c70"
ACCENT = "#1e5a8a"
LINE = "#dfdad9"
PAPER = "#faf4ed"

SERIF_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"
SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def main():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Thin frame.
    d.rectangle([28, 28, W - 28, H - 28], outline=LINE, width=3)

    # Brand mark: ink-blue rounded square with a paper speech bubble + 3 dots.
    mx, my, ms = 110, 195, 240
    d.rounded_rectangle([mx, my, mx + ms, my + ms], radius=52, fill=ACCENT)
    # Bubble: rounded rect + tail, paper colored.
    bx, by, bw, bh = mx + 52, my + 62, 136, 104
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=34, fill=PAPER)
    d.polygon([(bx + 26, by + bh - 14), (bx + 8, by + bh + 34), (bx + 62, by + bh - 10)],
              fill=PAPER)
    # Three ink-blue dots.
    for cx in (bx + 44, bx + 68, bx + 92):
        d.ellipse([cx - 9, by + 43, cx + 9, by + 61], fill=ACCENT)

    # Wordmark + tagline.
    f_title = ImageFont.truetype(SERIF_BOLD, 104)
    f_tag = ImageFont.truetype(SANS, 44)
    f_sub = ImageFont.truetype(SANS, 32)
    tx = 420
    d.text((tx, 200), "Muse Relay", font=f_title, fill=INK)
    d.text((tx, 340), "Your Muse can talk to their Muse.", font=f_tag, fill=MUTED)
    d.text((tx, 420), "End-to-end encrypted \u00b7 No accounts", font=f_sub, fill=MUTED)

    img.save(OUT)
    print(f"wrote {OUT} ({os.path.getsize(OUT)//1024} KB)")


if __name__ == "__main__":
    main()
