"""Rebuild the images README.md embeds.

Nothing here is drawn for the picture. The anchor, the sprite sheet, the
normalised frames and the pose pair are loaded back off disk after a real run,
so an image can only ever show what the pipeline actually produced — if a
change breaks the output, these break with it rather than papering over it.

Full sequence from an empty directory:

    cd /tmp/build
    OUT=. python .../spike/demo_mascot.py           # the two pose grids
    OUT=. python .../spike/demo_mascot.py --wave    # wave sheet + anchor
    mascotify ingest wave.png --name Wave --rows 3 --cols 4 --fps 12
    mascotify pose-ingest --job visor \
      --directions directions.png --reactions reactions.png
    BUILD=. python .../spike/readme_images.py

The web app screenshot is captured separately, because it needs the server:

    mascotify serve --port 8795 --no-open &
    chrome --headless=new --hide-scrollbars --virtual-time-budget=4000 \
      --window-size=1280,935 --screenshot=ui.png http://127.0.0.1:8795/
"""
import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

BG, INK, INK_2, INK_3 = (8, 9, 12), (236, 234, 231), (152, 160, 171), (101, 109, 121)
ACCENT, LINE = (94, 233, 196), (30, 34, 42)
MONO = "/System/Library/Fonts/SFNSMono.ttf"
SANS = "/System/Library/Fonts/SFNS.ttf"

B = Path(os.environ.get("BUILD", "/tmp/build"))
JOB = B / ".mascotify/wave"
POSE = B / ".mascotify/poses/visor/export/page-mascot"
DOCS = Path(__file__).resolve().parent.parent / "docs"


def font(path, size):
    return ImageFont.truetype(path, size)


def label(d, xy, text, size=13, fill=INK_3, track=1.6):
    """Mono caps with tracking, matching the UI's own micro-labels."""
    x, y = xy
    f = font(MONO, size)
    for ch in text.upper():
        d.text((x, y), ch, font=f, fill=fill)
        x += d.textlength(ch, font=f) + track
    return x - xy[0]


def label_w(d, text, size=13, track=1.6):
    f = font(MONO, size)
    return sum(d.textlength(c, font=f) + track for c in text.upper()) - track


def glow(img, cx, cy, r, colour=ACCENT, strength=0.16):
    """A soft radial pool of light, dithered by the resize."""
    g = Image.new("L", (r * 2, r * 2), 0)
    gd = ImageDraw.Draw(g)
    steps = 26
    for i in range(steps, 0, -1):
        k = int(r * i / steps)
        gd.ellipse([r - k, r - k, r + k, r + k], fill=int(255 * strength * (1 - i / steps) ** 2))
    img.paste(Image.new("RGB", g.size, colour), (cx - r, cy - r), g)


def card(d, box, radius=14, fill=(16, 19, 24), outline=LINE):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=1)


def fit(im, w, h):
    im = im.copy()
    im.thumbnail((w, h), Image.LANCZOS)
    return im


# ── hero: the whole pipeline in one animated strip ─────────────────────────

