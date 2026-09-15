"""Draw the mascot on the web app's masthead: two 3x3 pose grids, on the key.

The hero needs a character before anyone has generated one, and a placeholder
box there undersells a tool whose whole claim is that it produces art. This
draws one, and it goes through the same pipeline everything else does:

    uv run python spike/demo_mascot.py
    mascotify pose-ingest --job visor \
      --directions /tmp/art/directions.png --reactions /tmp/art/reactions.png
    cp .../visor-directions.webp src/mascotify/web/static/mascot-directions.webp
    cp .../visor-reactions.webp  src/mascotify/web/static/mascot-reactions.webp

Which is the point: the thing on the masthead is an export, not a mockup, and
it validates at 9/9 cells with 0% anchor drift like any other pair.

Supersampled 3x and downsampled — flat vector art with bold outlines is exactly
the case where aliased curves read as cheap. The palette is the UI's own accent
and a near-black visor, so the character sits in the page's light rather than
on top of it.
"""
import math
import os
import sys
from PIL import Image, ImageDraw

KEY = (0, 255, 0)
CREAM, CREAM_2 = (242, 234, 217), (214, 205, 186)
TEAL, TEAL_2 = (94, 233, 196), (47, 183, 154)
VISOR = (31, 38, 49)
LINE = (14, 17, 22)

CELL, SS = 320, 3
LW = 5 * SS

