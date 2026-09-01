"""Separating the character from its backdrop.

Image models that can draw a good mascot mostly cannot emit a clean alpha
channel, so the reliable route is to generate onto a flat key colour and pull
the matte here. Everything is vectorised: a 4x3 sheet at 512px cells is 3.1M
pixels, which a per-pixel Python loop turns into tens of seconds.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter

from ..spec import CutoutSpec, parse_hex

# Below this, an alpha value is matte noise rather than a real soft edge.
ALPHA_NOISE_FLOOR = 8
# Where the dominance ramp starts pulling alpha, as a fraction of the key
# colour's own dominance. Without a knee at 0, any colour whose green channel
# merely leads — a teal mascot, say — loses alpha; measured against a #00FF00
# key, a teal body leads by ~20 while a genuine antialiased edge against that
# key leads by ~120, so the two separate cleanly well below the midpoint.
DOMINANCE_KNEE = 0.25


def _spill_channels(key: np.ndarray) -> list[int]:
    """Channels the key colour is built from — green for #00FF00, and both
    green and blue for a cyan key. Returns [] for a dark key, which has no
    meaningful spill."""
    key_max = float(key.max())
    if key_max < 128:
        return []
    return [i for i, v in enumerate(key) if v >= key_max - 16 and v >= 128]


def sample_key(img: Image.Image, mode: str = "corners") -> tuple[int, int, int]:
    """Read the actual backdrop colour off the image border.

    Worth doing even when you asked for an exact hex: generators routinely land
    a few points off, and a hardcoded key then leaves a green fringe.
    """
    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
    h, w = arr.shape[:2]

    if mode == "corners":
        p = max(1, min(w, h, 12))
        patches = [arr[:p, :p], arr[:p, w - p :], arr[h - p :, :p], arr[h - p :, w - p :]]
        samples = np.concatenate([p_.reshape(-1, 3) for p_ in patches], axis=0)
    else:
        b = max(1, min(w, h, 6))
        samples = np.concatenate(
            [
                arr[:b, :, :].reshape(-1, 3),
                arr[h - b :, :, :].reshape(-1, 3),
                arr[:, :b, :].reshape(-1, 3),
                arr[:, w - b :, :].reshape(-1, 3),
            ],
            axis=0,
        )

    if samples.size == 0:
        raise ValueError("could not sample a key colour from the image border")
    med = np.median(samples.astype(np.float32), axis=0)
    return tuple(int(round(v)) for v in med)  # type: ignore[return-value]


def _smoothstep(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def chroma_key(img: Image.Image, spec: CutoutSpec) -> Image.Image:
    """Replace a flat key colour with alpha.

    Combines two independent estimates and keeps the more transparent of the
    two: a distance ramp (how close is this pixel to the key?) and a channel
    dominance ramp (how much does the key channel lead the others?). Distance
    alone punches holes in character colours that happen to sit near the key;
    dominance alone smears on desaturated pixels.
    """
    rgba = img.convert("RGBA")
    arr = np.asarray(rgba, dtype=np.float32)
    rgb, a0 = arr[..., :3], arr[..., 3]

    key = np.array(
        sample_key(rgba, spec.auto_key) if spec.auto_key != "none" else parse_hex(spec.key_color),
        dtype=np.float32,
    )

    # Chebyshev distance: a single channel drifting far is enough to disqualify
    # a pixel, which matches how key spill actually behaves.
    dist = np.abs(rgb - key).max(axis=-1)

    spill = _spill_channels(key)
    if spill:
        non_spill = [i for i in range(3) if i not in spill]
        key_strength = rgb[..., spill].min(axis=-1)
        non_key_strength = rgb[..., non_spill].max(axis=-1) if non_spill else np.zeros_like(dist)
        dominance = key_strength - non_key_strength

        # How far the key colour itself leads. Everything is measured against
        # this so the ramp adapts to whatever backdrop was actually generated.
        key_dominance = float(key[spill].min() - (key[non_spill].max() if non_spill else 0))
        knee = max(1.0, key_dominance * DOMINANCE_KNEE)
        span = max(1.0, key_dominance - knee)
        alpha_dom = 1.0 - np.clip((dominance - knee) / span, 0.0, 1.0)
        key_like = (dist <= 32) | (dominance >= knee)
    else:
        alpha_dom = np.ones_like(dist)
        key_like = np.ones_like(dist, dtype=bool)

    if spec.soft_matte:
        lo, hi = spec.transparent_threshold, spec.opaque_threshold
        if lo >= hi:
            raise ValueError("transparent_threshold must be below opaque_threshold")
        alpha_soft = _smoothstep((dist - lo) / (hi - lo))
        alpha = np.where(key_like, np.minimum(alpha_soft, alpha_dom), 1.0)
    else:
        alpha = (dist > spec.tolerance).astype(np.float32)

    alpha = alpha * (a0 / 255.0)
    alpha8 = np.clip(np.rint(alpha * 255.0), 0, 255).astype(np.uint8)
    alpha8[alpha8 <= ALPHA_NOISE_FLOOR] = 0

    out_rgb = rgb.copy()
    if spec.despill and spill:
        # Pull the key channel back under the strongest non-key channel, but
        # only on pixels the matte already considers partly background —
        # otherwise a legitimately green character loses its colour.
        touched = (alpha8 < 252) & key_like
        if touched.any():
            non_spill = [i for i in range(3) if i not in spill]
            cap = (
                np.maximum(0.0, out_rgb[..., non_spill].max(axis=-1) - 1.0)
                if non_spill
                else np.zeros_like(dist)
            )
            for c in spill:
                ch = out_rgb[..., c]
                out_rgb[..., c] = np.where(touched & (ch > cap), cap, ch)

    out = np.dstack([np.clip(out_rgb, 0, 255).astype(np.uint8), alpha8])
    out[alpha8 == 0] = 0  # zero the colour too, so scaling cannot bleed it back
    result = Image.fromarray(out, mode="RGBA")

    if spec.edge_contract:
        result = _contract(result, spec.edge_contract)
    if spec.edge_feather:
        result = _feather(result, spec.edge_feather)
    return result


def _contract(img: Image.Image, px: int) -> Image.Image:
    a = img.getchannel("A")
    for _ in range(px):
        a = a.filter(ImageFilter.MinFilter(3))
    img.putalpha(a)
    return img


def _feather(img: Image.Image, radius: float) -> Image.Image:
    a = img.getchannel("A").filter(ImageFilter.GaussianBlur(radius=radius))
    img.putalpha(a)
    return img


def matte(img: Image.Image) -> Image.Image:
    """Local segmentation fallback, for art that was not generated onto a key.

    Needs the optional `matting` extra; the chroma path covers the common case
    without pulling in onnxruntime.
    """
    try:
        from rembg import remove  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "cutout method 'matte' needs the matting extra: pip install 'mascotify[matting]'"
        ) from exc
    return remove(img.convert("RGBA"))


def cut_out(img: Image.Image, spec: CutoutSpec) -> Image.Image:
    if spec.method == "chroma":
        return chroma_key(img, spec)
    if spec.method == "matte":
        return matte(img)
    if spec.method == "alpha":
        rgba = img.convert("RGBA")
        if np.asarray(rgba)[..., 3].min() == 255:
            raise ValueError(
                "cutout method 'alpha' was requested but the image is fully opaque; "
                "generate onto a flat key colour and use --cutout chroma instead"
            )
        return rgba
    raise ValueError(f"unknown cutout method {spec.method!r}")


def content_bbox(
    img: Image.Image, threshold: int = 8, work: int = 192, min_area: float = 0.05
) -> tuple[int, int, int, int] | None:
    """Bounds of the real content, ignoring detached specks.

    `alpha_bbox` spans everything visible, so a single stray pixel left in a
    corner by an imperfect key stretches the box across the whole canvas. When
    that box is then used to scale two images to a common size, the comparison
    silently stops being like-for-like — measured at one speck turning a 181px
    render into 321px.

    Takes the union of every connected region at least `min_area` of the largest,
    rather than the largest alone: a mascot with a floating hat or a held object
    would lose it, and a sheet of separate characters would collapse to one cell.
    A speck is orders of magnitude below the cut; anything deliberate is not.
    """
    rgba = img.convert("RGBA")
    a = np.asarray(rgba)[..., 3]
    h, w = a.shape
    if not (a > threshold).any():
        return None

    scale = min(1.0, work / max(h, w))
    sh, sw = max(1, int(h * scale)), max(1, int(w * scale))
    small = np.asarray(
        Image.fromarray(a).resize((sw, sh), Image.BILINEAR)
    ) > threshold

    # Flood fill every region, then keep the ones big enough to be deliberate.
    seen = np.zeros_like(small, dtype=bool)
    regions: list[tuple[int, tuple[int, int, int, int]]] = []
    for sy, sx in zip(*np.nonzero(small)):
        if seen[sy, sx]:
            continue
        stack = [(int(sy), int(sx))]
        seen[sy, sx] = True
        y0 = y1 = int(sy)
        x0 = x1 = int(sx)
        area = 0
        while stack:
            cy, cx = stack.pop()
            area += 1
            y0, y1 = min(y0, cy), max(y1, cy)
            x0, x1 = min(x0, cx), max(x1, cx)
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < sh and 0 <= nx < sw and small[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        regions.append((area, (x0, y0, x1 + 1, y1 + 1)))

    if not regions:
        return None

    cut = max(a_ for a_, _ in regions) * min_area
    kept = [b for a_, b in regions if a_ >= cut]
    best = (
        min(b[0] for b in kept),
        min(b[1] for b in kept),
        max(b[2] for b in kept),
        max(b[3] for b in kept),
    )

    # Map back with a pixel of slack, then take exact bounds inside that window
    # so the downscale does not cost precision at the character's edges.
    inv = 1.0 / scale if scale else 1.0
    x0 = max(0, int(best[0] * inv) - 2)
    y0 = max(0, int(best[1] * inv) - 2)
    x1 = min(w, int(best[2] * inv) + 2)
    y1 = min(h, int(best[3] * inv) + 2)

    window = a[y0:y1, x0:x1]
    ys, xs = np.where(window > threshold)
    if len(xs) == 0:
        return best
    return (x0 + int(xs.min()), y0 + int(ys.min()), x0 + int(xs.max()) + 1, y0 + int(ys.max()) + 1)


def alpha_bbox(img: Image.Image, threshold: int = 8) -> tuple[int, int, int, int] | None:
    """Tight bounds of the visible pixels, ignoring matte noise.

    Pillow's own getbbox() treats alpha==1 as visible, which after keying means
    the box snaps to stray fringe pixels instead of the character.
    """
    a = np.asarray(img.convert("RGBA"))[..., 3]
    ys, xs = np.where(a > threshold)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