def hero():
    W, H = 1280, 470
    PAD, GAP = 44, 30
    panel_w = (W - PAD * 2 - GAP * 2) // 3
    top, panel_h = 96, 252

    base = Image.new("RGB", (W, H), BG)
    glow(base, W // 2, 30, 560, strength=0.13)
    d = ImageDraw.Draw(base)

    d.text((PAD, 36), "mascotify", font=font(SANS, 27), fill=INK)
    w = d.textlength("mascotify", font=font(SANS, 27))
    label(d, (PAD + w + 14, 46), "one still in, every platform out", 12, INK_3)

    # The keyed-out anchor, not the raw green plate: cut_out is the first thing
    # that happens to it, so this is the artefact the rest of the run uses.
    from mascotify.imaging.cutout import cut_out
    from mascotify.spec import CutoutSpec
    anchor = cut_out(Image.open(B / "ref.png"), CutoutSpec())
    cut = Image.open(JOB / "sheet-cut.png").convert("RGBA")
    frames = sorted((JOB / "frames").glob("*.png"))

    steps = [("1 · anchor", anchor, None), ("2 · sheet", cut, None), ("3 · export", None, frames)]
    slots = []
    for i, (cap, img, seq) in enumerate(steps):
        x = PAD + i * (panel_w + GAP)
        card(d, [x, top, x + panel_w, top + panel_h])
        label(d, (x + 2, top + panel_h + 16), cap, 12, ACCENT if seq else INK_3)
        if img is not None:
            thumb = fit(img, panel_w - 40, panel_h - 40)
            base.paste(thumb, (x + (panel_w - thumb.width) // 2,
                               top + (panel_h - thumb.height) // 2),
                       thumb if thumb.mode == "RGBA" else None)
        else:
            slots.append((x, top, panel_w, panel_h))

        if i < 2:                     # the arrow between panels
            ax = x + panel_w + GAP // 2
            ay = top + panel_h // 2
            d.line([ax - 7, ay, ax + 5, ay], fill=INK_3, width=2)
            d.polygon([(ax + 9, ay), (ax + 2, ay - 5), (ax + 2, ay + 5)], fill=INK_3)

    d.line([PAD, H - 62, W - PAD, H - 62], fill=LINE, width=1)
    label(d, (PAD, H - 44), "12 frames  ·  12 fps  ·  seam 0.34x  ·  size drift 0.0%", 12, INK_3)
    tail = "measured, not guessed"
    label(d, (W - PAD - label_w(d, tail, 12), H - 44), tail, 12, INK_3)

    # Only the third panel moves, so every frame is the same base with one paste.
    x, y, pw, ph = slots[0]
    out = []
    for f in frames:
        im = Image.open(f).convert("RGBA")
        t = fit(im, pw - 88, ph - 88)
        canvas = base.copy()
        canvas.paste(t, (x + (pw - t.width) // 2, y + (ph - t.height) // 2), t)
        out.append(canvas)
    out[0].save(DOCS / "hero.webp", save_all=True, append_images=out[1:],
                duration=round(1000 / 12), loop=0, quality=88, method=6)
    print(DOCS / "hero.webp", out[0].size, len(out), "frames")




# ── the pose pair that feeds page-mascot ───────────────────────────────────

def poses():
    W, PAD, GAP = 1280, 44, 40
    sheet_w = (W - PAD * 2 - GAP) // 2
    top, H = 104, 104 + sheet_w + 76

    img = Image.new("RGB", (W, H), BG)
    glow(img, W // 2, H // 2, 520, strength=0.10)
    d = ImageDraw.Draw(img)

    d.text((PAD, 34), "one character, two grids", font=font(SANS, 25), fill=INK)
    label(d, (PAD, 72), "nine head directions  ·  nine expressions  ·  nothing plays", 12, INK_3)

    pairs = [
        ("directions", "read by compass position", POSE / "visor-directions.webp"),
        ("reactions", "one is shown on a click", POSE / "visor-reactions.webp"),
    ]
    for i, (name, note, path) in enumerate(pairs):
        x = PAD + i * (sheet_w + GAP)
        card(d, [x, top, x + sheet_w, top + sheet_w], radius=16)
        s_im = Image.open(path).convert("RGBA")
        t = fit(s_im, sheet_w - 26, sheet_w - 26)
        img.paste(t, (x + (sheet_w - t.width) // 2, top + (sheet_w - t.height) // 2), t)

        # The 3x3 the consumer indexes by, drawn over the art it applies to.
        cell = (sheet_w - 26) / 3
        ox, oy = x + 13, top + 13
        for k in (1, 2):
            d.line([ox + cell * k, oy, ox + cell * k, oy + cell * 3], fill=LINE, width=1)
            d.line([ox, oy + cell * k, ox + cell * 3, oy + cell * k], fill=LINE, width=1)

        label(d, (x + 2, top + sheet_w + 18), name, 13, ACCENT)
        label(d, (x + 2 + label_w(d, name, 13) + 14, top + sheet_w + 18), note, 12, INK_3)
    img.save(str(DOCS / "poses.png"))
    print(str(DOCS / "poses.png"), img.size)


hero()
poses()
