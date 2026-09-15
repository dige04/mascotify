"""Putting the frames onto a shared canvas.

Frames come out of a sheet at whatever size and offset the generator felt like.
Played back raw they jitter: the character swims around inside the frame even
when the drawn pose is fine. Normalising means picking one canvas, one anchor
point, and one scale, then re-seating every frame against them.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from PIL import Image

from .cutout import alpha_bbox

Align = Literal["baseline", "center", "centroid"]

# Above this, the wrap from last frame to first reads as a stutter rather than
# as one more step of the motion. Measured as a multiple of the average step,
# so it is independent of how fast the animation moves.
SEAM_STUTTER = 2.0


def centroid(img: Image.Image) -> tuple[float, float]:
    """Alpha-weighted centre of mass.

    Steadier than a bbox centre for motion where a limb swings out: the bbox
    lurches sideways when an arm extends, the centroid barely moves.
    """
    a = np.asarray(img.convert("RGBA"), dtype=np.float32)[..., 3]
    total = a.sum()
    if total == 0:
        h, w = a.shape
        return w / 2.0, h / 2.0
    ys, xs = np.nonzero(a)
    weights = a[ys, xs]
    return float((xs * weights).sum() / total), float((ys * weights).sum() / total)


def normalize(
    frames: list[Image.Image],
    *,
    align: Align = "baseline",
    padding: float = 0.06,
    canvas: tuple[int, int] | None = None,
    rescale_to_max: bool = False,
) -> list[Image.Image]:
    """Re-seat every frame on one canvas against a shared anchor.

    `padding` is a fraction of the content box left as breathing room, so a
    limb that swings wider in one frame does not get clipped by the canvas.

    `rescale_to_max` stretches every frame to the tallest frame's height. Off by
    default: height differences between frames are usually the motion itself —
    a bounce really is taller at its apex — and flattening them kills it. Turn
    it on when the generator drew the character at genuinely inconsistent
    scales, which `grid.GridReport.scale_spread` will tell you.
    """
    if not frames:
        return []

    trimmed: list[Image.Image] = []
    for f in frames:
        rgba = f.convert("RGBA")
        bb = alpha_bbox(rgba)
        trimmed.append(rgba.crop(bb) if bb else rgba)

    if rescale_to_max:
        target_h = max(im.height for im in trimmed)
        trimmed = [
            im
            if im.height == target_h
            else im.resize(
                (max(1, round(im.width * target_h / im.height)), target_h), Image.LANCZOS
            )
            for im in trimmed
        ]

    max_w = max(im.width for im in trimmed)
    max_h = max(im.height for im in trimmed)
    pad_x = round(max_w * padding)
    pad_y = round(max_h * padding)

    if canvas is None:
        cw, ch = max_w + 2 * pad_x, max_h + 2 * pad_y
    else:
        cw, ch = canvas
        # An explicit canvas may be tighter than the padding implies; never let
        # the margin push content off the bottom.
        pad_y = min(pad_y, max(0, ch - max_h))

    out: list[Image.Image] = []
    for im in trimmed:
        sheet = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        if align == "baseline":
            # Feet on a common line, horizontally centred: the right choice for
            # anything where the character stays planted on the ground.
            x = (cw - im.width) // 2
            y = ch - pad_y - im.height
        elif align == "centroid":
            cx, cy = centroid(im)
            x = round(cw / 2 - cx)
            y = round(ch / 2 - cy)
        else:
            x = (cw - im.width) // 2
            y = (ch - im.height) // 2
        sheet.alpha_composite(im, (max(0, min(x, cw - im.width)), max(0, min(y, ch - im.height))))
        out.append(sheet)
    return out


def seam_score(frames: list[Image.Image]) -> float:
    """How hard the loop snaps from the last frame back to the first.

    Expressed as a multiple of the average frame-to-frame step, so 1.0 means the
    wrap is indistinguishable from any other step and needs no help.

    Compares silhouette *and* colour. Alpha alone is enough for a wave or a
    bounce, where the outline carries the motion, but blind to a blink — the
    silhouette there is identical in every frame and only the pixels change.
    """
    if len(frames) < 2:
        return 0.0

    arrs = []
    for f in frames:
        a = np.asarray(f.convert("RGBA"), dtype=np.float32) / 255.0
        alpha = a[..., 3]
        # Premultiply so a colour change only counts where the character is.
        arrs.append(np.dstack([a[..., :3] * alpha[..., None], alpha]))

    shape = arrs[0].shape
    if any(x.shape != shape for x in arrs):
        raise ValueError("seam_score needs frames on a common canvas; normalize() them first")

    def diff(a: np.ndarray, b: np.ndarray) -> float:
        return float(np.abs(a - b).mean())

    steps = [diff(arrs[i + 1], arrs[i]) for i in range(len(arrs) - 1)]
    typical = float(np.mean(steps))
    if typical <= 1e-6:
        return 0.0  # nothing moves at all; there is no seam to hide
    return diff(arrs[0], arrs[-1]) / typical


def should_ping_pong(frames: list[Image.Image]) -> bool:
    """Whether this loop needs bouncing to hide its seam."""
    return seam_score(frames) > SEAM_STUTTER


def apply_loop(frames: list[Image.Image], *, ping_pong: bool | None = None) -> list[Image.Image]:
    """Optionally bounce the loop back on itself.

    `None` decides from the measured seam. Bouncing doubles the frame count and
    makes the motion symmetric — correct for a wave or a bounce, wrong for
    anything directional — so it is worth paying for only when the wrap actually
    stutters.
    """
    if len(frames) < 3:
        return list(frames)
    if ping_pong is None:
        ping_pong = should_ping_pong(frames)
    if not ping_pong:
        return list(frames)
    return list(frames) + list(reversed(frames[1:-1]))


def foot_anchor_x(img: Image.Image, frac: float = 0.25) -> float:
    """Horizontal centre of the character's base.

    `normalize(align="baseline")` centres each frame on its own bbox, which is
    right for a loop where the silhouette stays put but wrong for a pose set. On
    a directions grid the head turns, the bbox grows on that side, and
    bbox-centring slides the body the *other* way — so the mascot counter-slides
    away from the cursor it is supposed to be following.

    The bottom slice is the fix: feet do not move when a head turns, so the
    alpha-weighted centre of the lowest `frac` of the character is a body-fixed
    point that a head turn or a raised arm leaves alone.
    """
    a = np.asarray(img.convert("RGBA"), dtype=np.float32)[..., 3]
    ys, xs = np.nonzero(a)
    if len(ys) == 0:
        return img.width / 2.0
    cut = ys.max() - max(1.0, (ys.max() - ys.min() + 1) * frac)
    keep = ys >= cut
    xs, weights = xs[keep], a[ys[keep], xs[keep]]
    total = weights.sum()
    if total == 0:
        return img.width / 2.0
    return float((xs * weights).sum() / total)


def anchor_horizontally(frames: list[Image.Image], *, frac: float = 0.25) -> list[Image.Image]:
    """Shift already-normalised frames onto one shared body anchor.

    Runs after `normalize`, not instead of it: normalize picks the canvas and
    the baseline, this only slides each frame sideways.

    Deliberately returns no drift measurement. Most of the spread it corrects
    here is the bbox-centring `normalize` just applied, not anything the
    generator did — measuring it at this point reads 22% on a fixture whose body
    is nailed to the same pixel in all nine cells. Whether the *generator* moved
    the body is a different question, answered in sheet coordinates before any
    of this runs.
    """
    if not frames:
        return []

    anchors = [foot_anchor_x(f, frac) for f in frames]
    target = float(np.median(anchors))

    out: list[Image.Image] = []
    for frame, ax in zip(frames, anchors):
        dx = round(target - ax)
        if dx == 0:
            out.append(frame)
            continue
        # Shift within the existing canvas rather than growing it: normalize
        # already sized it, and the pose pair has to stay square.
        shifted = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        shifted.alpha_composite(frame, (max(dx, 0), 0), (max(-dx, 0), 0, frame.width, frame.height))
        out.append(shifted)
    return out