DIRS = [(-1, -1), (0, -1), (1, -1), (-1, 0), (0, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
REACTS = ["surprised", "happy", "laughing", "wink", "sleepy", "shy", "curious", "dizzy", "love"]


def rr(d, box, r, fill, outline=LINE, w=LW):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=w)


def eye(d, ex, ey, kind, hx):
    """The visor's glowing eye. Shape carries the whole expression."""
    r = 26 * SS
    if kind == "look":
        d.ellipse([ex - r, ey - r, ex + r, ey + r], fill=TEAL)
        d.ellipse([ex - r * .34 + hx, ey - r * .5, ex + r * .18 + hx, ey - r * .1], fill=(255, 255, 255))
    elif kind == "surprised":
        d.ellipse([ex - r * 1.3, ey - r * 1.3, ex + r * 1.3, ey + r * 1.3], fill=TEAL)
        d.ellipse([ex - r * .4, ey - r * .55, ex + r * .1, ey - r * .05], fill=(255, 255, 255))
    elif kind == "happy":
        d.arc([ex - r * 1.4, ey - r * .3, ex + r * 1.4, ey + r * 1.9], 190, 350, fill=TEAL, width=int(r * .55))
    elif kind == "laughing":
        d.chord([ex - r * 1.25, ey - r * 1.15, ex + r * 1.25, ey + r * 1.15], 15, 165, fill=TEAL)
    elif kind == "wink":
        d.line([ex - r, ey, ex + r, ey], fill=TEAL, width=int(r * .5))
    elif kind == "sleepy":
        d.chord([ex - r * 1.1, ey - r * .9, ex + r * 1.1, ey + r * 1.1], 0, 180, fill=TEAL)
    elif kind == "shy":
        d.arc([ex - r * 1.3, ey - r * .2, ex + r * 1.3, ey + r * 1.8], 195, 345, fill=TEAL, width=int(r * .5))
    elif kind == "curious":
        d.ellipse([ex - r * .8, ey - r * 1.1, ex + r * .8, ey + r * 1.1], fill=TEAL)
        d.ellipse([ex - r * .3, ey - r * .6, ex + r * .1, ey - r * .1], fill=(255, 255, 255))
    elif kind == "dizzy":
        for i in range(3):
            k = r * (1.25 - i * .34)
            d.arc([ex - k, ey - k, ex + k, ey + k], 40 + i * 110, 330 + i * 110, fill=TEAL, width=int(r * .26))
    elif kind == "love":
        for sx in (-1, 1):
            d.ellipse([ex + sx * r * .34 - r * .46, ey - r * .6, ex + sx * r * .34 + r * .46, ey + r * .3], fill=TEAL)
        d.polygon([ex - r * .78, ey - r * .06, ex + r * .78, ey - r * .06, ex, ey + r * .95], fill=TEAL)


def character(d, ox, oy, hdx, hdy, kind):
    """One cell. The body is nailed in place; only the head and face move."""
    cx = ox + CELL * SS // 2
    # The character is 324 units tall antenna to feet, and the cell is 320, so
    # it has to be scaled to leave the margin the validator insists on.
    S = SS * 0.66
    base = oy + int(CELL * SS * 0.84)          # the shared baseline
    hx, hy = hdx * 16 * S, hdy * 11 * S

    def y(v):
        return base - v * S

    # feet
    for s in (-1, 1):
        rr(d, [cx + s * 32 * S - 16 * S, y(26), cx + s * 32 * S + 16 * S, y(0)], 11 * S, CREAM_2)
    # arms, behind the torso so the shoulder line stays clean
    for s in (-1, 1):
        rr(d, [cx + s * 74 * S - 15 * S, y(106), cx + s * 74 * S + 15 * S, y(40)], 14 * S, CREAM_2)
    # neck, drawn before both so the torso and head cap its outline
    rr(d, [cx - 24 * S + hx, y(152), cx + 24 * S + hx, y(104)], 10 * S, CREAM_2)
    # torso
    rr(d, [cx - 66 * S, y(122), cx + 66 * S, y(22)], 36 * S, CREAM)
    rr(d, [cx - 32 * S, y(98), cx + 32 * S, y(60)], 13 * S, TEAL_2)
    # antenna, above the head so it is actually visible
    d.line([cx + hx, y(268), cx + hx, y(302)], fill=CREAM_2, width=int(LW * .9))
    d.ellipse([cx + hx - 12 * S, y(324), cx + hx + 12 * S, y(300)], fill=TEAL, outline=LINE, width=LW)
    # head
    rr(d, [cx - 80 * S + hx, y(286), cx + 80 * S + hx, y(146)], 48 * S, CREAM)
    rr(d, [cx - 61 * S + hx, y(258), cx + 61 * S + hx, y(176)], 31 * S, VISOR)
    if kind == "shy":
        for sx in (-1, 1):
            d.ellipse([cx + hx + sx * 68 * S - 11 * S, y(200), cx + hx + sx * 68 * S + 11 * S, y(182)],
                      fill=(240, 150, 150))
    eye(d, cx + hx + hdx * 26 * S, y(217) + hdy * 15 * S, kind, hdx * 6 * S)


def sheet(cells):
    n = CELL * SS
    img = Image.new("RGB", (3 * n, 3 * n), KEY)
    d = ImageDraw.Draw(img)
    for i, (hdx, hdy, kind) in enumerate(cells):
        r, c = divmod(i, 3)
        character(d, c * n, r * n, hdx, hdy, kind)
    return img.resize((3 * CELL, 3 * CELL), Image.LANCZOS)


def wave_sheet(rows: int = 3, cols: int = 4) -> Image.Image:
    """A motion grid: one arm sweeps, the rest of the body is nailed down.

    The same invariant `sheet_prompt` demands of a generator — only the motion
    changes between cells — so this validates the way a real generation should
    and is not a fixture that only passes because it was built to.
    """
    n = CELL * SS
    img = Image.new("RGB", (cols * n, rows * n), KEY)
    d = ImageDraw.Draw(img)
    S = SS * 0.66
    for i in range(rows * cols):
        r, c = divmod(i, cols)
        character(d, c * n, r * n, 0, 0, "look")
        cx, base = c * n + n // 2, r * n + int(n * 0.84)
        ang = -math.pi / 2 + math.sin(i / (rows * cols) * 2 * math.pi) * 1.15
        x0, y0 = cx + 74 * S, base - 104 * S
        x1, y1 = x0 + math.cos(ang) * 60 * S, y0 + math.sin(ang) * 60 * S
        d.line([x0, y0, x1, y1], fill=CREAM_2, width=int(30 * S))
        d.ellipse([x1 - 16 * S, y1 - 16 * S, x1 + 16 * S, y1 + 16 * S],
                  fill=CREAM_2, outline=LINE, width=LW)
    return img.resize((cols * CELL, rows * CELL), Image.LANCZOS)


def anchor() -> Image.Image:
    """The canonical still every sheet is generated from."""
    n = CELL * SS
    a = Image.new("RGB", (n, n), KEY)
    character(ImageDraw.Draw(a), 0, 0, 0, 0, "look")
    return a.resize((CELL, CELL), Image.LANCZOS)


# Guarded, so `character()` can be imported to draw other sheets without
# importing writing files as a side effect.
if __name__ == "__main__":
    OUT = os.environ.get("OUT", "/tmp/art")
    os.makedirs(OUT, exist_ok=True)
    if "--wave" in sys.argv:
        wave_sheet().save(f"{OUT}/wave.png")
        anchor().save(f"{OUT}/ref.png")
        print(f"drawn -> {OUT}/wave.png, {OUT}/ref.png")
    else:
        sheet([(dx, dy, "look") for dx, dy in DIRS]).save(f"{OUT}/directions.png")
        sheet([(0, 0, k) for k in REACTS]).save(f"{OUT}/reactions.png")
        print(f"drawn -> {OUT}/directions.png, {OUT}/reactions.png")
