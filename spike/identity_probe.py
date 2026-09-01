"""Measuring whether identity drift is detectable cheaply.

Not part of the mascotify package — a standalone script that reproduces the
identity-metric table in SPIKE.md. It imports from ../src directly, so it breaks
if the layout moves; that is fine, it is evidence rather than code.


Question: can Pillow+numpy separate "same character, different pose" from
"character has drifted into a sibling design"? If not, the identity check has
to be the agent's eyes, not a number.

Controls, from real spike data:
  within-sheet2   same character, different poses  -> the noise floor
  ref vs sheet2   known mild drift
  ref vs sheet1   known worse drift (head and ear pods visibly changed)
A useful metric puts the noise floor well below the drift cases.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mascotify.imaging import grid
from mascotify.imaging import normalize as nz
from mascotify.imaging.cutout import alpha_bbox, chroma_key
from mascotify.spec import CutoutSpec

HERE = Path(__file__).parent


def frames_of(path: Path) -> list[Image.Image]:
    cut = chroma_key(Image.open(path), CutoutSpec())
    rep = grid.detect(cut, 3, 4)
    return nz.normalize(grid.slice_frames(cut, rep), align="baseline")


def single(path: Path) -> Image.Image:
    cut = chroma_key(Image.open(path), CutoutSpec())
    bb = alpha_bbox(cut)
    return nz.normalize([cut.crop(bb)], align="baseline")[0]


# ---------------------------------------------------------------- metrics


def palette(img: Image.Image, bins: int = 4) -> np.ndarray:
    """Alpha-weighted RGB histogram. A mascot's colours are its signature."""
    a = np.asarray(img.convert("RGBA"), dtype=np.float32)
    rgb, alpha = a[..., :3], a[..., 3] / 255.0
    m = alpha > 0.5
    if not m.any():
        return np.zeros(bins**3)
    q = np.clip((rgb[m] / 256 * bins).astype(int), 0, bins - 1)
    idx = q[:, 0] * bins * bins + q[:, 1] * bins + q[:, 2]
    h = np.bincount(idx, weights=alpha[m], minlength=bins**3)
    return h / h.sum()


def palette_dist(a: Image.Image, b: Image.Image) -> float:
    """Total variation distance, 0 = identical palette, 1 = disjoint."""
    return float(np.abs(palette(a) - palette(b)).sum() / 2)


def silhouette_iou(a: Image.Image, b: Image.Image, size: int = 128) -> float:
    """Overlap of the two alpha masks, scaled to a common box first."""

    def mask(im: Image.Image) -> np.ndarray:
        bb = alpha_bbox(im)
        crop = im.crop(bb) if bb else im
        return np.asarray(crop.resize((size, size), Image.LANCZOS).convert("RGBA"))[..., 3] > 127

    ma, mb = mask(a), mask(b)
    union = (ma | mb).sum()
    return float((ma & mb).sum() / union) if union else 1.0


def proportions(img: Image.Image) -> float:
    """Content aspect ratio — catches a body that got chunkier or taller."""
    bb = alpha_bbox(img)
    return (bb[2] - bb[0]) / (bb[3] - bb[1]) if bb else 0.0


def report(name: str, pairs: list[tuple[Image.Image, Image.Image]]) -> dict:
    pal = [palette_dist(a, b) for a, b in pairs]
    iou = [silhouette_iou(a, b) for a, b in pairs]
    ar = [abs(proportions(a) - proportions(b)) for a, b in pairs]
    row = {
        "n": len(pairs),
        "palette_med": float(np.median(pal)),
        "palette_p90": float(np.percentile(pal, 90)),
        "iou_med": float(np.median(iou)),
        "iou_p10": float(np.percentile(iou, 10)),
        "aspect_med": float(np.median(ar)),
    }
    print(
        f"{name:<26} n={row['n']:<4} "
        f"palette med={row['palette_med']:.3f} p90={row['palette_p90']:.3f}   "
        f"IoU med={row['iou_med']:.3f} p10={row['iou_p10']:.3f}   "
        f"|Δaspect| med={row['aspect_med']:.3f}"
    )
    return row


def main() -> None:
    ref = single(HERE / "ref.png")
    s1 = frames_of(HERE / "sheet.png")
    s2 = frames_of(HERE / "sheet2.png")
    print(f"ref 1 frame, sheet1 {len(s1)} frames, sheet2 {len(s2)} frames\n")

    noise = report("within-sheet2 (baseline)", list(itertools.combinations(s2, 2)))
    report("within-sheet1 (baseline)", list(itertools.combinations(s1, 2)))
    drift2 = report("ref vs sheet2 (mild)", [(ref, f) for f in s2])
    drift1 = report("ref vs sheet1 (worse)", [(ref, f) for f in s1])
    cross = report("sheet1 vs sheet2", [(a, b) for a in s1[:6] for b in s2[:6]])

    print("\nseparation (drift vs the within-sheet noise floor):")
    for label, d in (("ref vs sheet2", drift2), ("ref vs sheet1", drift1), ("s1 vs s2", cross)):
        pal_sep = d["palette_med"] / noise["palette_med"] if noise["palette_med"] else float("inf")
        iou_gap = noise["iou_med"] - d["iou_med"]
        print(f"  {label:<16} palette {pal_sep:5.2f}x noise   IoU drops {iou_gap:+.3f}")


if __name__ == "__main__":
    main()
