"""The image-to-video tier.

Same destination as the sprite path — normalised transparent frames, packed and
exported — reached differently: generate a clip, pull frames with ffmpeg, then
key every one of them.

The expensive difference is that keying happens N times instead of once. The
clip starts from an anchor already on a flat key and the prompt demands the
backdrop hold that colour, so chroma is the default here as well — reach for
`--cutout matte` only when a model lets the background drift, and note that
needs the matting extra installed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

from ..export.targets import TARGETS, export_all
from ..imaging import normalize as nz
from ..imaging.cutout import cut_out
from ..imaging.pack import pack, write_animation
from ..pipeline import Job
from ..spec import CutoutSpec, JobSpec, MotionSpec, SheetSpec
from . import providers


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError(
            "the video path needs ffmpeg on PATH — brew install ffmpeg (macOS) "
            "or apt install ffmpeg (Debian/Ubuntu)"
        )
    return exe


def _check_cutout(spec: CutoutSpec) -> None:
    """Prove the chosen cutout method can actually run, before spending money."""
    if spec.method != "matte":
        return
    try:
        import rembg  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "--cutout matte needs the matting extra: pip install 'mascotify[matting]'.\n"
            "The default --cutout chroma needs nothing extra and suits a clip generated "
            "from an anchor on a flat key colour."
        ) from exc


def extract_frames(clip: Path, out_dir: Path, fps: int, max_frames: int) -> list[Path]:
    """Pull frames out of a clip at a fixed rate.

    Capped rather than decimated: a 5s clip at 24fps is 120 frames, and every
    one of them costs a keying pass and a slot in the exported sheet.
    """
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            _ffmpeg(),
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(clip),
            "-vf", f"fps={fps}",
            "-frames:v", str(max_frames),
            str(out_dir / "%04d.png"),
        ],
        check=True,
    )
    frames = sorted(out_dir.glob("*.png"))
    if not frames:
        raise RuntimeError(f"ffmpeg extracted no frames from {clip}")
    return frames


def run_video(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    ref = Path(args.ref).resolve()
    if not ref.exists():
        print(f"reference image not found: {ref}", file=sys.stderr)
        return 1

    spec = JobSpec(
        motion=MotionSpec(action=args.action, description=args.describe, name=args.name),
        sheet=SheetSpec(rows=0, cols=0, fps=args.fps, base_pt=args.base_pt, ping_pong=False),
        cutout=CutoutSpec(method=args.cutout, key_color=args.key_color),
        engine="video",
    )
    job = Job(root=root, spec=spec, name=args.job or f"{args.action}-video")
    job.prepare()

    # Everything that can fail must fail before the charge. A missing ffmpeg or
    # an uninstalled matting extra discovered *after* generation means the user
    # paid for a clip and got an exception.
    providers.require_key(args.provider)
    _ffmpeg()
    _check_cutout(spec.cutout)

    model = args.model or providers.DEFAULT_MODELS[args.provider]
    est = providers.ROUGH_COST_USD[args.provider]

    if not args.yes:
        print(
            f"About to generate a {args.duration}s clip via {args.provider} ({model}).\n"
            f"Estimated cost: ~${est:.2f} on your key. This is a real charge.\n"
            f"Re-run with --yes to proceed.",
            file=sys.stderr,
        )
        return 3

    motion = spec.motion.phrase()
    prompt = (
        f"The character {motion}. The character stays centered and fully in frame at a "
        f"constant size. Static locked-off camera, no camera movement, no zoom, no pan. "
        f"The background stays a completely flat solid {args.key_color} at all times. "
        f"No new objects, no text, no shadow on the background."
    )

    clip = providers.generate(
        args.provider,
        image=ref,
        prompt=prompt,
        duration=args.duration,
        model=args.model,
        out=job.dir / "clip.mp4",
    )
    print(f"clip: {clip.path}", file=sys.stderr)

    paths = extract_frames(clip.path, job.dir / "raw", args.fps, args.max_frames)
    print(f"extracted {len(paths)} frames at {args.fps} fps", file=sys.stderr)

    cut = [cut_out(Image.open(p), spec.cutout) for p in paths]
    frames = nz.normalize(cut, align="baseline")
    seam = nz.seam_score(frames)
    loop = nz.apply_loop(frames, ping_pong=spec.sheet.ping_pong)

    if job.frames_dir.exists():
        shutil.rmtree(job.frames_dir)
    job.frames_dir.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(loop):
        f.save(job.frames_dir / f"{i:03d}.png")

    atlas = pack(loop, fps=args.fps)
    preview = write_animation(loop, job.dir / "preview.webp", fps=args.fps)
    targets = tuple(args.targets.split(",")) if args.targets else TARGETS
    bundles = export_all(
        atlas, loop, job.out_dir, job.name, targets=targets, base_pt=args.base_pt
    )

    (job.dir / "report.json").write_text(
        json.dumps(
            {
                "engine": "video",
                "provider": clip.provider,
                "model": clip.model,
                "frames": len(loop),
                "fps": args.fps,
                "seam": round(seam, 4),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"\nexported {len(loop)} frames at {args.fps} fps", file=sys.stderr)
    for b in bundles:
        print(f"  {b.target:<8}{b.root}", file=sys.stderr)
    print(f"\npreview  {preview}", file=sys.stderr)
    return 0
