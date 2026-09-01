"""MCP server, for agents that prefer tools over a shell.

Same pipeline as the CLI. The tools deliberately do not generate anything: they
hand back a prompt and a path, the agent's own image tool draws it, and
`mascotify_ingest` picks it up. An MCP server cannot call the calling agent's
image tool, and routing through a provider key would defeat the point.

Run with: python -m mascotify.mcp_server
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from . import pipeline
from .export.targets import TARGETS
from .gen import prompts
from .pipeline import Job
from .spec import MOTIONS, CutoutSpec, JobSpec, MotionSpec, SheetSpec

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - optional extra
    raise SystemExit(
        "the MCP server needs the mcp extra: pip install 'mascotify[mcp]'"
    ) from exc

mcp = FastMCP("mascotify")


@mcp.tool()
def mascotify_motions() -> dict[str, str]:
    """List the motions that reliably survive being drawn as a 12-frame grid."""
    return MOTIONS


@mcp.tool()
def mascotify_anchor(character: str, out: str = "ref.png") -> dict:
    """Get the prompt for the canonical still that locks a mascot's identity.

    Generate the returned prompt with your own image tool, save it to `out`,
    look at it, then pass that path to mascotify_plan.
    """
    return {
        "prompt": prompts.anchor_prompt(character),
        "save_to": str(Path(out).resolve()),
        "size": "1024x1024",
        "next": "generate this, save it, then call mascotify_plan with ref=<save_to>",
    }


@mcp.tool()
def mascotify_plan(
    action: str = "wave",
    ref: str = "",
    describe: str = "",
    rows: int = 3,
    cols: int = 4,
    fps: int = 12,
    root: str = ".",
    job: str = "",
) -> dict:
    """Create a job and return the sprite-sheet prompt plus where to save it.

    Generate the returned prompt with your own image tool at roughly the given
    size, save it to `save_to`, then call mascotify_ingest.
    """
    spec = JobSpec(
        motion=MotionSpec(action=action, description=describe),
        sheet=SheetSpec(rows=rows, cols=cols, fps=fps),
    )
    if ref:
        ref_path = Path(ref).resolve()
        if not ref_path.exists():
            raise FileNotFoundError(f"reference not found: {ref_path}")
        spec.reference_sha = pipeline.sha(ref_path)

    j = Job(root=Path(root).resolve(), spec=spec, name=job or action)
    j.prepare()
    prompt = prompts.sheet_prompt(spec, character=describe, from_reference=bool(ref))
    (j.dir / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")

    return {
        "job": j.name,
        "prompt": prompt,
        "save_to": str(j.sheet_path),
        "size": prompts.sheet_size(spec.sheet),
        "reference": str(ref) if ref else None,
        "next": "generate this, save it to save_to, then call mascotify_ingest",
    }


@mcp.tool()
def mascotify_doctor(sheet: str, rows: int = 3, cols: int = 4) -> dict:
    """Measure a generated sheet without writing anything."""
    path = Path(sheet).resolve()
    if not path.exists():
        raise FileNotFoundError(f"sheet not found: {path}")
    spec = JobSpec(sheet=SheetSpec(rows=rows, cols=cols))
    _, rep = pipeline.analyse(Image.open(path), spec)
    return {
        "ok": rep.ok,
        "frames_found": rep.found,
        "frames_expected": rep.expected,
        "scale_spread": round(rep.scale_spread, 4),
        "clipped": [i + 1 for i in rep.clipped],
        "problems": rep.problems(),
    }


@mcp.tool()
def mascotify_compare(ref: str, against: str, out: str = "compare.png", sample: int = 4) -> dict:
    """Write a size-matched strip of the anchor beside generated frames, then look at it.

    There is no automated identity verdict — the cheap metrics contradict each
    other on real drift. Read the image at `strip` and judge: limb thickness,
    antenna or ear length, marking size, head roundness. The numbers are context.
    """
    from .imaging import compare as cmp_mod
    from .imaging import grid as grid_mod, normalize as nz_mod

    ref_p, gen_p = Path(ref).resolve(), Path(against).resolve()
    for p in (ref_p, gen_p):
        if not p.exists():
            raise FileNotFoundError(f"not found: {p}")

    spec = JobSpec()
    anchor_cut, _ = pipeline.analyse(Image.open(ref_p), spec)
    gen_cut, rep = pipeline.analyse(Image.open(gen_p), spec)
    frames = (
        nz_mod.normalize(grid_mod.slice_frames(gen_cut, rep))
        if rep.found > 1
        else nz_mod.normalize([gen_cut])
    )

    result = cmp_mod.build(anchor_cut, frames, sample=sample)
    out_p = Path(out).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)
    result.strip.save(out_p)

    return {
        "strip": str(out_p),
        "notes": result.notes(),
        "verdict": None,
        "next": "read the strip image and decide whether the character drifted",
    }


@mcp.tool()
def mascotify_ingest(sheet: str, job: str = "", root: str = ".", targets: str = "") -> dict:
    """Validate a generated sheet and export every platform bundle.

    If `ok` is false, regenerate using `repair_prompt` and ingest again.
    """
    root_path = Path(root).resolve()
    path = Path(sheet).resolve()
    if not path.exists():
        raise FileNotFoundError(f"sheet not found: {path}")

    name = job or (path.parent.name if (path.parent / "job.json").exists() else "mascot")
    try:
        j = Job.open(root_path, name)
    except FileNotFoundError:
        j = Job(root=root_path, spec=JobSpec(), name=name)
        j.prepare()

    if path != j.sheet_path:
        j.sheet_path.parent.mkdir(parents=True, exist_ok=True)
        j.sheet_path.write_bytes(path.read_bytes())

    chosen = tuple(targets.split(",")) if targets else TARGETS
    result = pipeline.process(j.sheet_path, j, targets=chosen)
    rep = result.report

    if not rep.ok:
        return {
            "ok": False,
            "problems": rep.problems(),
            "repair_prompt": prompts.repair_prompt(rep.problems(), j.spec),
            "next": "regenerate with repair_prompt, save to the same path, ingest again",
        }

    return {
        "ok": True,
        "frames": len(result.frames),
        "fps": j.spec.sheet.fps,
        "seam": round(result.seam, 3),
        "ping_pong": result.ping_pong,
        "preview": str(result.preview),
        "exports": [
            {"target": b.target, "dir": str(b.root), "snippet": b.snippet, "files": b.rel()}
            for b in result.bundles
        ],
        "next": "copy the bundle the project needs into its asset directory and wire up the snippet",
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
