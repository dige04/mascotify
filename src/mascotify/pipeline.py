"""The one path every interface goes through.

The CLI, the MCP server and the agent skill all call these functions, so a sheet
processed through any of them lands identically. That is deliberate: the moment
the web path and the CLI path diverge, exported assets stop being reproducible.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .export.targets import TARGETS, Bundle, export_all
from .imaging import grid, normalize as nz
from .imaging.cutout import cut_out
from .imaging.pack import Atlas, pack, write_animation
from .spec import JobSpec

WORKSPACE = ".mascotify"


def sha(data: bytes | Path, length: int = 12) -> str:
    """Content address. Everything expensive is cached against one of these."""
    raw = Path(data).read_bytes() if isinstance(data, Path) else data
    return hashlib.sha256(raw).hexdigest()[:length]


@dataclass
class Job:
    """One animation's working directory."""

    root: Path
    spec: JobSpec
    name: str = "mascot"

    @property
    def dir(self) -> Path:
        return self.root / WORKSPACE / self.name

    @property
    def sheet_path(self) -> Path:
        return self.dir / "sheet.png"

    @property
    def frames_dir(self) -> Path:
        return self.dir / "frames"

    @property
    def out_dir(self) -> Path:
        return self.dir / "export"

    def prepare(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.spec.write(self.dir / "job.json")
        return self.dir

    @classmethod
    def open(cls, root: Path, name: str) -> "Job":
        d = root / WORKSPACE / name
        if not (d / "job.json").exists():
            raise FileNotFoundError(
                f"no job named {name!r} under {root / WORKSPACE} — run `mascotify plan` first"
            )
        return cls(root=root, spec=JobSpec.load(d / "job.json"), name=name)


@dataclass
class Result:
    """Everything one ingest produced."""

    report: grid.GridReport
    frames: list[Image.Image] = field(default_factory=list)
    atlas: Atlas | None = None
    bundles: list[Bundle] = field(default_factory=list)
    seam: float = 0.0
    ping_pong: bool = False
    preview: Path | None = None

    @property
    def ok(self) -> bool:
        return self.report.ok


def analyse(sheet: Image.Image, spec: JobSpec) -> tuple[Image.Image, grid.GridReport]:
    """Key out the backdrop and measure the grid. No writes."""
    cut = cut_out(sheet, spec.cutout)
    return cut, grid.detect(cut, spec.sheet.rows, spec.sheet.cols)


def process(
    sheet_path: Path,
    job: Job,
    *,
    targets: tuple[str, ...] = TARGETS,
    strict: bool = True,
) -> Result:
    """Sheet on disk to exported bundles.

    `strict` stops at validation rather than exporting a sheet with clipped or
    mis-scaled frames. Turning it off is for inspecting a bad generation, not
    for shipping one.
    """
    spec = job.spec
    spec.sheet.validate()

    cut, report = analyse(Image.open(sheet_path), spec)
    job.prepare()
    cut.save(job.dir / "sheet-cut.png")

    def _write_report(extra: dict | None = None) -> None:
        """Written twice: once so a rejected sheet still leaves a record, and
        again with the loop measurements once the export succeeds."""
        payload = {
            "ok": report.ok,
            "frames_found": report.found,
            "frames_expected": report.expected,
            "rows": report.rows,
            "cols": report.cols,
            "scale_spread": round(report.scale_spread, 4),
            "baseline_spread": round(report.baseline_spread, 4),
            "clipped": [i + 1 for i in report.clipped],
            "problems": report.problems(),
            **(extra or {}),
        }
        (job.dir / "report.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    _write_report()
    if strict and not report.ok:
        return Result(report=report)

    frames = nz.normalize(grid.slice_frames(cut, report))
    seam = nz.seam_score(frames)
    loop = nz.apply_loop(frames, ping_pong=spec.sheet.ping_pong)

    if job.frames_dir.exists():
        shutil.rmtree(job.frames_dir)
    job.frames_dir.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(loop):
        f.save(job.frames_dir / f"{i:03d}.png")

    atlas = pack(loop, fps=spec.sheet.fps)
    preview = write_animation(loop, job.dir / "preview.webp", fps=spec.sheet.fps)
    bundles = export_all(
        atlas, loop, job.out_dir, job.name, targets=targets, base_pt=spec.sheet.base_pt
    )

    _write_report(
        {
            "seam": round(seam, 4),
            "fps": spec.sheet.fps,
            # Kept so a reopened job can show how to use each bundle without
            # re-running the export just to recover a one-line string.
            "snippets": {b.target: b.snippet for b in bundles},
        }
    )

    return Result(
        report=report,
        frames=loop,
        atlas=atlas,
        bundles=bundles,
        seam=seam,
        ping_pong=len(loop) != len(frames),
        preview=preview,
    )
