"""Making an identity check something an agent can actually do.

There is deliberately no automated identity gate here, and the numbers below are
not one waiting to be wired up. Three cheap metrics were measured against real
drift and each one failed, concretely:

- silhouette IoU: the within-sheet noise floor (0.740) came out *worse* than the
  drift cases (0.750, 0.772). An arm raised versus lowered moves the outline
  more than a changed character design does. Pose swamps identity.
- palette vs aspect: on the same two sheets, palette ranked sheet2 as farther
  from the anchor (0.087 vs 0.063) while aspect ranked it closer (0.090 vs
  0.114). Size-matched visual inspection agreed with aspect. So palette — the
  metric with the widest margin over noise, and therefore the tempting one to
  gate on — is the one that got it backwards.
- resolution was ruled out as the cause: size-matching narrows the palette gap
  but never flips that ordering.

Anything here turned into a pass/fail would be confidently wrong. See SPIKE.md.

What does work is a multimodal caller looking at the images — but only if they
are size-matched and side by side. Comparing a 1024px anchor against a 390px
frame straight from a sheet is what made the drift hard to judge in the first
place. So this module builds the strip and reports the numbers as context,
never as a verdict.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw

from .cutout import alpha_bbox, content_bbox

STRIP_HEIGHT = 420
LABEL_BAND = 30


def _fit(img: Image.Image, height: int) -> Image.Image:
    """Trim to the subject and scale to a common height.

    Both halves matter: without the trim, canvas padding shifts the character;
    without the scale match, every downstream comparison measures resolution.

    Uses `content_bbox`, not `alpha_bbox`, because this is the one place where a
    speck left over from keying does real damage — it inflates the box, shrinks
    the character inside the strip, and produces a mismatched comparison that
    still looks like a matched one.
    """
    bb = content_bbox(img)
    crop = img.crop(bb) if bb else img
    if crop.height == 0:
        return crop
    return crop.resize((max(1, round(crop.width * height / crop.height)), height), Image.LANCZOS)


def palette(img: Image.Image, bins: int = 4) -> np.ndarray:
    """Alpha-weighted RGB histogram, normalised."""
    a = np.asarray(img.convert("RGBA"), dtype=np.float32)
    rgb, alpha = a[..., :3], a[..., 3] / 255.0
    m = alpha > 0.5
    if not m.any():
        return np.zeros(bins**3)
    q = np.clip((rgb[m] / 256 * bins).astype(int), 0, bins - 1)
    idx = q[:, 0] * bins * bins + q[:, 1] * bins + q[:, 2]
    h = np.bincount(idx, weights=alpha[m], minlength=bins**3)
    return h / h.sum()


def palette_distance(a: Image.Image, b: Image.Image) -> float:
    """Total variation distance between two palettes, 0..1.

    Size-match before calling: antialiasing at different resolutions shifts the
    histogram on its own.
    """
    return float(np.abs(palette(a) - palette(b)).sum() / 2)


def aspect(img: Image.Image) -> float:
    bb = alpha_bbox(img)
    return (bb[2] - bb[0]) / (bb[3] - bb[1]) if bb and bb[3] > bb[1] else 0.0


@dataclass
class Comparison:
    """Context for a human or an agent to judge, not a pass/fail."""

    strip: Image.Image
    palette_median: float
    palette_noise: float
    aspect_anchor: float
    aspect_median: float

    @property
    def palette_ratio(self) -> float:
        """Palette gap as a multiple of the within-sheet noise floor.

        Indicative only. The denominator is a median over `sample choose 2`
        pairs — six of them at the default sample of 4, and zero when sample is
        1 — so it is far noisier than the figure it approximates. The same two
        sheets scored 5.2x and 8.1x here against 7.4x and 10.3x when the noise
        floor was taken over all 66 within-sheet pairs.

        High means the colours moved. It does not mean the character stopped
        being recognisable, and it is not ordered the same way a person would
        order it — see the module docstring.
        """
        return self.palette_median / self.palette_noise if self.palette_noise else float("inf")

    def notes(self) -> list[str]:
        out = [
            f"palette gap {self.palette_median:.3f} "
            f"({self.palette_ratio:.1f}x the within-sheet noise floor)",
            f"aspect ratio {self.aspect_anchor:.3f} anchor vs {self.aspect_median:.3f} generated",
        ]
        out.append(
            "these are context, not a verdict — they disagree with each other on real "
            "drift, so judge from the strip"
        )
        return out


def build(anchor: Image.Image, frames: list[Image.Image], *, sample: int = 4) -> Comparison:
    """Lay the anchor next to sampled frames, all at one height."""
    if not frames:
        raise ValueError("nothing to compare against")

    step = max(1, len(frames) // sample)
    picked = frames[::step][:sample]

    a_fit = _fit(anchor, STRIP_HEIGHT)
    f_fit = [_fit(f, STRIP_HEIGHT) for f in picked]

    cell = max([a_fit.width] + [f.width for f in f_fit]) + 32
    strip = Image.new("RGB", (cell * (len(f_fit) + 1), STRIP_HEIGHT + LABEL_BAND), (250, 250, 252))
    d = ImageDraw.Draw(strip)

    for i, (label, im) in enumerate([("anchor", a_fit)] + [(f"frame {j + 1}", f) for j, f in enumerate(f_fit)]):
        strip.paste(im, (i * cell + (cell - im.width) // 2, LABEL_BAND), im)
        d.text((i * cell + 10, 9), label, fill=(30, 30, 40))
        if i == 1:  # divider between the anchor and the generated frames
            d.line([(i * cell - 1, 0), (i * cell - 1, strip.height)], fill=(200, 200, 210), width=2)

    dists = [palette_distance(a_fit, f) for f in f_fit]
    noise = (
        float(np.median([palette_distance(x, y) for x, y in itertools.combinations(f_fit, 2)]))
        if len(f_fit) > 1
        else 0.0
    )
    return Comparison(
        strip=strip,
        palette_median=float(np.median(dists)),
        palette_noise=noise,
        aspect_anchor=aspect(a_fit),
        aspect_median=float(np.median([aspect(f) for f in f_fit])),
    )
