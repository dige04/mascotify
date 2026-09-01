"""Finding the frames inside a generated sprite sheet.

Slicing a sheet into equal rectangles is the obvious approach and it does not
work. Image models lay cells out by eye: rows come out different heights, the
character floats at a different offset in each cell, and the bottom row is
routinely clipped by the canvas edge. So instead of trusting the requested
grid, find the gutters — the bands of pure background that separate real
content — and measure the cells that fall out of that.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

from .cutout import alpha_bbox


@dataclass(frozen=True)
class Cell:
    """One detected frame: its band in the sheet, and the character inside it."""

    row: int
    col: int
    band: tuple[int, int, int, int]  # cell bounds within the sheet
    bbox: tuple[int, int, int, int]  # tight bounds of the character

    @property
    def width(self) -> int:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> int:
        return self.bbox[3] - self.bbox[1]

    @property
    def baseline(self) -> int:
        """Bottom of the character. The line feet should share across frames."""
        return self.bbox[3]

    @property
    def center_x(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2.0


@dataclass
class GridReport:
    """What was found, and whether it is good enough to ship."""

    cells: list[Cell]
    rows: int
    cols: int
    expected: int
    clipped: list[int]  # frame indices touching the canvas edge
    scale_spread: float  # (max-min)/max of character heights
    baseline_spread: float  # baseline drift as a fraction of median height

    @property
    def found(self) -> int:
        return len(self.cells)

    @property
    def ok(self) -> bool:
        return (
            self.found == self.expected
            and not self.clipped
            and self.scale_spread <= 0.15
            and self.baseline_spread <= 0.10
        )

    def problems(self) -> list[str]:
        out: list[str] = []
        if self.found != self.expected:
            out.append(
                f"found {self.found} frames in a {self.rows}x{self.cols} layout, "
                f"expected {self.expected}"
            )
        if self.clipped:
            out.append(
                "frames clipped by the canvas edge: "
                + ", ".join(str(i + 1) for i in self.clipped)
                + " — the character needs more margin"
            )
        if self.scale_spread > 0.15:
            out.append(
                f"character size varies by {self.scale_spread:.0%} across frames "
                "(should be under 15%)"
            )
        if self.baseline_spread > 0.10:
            out.append(
                f"feet drift vertically by {self.baseline_spread:.0%} of character height "
                "(should be under 10%)"
            )
        return out


def _split_at(mask: np.ndarray, axis: int, expect: int, floor: int = 3) -> list[tuple[int, int]]:
    """Split an axis into `expect` bands by cutting at the widest gaps.

    Replaces a fixed threshold scaled off the image size, which was wrong in a
    way that only showed up on real output: a 1086px-tall sheet gave a 21px
    threshold while its actual row gutters were 15px, so three rows merged into
    one and a valid 3x4 grid was reported as 1x4.

    The caller already knows how many bands there should be, so use that instead
    of guessing a size. Cutting at the `expect - 1` widest interior gaps is
    scale-free and does not care how tight the layout is.
    """
    idx = np.nonzero(mask.any(axis=axis))[0]
    if len(idx) == 0:
        return []

    # Interior gaps only: leading and trailing background is margin, not gutter.
    step = np.diff(idx)
    at = np.nonzero(step > 1)[0]  # gap sits between idx[p] and idx[p + 1]
    sizes = step[at] - 1

    if expect <= 1 or len(at) < expect - 1:
        # Fewer candidate gutters than bands asked for: the layout is not what
        # the caller expects. Split permissively so `detect` reports the real
        # count rather than inventing one.
        return _bands(mask, axis, floor)

    # Cut at chosen positions rather than deriving a threshold and re-splitting.
    # A threshold cuts at every gap of that width, so when gaps tie — which is
    # exactly what an evenly laid out sheet produces — one extra internal gap
    # matching the gutter width yields expect + 1 bands. Measured: six 40px gaps
    # across six columns produced seven.
    span_start, span_end = int(idx[0]), int(idx[-1])
    stride = (span_end - span_start) / expect
    mids = [(int(idx[p]) + int(idx[p + 1])) / 2 for p in at]

    # Look for one gutter near each evenly-spaced boundary, rather than taking
    # the widest gaps globally. Position is the stronger evidence: a gap inside
    # a character sits mid-cell, and it can easily be wider than a real gutter
    # on a tightly packed sheet — legs apart at 40px between 30px gutters picks
    # the wrong cut every time if size leads.
    chosen: list[int] = []
    taken: set[int] = set()
    for k in range(expect - 1):
        target = span_start + stride * (k + 1)
        near = [
            i
            for i in range(len(at))
            if i not in taken and abs(mids[i] - target) <= stride / 2
        ]
        # Nearest gap to the boundary, widest breaking ties. Distance leads
        # because the window has already filtered to plausible gutters, and
        # inside it the wider gap is often the character's own — legs apart at
        # 40px beat a 30px gutter on size while sitting mid-cell.
        pick = min(near, key=lambda i: (abs(mids[i] - target), -int(sizes[i])), default=None)
        if pick is None:
            # No candidate near this boundary — the layout is uneven enough that
            # evenly-spaced targets do not describe it. Fall back to the widest
            # gap still unused, which is the best remaining evidence.
            remaining = [i for i in range(len(at)) if i not in taken]
            if not remaining:
                break
            pick = max(remaining, key=lambda i: int(sizes[i]))
        taken.add(pick)
        chosen.append(pick)

    cuts = sorted(int(at[i]) for i in chosen)

    starts = [int(idx[0]), *(int(idx[c + 1]) for c in cuts)]
    ends = [*(int(idx[c]) for c in cuts), int(idx[-1])]
    return [(s, e + 1) for s, e in zip(starts, ends)]


def _bands(mask: np.ndarray, axis: int, min_gap: int) -> list[tuple[int, int]]:
    """Split an axis into runs of content, separated by gaps of pure background.

    `min_gap` keeps a character's own internal gaps (the space between two legs,
    say) from being mistaken for a gutter between cells.

    Every band is trimmed to its content on both sides. The previous version let
    the final band run to the edge of the image, so the last row's band included
    the bottom margin while every other row's did not — which showed up as a
    fictitious 60% baseline drift on a perfectly aligned sheet.
    """
    idx = np.nonzero(mask.any(axis=axis))[0]
    if len(idx) == 0:
        return []

    # A run of `g` background pixels between two content pixels shows up as a
    # difference of g + 1 in their indices.
    breaks = np.nonzero(np.diff(idx) > min_gap)[0]
    starts = [idx[0], *(idx[b + 1] for b in breaks)]
    ends = [*(idx[b] for b in breaks), idx[-1]]
    return [(int(s), int(e) + 1) for s, e in zip(starts, ends)]


def detect(
    sheet: Image.Image,
    rows: int,
    cols: int,
    *,
    alpha_threshold: int = 8,
) -> GridReport:
    """Locate every frame in an already-cut-out sheet.

    Expects `sheet` to be RGBA with the backdrop already keyed to alpha 0.
    """
    arr = np.asarray(sheet.convert("RGBA"))
    mask = arr[..., 3] > alpha_threshold
    h, w = mask.shape

    # Rows first: find horizontal bands of content, then split each band into
    # columns independently. Doing it in this order tolerates rows that are
    # different heights, which is the common failure.
    row_bands = _split_at(mask, axis=1, expect=rows)
    cells: list[Cell] = []

    for r, (y0, y1) in enumerate(row_bands):
        strip = mask[y0:y1, :]
        for c, (x0, x1) in enumerate(_split_at(strip, axis=0, expect=cols)):
            sub = sheet.crop((x0, y0, x1, y1))
            bb = alpha_bbox(sub, alpha_threshold)
            if bb is None:
                continue
            bbox = (bb[0] + x0, bb[1] + y0, bb[2] + x0, bb[3] + y0)
            cells.append(Cell(row=r, col=c, band=(x0, y0, x1, y1), bbox=bbox))

    cells.sort(key=lambda c: (c.row, c.col))

    # A character touching the canvas edge was cropped by the generator; its
    # silhouette is wrong and no amount of normalisation recovers it.
    clipped = [
        i
        for i, cell in enumerate(cells)
        if cell.bbox[0] <= 1 or cell.bbox[1] <= 1 or cell.bbox[2] >= w - 1 or cell.bbox[3] >= h - 1
    ]

    heights = [c.height for c in cells] or [1]
    scale_spread = (max(heights) - min(heights)) / max(heights)

    # Baseline drift is only comparable within a row — rows sit at different
    # heights by construction. So measure how far apart the feet are inside each
    # row and report the worst one, as a fraction of character height.
    med_h = float(np.median(heights)) or 1.0
    per_row = [
        (max(b) - min(b)) / med_h
        for r in {c.row for c in cells}
        if (b := [c.baseline for c in cells if c.row == r])
    ]
    baseline_spread = max(per_row) if per_row else 0.0

    return GridReport(
        cells=cells,
        rows=len(row_bands),
        cols=max((c.col for c in cells), default=-1) + 1,
        expected=rows * cols,
        clipped=clipped,
        scale_spread=float(scale_spread),
        baseline_spread=float(baseline_spread),
    )


def slice_frames(sheet: Image.Image, report: GridReport) -> list[Image.Image]:
    """Cut each detected character out of the sheet at its tight bounds."""
    return [sheet.crop(cell.bbox) for cell in report.cells]
