"""Packing normalised frames back into a sprite sheet.

Frames arriving here already share a canvas, so packing is a uniform grid — no
bin-packing needed, and a uniform grid is what every consumer wants anyway.
CSS `steps()`, Unity's sprite slicer and Godot's SpriteFrames all assume equal
cells; a tightly-packed atlas would need an offset table each of them reads
differently.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path

from PIL import Image

# Plenty of shipping GPUs still cap a texture at this, and Unity and Godot both
# refuse to upload past it. Exceeding it turns into a black sprite at runtime
# rather than an import error, so it is worth enforcing at pack time.
MAX_TEXTURE_EDGE = 4096


@dataclass
class Atlas:
    """A packed sheet plus everything needed to play it back."""

    image: Image.Image
    cell_w: int
    cell_h: int
    cols: int
    rows: int
    count: int
    fps: int

    def frame_rect(self, i: int) -> tuple[int, int, int, int]:
        return (i % self.cols) * self.cell_w, (i // self.cols) * self.cell_h, self.cell_w, self.cell_h

    def meta(self, name: str) -> dict:
        return {
            "name": name,
            "frames": self.count,
            "fps": self.fps,
            "duration_ms": round(1000 * self.count / self.fps),
            "cell": {"w": self.cell_w, "h": self.cell_h},
            "grid": {"cols": self.cols, "rows": self.rows},
            "sheet": {"w": self.image.width, "h": self.image.height},
            "rects": [
                dict(zip(("x", "y", "w", "h"), self.frame_rect(i))) for i in range(self.count)
            ],
        }


def pack(
    frames: list[Image.Image],
    *,
    fps: int = 12,
    cols: int | None = None,
    max_edge: int = MAX_TEXTURE_EDGE,
) -> Atlas:
    """Lay frames out in a uniform grid.

    Defaults to a near-square grid, which keeps the sheet's longest edge down.
    A near-square grid alone is not enough at video frame counts: 120 frames at
    the sizes the sprite path produces lands around 4300x4900, past the 4096px
    limit plenty of GPUs still impose. So the cells are scaled down to fit
    rather than silently emitting a texture the target cannot upload.
    """
    if not frames:
        raise ValueError("nothing to pack")
    cw = max(f.width for f in frames)
    ch = max(f.height for f in frames)
    n = len(frames)
    cols = cols or max(1, math.ceil(math.sqrt(n)))
    rows = math.ceil(n / cols)

    if max_edge and (cols * cw > max_edge or rows * ch > max_edge):
        scale = min(max_edge / (cols * cw), max_edge / (rows * ch))
        cw, ch = max(1, int(cw * scale)), max(1, int(ch * scale))
        frames = [f.resize((cw, ch), Image.LANCZOS) for f in frames]

    sheet = Image.new("RGBA", (cols * cw, rows * ch), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        # Centre within the cell: frames are normalised so this is usually a
        # no-op, but a caller packing un-normalised frames still gets sane output.
        x = (i % cols) * cw + (cw - f.width) // 2
        y = (i // cols) * ch + (ch - f.height) // 2
        sheet.alpha_composite(f.convert("RGBA"), (x, y))

    return Atlas(image=sheet, cell_w=cw, cell_h=ch, cols=cols, rows=rows, count=n, fps=fps)


def write_atlas(atlas: Atlas, out_dir: Path, name: str) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / f"{name}.png"
    meta = out_dir / f"{name}.json"
    atlas.image.save(png)
    meta.write_text(json.dumps(atlas.meta(name), indent=2) + "\n", encoding="utf-8")
    return {"sheet": png, "meta": meta}


def write_animation(
    frames: list[Image.Image],
    out: Path,
    *,
    fps: int = 12,
    fmt: str = "WEBP",
    quality: int = 90,
    lossless: bool = False,
) -> Path:
    """Write an animated WebP or GIF.

    GIF gets 1-bit alpha and a 256-colour palette, so it is a fallback rather
    than a target; WebP keeps the soft matte the keyer produced.

    Lossy by default. A 46-frame celebrate loop came out at 2.55MB lossless
    against 0.66MB at quality 90 — four times the weight to preserve detail that
    a generated raster never had, on an asset destined for a web page. Pass
    `lossless=True` when the mascot is going somewhere that will re-encode it.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = round(1000 / fps)
    head, *tail = frames

    if fmt.upper() == "GIF":
        head.save(
            out,
            format="GIF",
            save_all=True,
            append_images=tail,
            duration=duration,
            loop=0,
            disposal=2,
            transparency=0,
        )
    else:
        head.save(
            out,
            format="WEBP",
            save_all=True,
            append_images=tail,
            duration=duration,
            loop=0,
            lossless=lossless,
            quality=quality,
            method=6,  # slowest/smallest encoder pass; this runs once per export
        )
    return out
