"""The pose-pair path: two 3x3 grids that make one cursor-tracking mascot.

Separate from `pipeline.py` because almost nothing about a loop applies. There
is no fps, no seam to measure, no ping-pong — nothing plays. What replaces them
is a constraint the loop path never has: two independently generated sheets have
to agree with each other, because the component swaps between them on a click
and any disagreement reads as the character flinching.

So the two sheets are ingested in one call and normalised as eighteen frames on
one canvas. Normalising them separately gives two canvases and two baselines,
and the mascot jumps every time you poke it.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from .export.page_mascot import export_page_mascot
from .export.targets import Bundle
from .imaging import grid, normalize as nz
from .imaging.cutout import alpha_bbox, cut_out
from .imaging.pack import Atlas, pack
from .spec import PoseJobSpec

WORKSPACE = ".mascotify"

# Cap on the packed cell. The component renders into a ~140px box, so anything
# past this is weight the browser downloads and throws away — and these sheets
# are lossless, which makes the weight real.
MAX_CELL = 512


@dataclass
class PoseJob:
    """One pose pair's working directory."""

    root: Path
    spec: PoseJobSpec

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def dir(self) -> Path:
        return self.root / WORKSPACE / "poses" / self.name

    def sheet_path(self, which: str) -> Path:
        return self.dir / f"{which}.png"

    @property
    def out_dir(self) -> Path:
        return self.dir / "export"

    def prepare(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.spec.write(self.dir / "job.json")
        return self.dir

    @classmethod
    def open(cls, root: Path, name: str) -> "PoseJob":
        d = root / WORKSPACE / "poses" / name
        if not (d / "job.json").exists():
            raise FileNotFoundError(
                f"no pose job named {name!r} under {root / WORKSPACE / 'poses'} — "
                "run `mascotify pose-plan` first"
            )
        return cls(root=root, spec=PoseJobSpec.load(d / "job.json"))


@dataclass
class PoseReport:
    """Whether the pair is good enough to ship, and why not."""

    directions: grid.GridReport
    reactions: grid.GridReport
    # How far the body anchor wanders *within* each sheet, as a fraction of
    # character width. Split per sheet rather than pooled so a repair prompt can
    # name the sheet that has to be redrawn — pooling the two reads high whether
    # the fault is one sheet's cells disagreeing or the two sheets disagreeing
    # with each other, and those want different fixes.
    within: dict[str, float] = field(default_factory=dict)
    # How far apart the two sheets seat the body. This one is invisible until a
    # click swaps sheets, and then the character jumps sideways.
    between: float = 0.0
    # Height of the reactions character over the directions character. 1.0 is a
    # match; anything else pops the mascot larger or smaller on click.
    scale_match: float = 1.0
    max_drift: float = 0.06

    @property
    def anchor_drift(self) -> float:
        """The worst of the two, for a one-line summary."""
        return max([*self.within.values(), self.between], default=0.0)

    def grid_problems(self, report: grid.GridReport, which: str) -> list[str]:
        out = []
        if report.found != report.expected:
            out.append(
                f"{which}: found {report.found} cells in a {report.rows}x{report.cols} "
                f"layout, expected {report.expected}"
            )
        if report.clipped:
            out.append(
                f"{which}: cells clipped by the canvas edge: "
                + ", ".join(str(i + 1) for i in report.clipped)
                + " — the character needs more margin"
            )
        return out

    def problems(self) -> list[str]:
        out = self.grid_problems(self.directions, "directions")
        out += self.grid_problems(self.reactions, "reactions")
        for which, drift in self.within.items():
            if drift > self.max_drift:
                out.append(
                    f"{which}: the body shifts sideways by {drift:.0%} of its width across the "
                    f"cells (should be under {self.max_drift:.0%}) — the character has to stay "
                    "planted in the same spot while only the head or the face changes"
                )
        if self.between > self.max_drift:
            out.append(
                f"the two sheets seat the body {self.between:.0%} of its width apart — redraw "
                "the reactions sheet with the character in the same spot as the directions "
                "sheet, or it jumps sideways when clicked"
            )
        if not 0.9 <= self.scale_match <= 1.1:
            out.append(
                f"the reactions character is {self.scale_match:.0%} the height of the "
                "directions character — both sheets must draw it at the same scale, or it "
                "resizes when clicked"
            )
        return out

    @property
    def ok(self) -> bool:
        return not self.problems()

    def as_dict(self) -> dict:
        # scale_spread and baseline_spread are reported but deliberately not
        # gated on. Both are tuned for loop frames, where the silhouette is
        # identical by construction; a pose set breaks them on purpose — an
        # arms-up "surprised" cell really is taller than a neutral one, and
        # rejecting it would reject a correct sheet. What "same character, same
        # place" actually means here is the anchor and the scale match.
        return {
            "ok": self.ok,
            "anchor_drift": {
                **{k: round(v, 4) for k, v in self.within.items()},
                "between_sheets": round(self.between, 4),
            },
            "scale_match": round(self.scale_match, 4),
            "cells": {
                "directions": self.directions.found,
                "reactions": self.reactions.found,
            },
            "informational": {
                which: {
                    "scale_spread": round(r.scale_spread, 4),
                    "baseline_spread": round(r.baseline_spread, 4),
                }
                for which, r in (("directions", self.directions), ("reactions", self.reactions))
            },
            "problems": self.problems(),
        }


@dataclass
class PoseResult:
    """Everything one pose ingest produced."""

    report: PoseReport
    directions: list[Image.Image] = field(default_factory=list)
    reactions: list[Image.Image] = field(default_factory=list)
    bundle: Bundle | None = None

    @property
    def ok(self) -> bool:
        return self.report.ok


def _squarify(frames: list[Image.Image]) -> list[Image.Image]:
    """Pad a normalised set out to a square canvas.

    The component's `size` prop is a single number, so a non-square cell is
    letterboxed or squashed at render time. One offset applied to every frame,
    rather than re-centring each, so the shared baseline and anchor survive.
    """
    if not frames:
        return []
    w, h = frames[0].size
    n = max(w, h)
    if (w, h) == (n, n):
        return frames
    dx, dy = (n - w) // 2, (n - h) // 2
    out = []
    for f in frames:
        canvas = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        canvas.alpha_composite(f, (dx, dy))
        out.append(canvas)
    return out


def _median_height(frames: list[Image.Image]) -> float:
    heights = [b[3] - b[1] for f in frames if (b := alpha_bbox(f))]
    return float(np.median(heights)) if heights else 0.0


def _anchor_offsets(cut: Image.Image, report: grid.GridReport, frac: float) -> list[float]:
    """Where the generator put the body in each cell, in character widths.

    Measured in sheet coordinates against the nominal cell pitch, before any
    normalising, because that is the only place the question can be answered.
    Once frames are trimmed to their bounding boxes, a head turned right and a
    body moved right look identical — both widen the box on the right. Here the
    cell the character was drawn in is still a fixed frame of reference, and the
    foot anchor within it ignores whatever the head is doing.

    Returned as a list rather than a spread so the caller can ask both
    questions: how far the body wanders inside one sheet, and how far the two
    sheets' medians sit apart. The second only shows up on a click.
    """
    pitch = cut.width / max(1, report.cols)
    widths = [c.width for c in report.cells] or [1]
    ref_w = float(np.median(widths)) or 1.0
    offsets = []
    for cell in report.cells:
        anchor = cell.bbox[0] + nz.foot_anchor_x(cut.crop(cell.bbox), frac)
        offsets.append((anchor - (cell.col + 0.5) * pitch) / ref_w)
    return offsets


def analyse(sheet: Image.Image, spec: PoseJobSpec) -> tuple[Image.Image, grid.GridReport]:
    """Key out the backdrop and measure one 3x3 grid. No writes."""
    cut = cut_out(sheet, spec.cutout)
    return cut, grid.detect(cut, spec.pose.rows, spec.pose.cols)


def process_pair(
    directions_path: Path,
    reactions_path: Path,
    job: PoseJob,
    *,
    strict: bool = True,
    max_cell: int = MAX_CELL,
) -> PoseResult:
    """Two sheets on disk to one page-mascot bundle."""
    spec = job.spec
    spec.pose.validate()

    dir_cut, dir_report = analyse(Image.open(directions_path), spec)
    react_cut, react_report = analyse(Image.open(reactions_path), spec)

    job.prepare()
    dir_cut.save(job.dir / "directions-cut.png")
    react_cut.save(job.dir / "reactions-cut.png")

    report = PoseReport(
        directions=dir_report,
        reactions=react_report,
        max_drift=spec.pose.max_anchor_drift,
    )

    def _write_report() -> None:
        (job.dir / "report.json").write_text(
            json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8"
        )

    # Bail before slicing if either grid is wrong. Deliberately not gated on
    # `strict`: everything below assumes eighteen cells in a known order, so
    # there is no bundle to force out of a short grid — only a crash in pack()
    # or, worse, nine poses silently mapped to the wrong compass directions.
    # `--force` exists to ship a pair whose placement is a little loose, not to
    # ship one whose cells mean something other than what they claim.
    if (
        report.grid_problems(dir_report, "directions")
        or report.grid_problems(react_report, "reactions")
    ):
        _write_report()
        return PoseResult(report=report)

    dir_raw = grid.slice_frames(dir_cut, dir_report)
    react_raw = grid.slice_frames(react_cut, react_report)
    dir_h = _median_height(dir_raw)
    report.scale_match = _median_height(react_raw) / dir_h if dir_h else 1.0

    frac = spec.pose.anchor_frac
    offsets = {
        "directions": _anchor_offsets(dir_cut, dir_report, frac),
        "reactions": _anchor_offsets(react_cut, react_report, frac),
    }
    report.within = {k: (max(v) - min(v)) if v else 0.0 for k, v in offsets.items()}
    medians = [float(np.median(v)) for v in offsets.values() if v]
    report.between = abs(medians[0] - medians[1]) if len(medians) == 2 else 0.0

    # One normalize over all eighteen. This is the whole reason the two sheets
    # are ingested together rather than one at a time.
    #
    # The padding is wider than the loop path's because the corrective shift
    # below has to fit inside the canvas; at 6% a head turned hard to one side
    # runs out of room and the shift clamps instead of landing.
    seated = _squarify(nz.normalize(dir_raw + react_raw, align="baseline", padding=0.12))
    seated = nz.anchor_horizontally(seated, frac=frac)

    _write_report()
    if strict and not report.ok:
        return PoseResult(report=report)

    if seated and seated[0].width > max_cell:
        seated = [f.resize((max_cell, max_cell), Image.LANCZOS) for f in seated]

    n = spec.pose.cells
    dir_frames, react_frames = seated[:n], seated[n:]

    for which, frames in (("directions", dir_frames), ("reactions", react_frames)):
        d = job.dir / which
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
        for i, f in enumerate(frames):
            f.save(d / f"{i}.png")

    # fps is meaningless here; pack wants one, and nothing downstream reads it.
    # cols is passed explicitly rather than left to pack's near-square guess,
    # because the component indexes cells by (row, col) and a 3x3 is the
    # contract, not a layout preference.
    cols = spec.pose.cols
    dir_atlas = pack(dir_frames, fps=1, cols=cols)
    react_atlas = pack(react_frames, fps=1, cols=cols)

    bundle = export_page_mascot(
        dir_atlas,
        react_atlas,
        job.out_dir,
        job.name,
        base_px=spec.pose.base_px,
        label=spec.character or f"{job.name} mascot",
    )

    return PoseResult(
        report=report, directions=dir_frames, reactions=react_frames, bundle=bundle
    )
