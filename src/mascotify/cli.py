"""mascotify command line.

The verbs split along one line: `plan` and `anchor` produce a prompt for
whoever is doing the generating, `ingest` consumes the image they produced.
Nothing in between needs an API key, because in the default mode the thing
doing the generating is the coding agent that ran the command.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from PIL import Image

from . import __version__, pipeline, pose
from .export.targets import TARGETS
from .gen import prompts
from .pipeline import Job
from .pose import PoseJob
from .spec import (
    DIRECTIONS,
    MOTIONS,
    REACTIONS,
    CutoutSpec,
    JobSpec,
    MotionSpec,
    PoseJobSpec,
    PoseSpec,
    SheetSpec,
)

BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    ("\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m")
    if sys.stdout.isatty()
    else ("", "", "", "", "", "")
)


def _say(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def _spec_from(args: argparse.Namespace) -> JobSpec:
    return JobSpec(
        motion=MotionSpec(
            action=getattr(args, "action", "wave"),
            description=getattr(args, "describe", "") or "",
            name=getattr(args, "name", "Mascot"),
        ),
        sheet=SheetSpec(
            rows=args.rows,
            cols=args.cols,
            fps=args.fps,
            base_pt=args.base_pt,
            ping_pong=None if args.ping_pong == "auto" else args.ping_pong == "on",
        ),
        cutout=CutoutSpec(
            method=args.cutout,
            key_color=args.key_color,
            auto_key="none" if args.no_auto_key else "corners",
        ),
    )


# --------------------------------------------------------------------- commands


def cmd_motions(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(MOTIONS, indent=2))
        return 0
    width = max(len(k) for k in MOTIONS)
    for k, v in MOTIONS.items():
        print(f"  {BOLD}{k:<{width}}{RESET}  {v}")
    return 0


def cmd_anchor(args: argparse.Namespace) -> int:
    """Emit the prompt for the one still that locks the character's identity."""
    prompt = prompts.anchor_prompt(args.character, CutoutSpec(key_color=args.key_color))
    out = Path(args.out).resolve()
    if args.prompt_only:
        print(prompt)
        return 0
    print(
        prompts.agent_brief(
            str(out),
            prompt,
            f"{args.size}x{args.size}",
            next_command=f"mascotify plan --ref {out} --action <motion>",
            next_note=(
                "Look at the anchor first. It is the identity source for every animation that\n"
                "follows, so regenerate now if the character is cropped, sitting on a shadow, or\n"
                "not what was asked for. Run `mascotify motions` to see the motion vocabulary."
            ),
            size_note="a square anchor keeps the character centred",
        )
    )
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Create a job and emit the sheet prompt for it."""
    root = Path(args.root).resolve()
    spec = _spec_from(args)
    if args.ref:
        ref = Path(args.ref).resolve()
        if not ref.exists():
            _say(f"{RED}reference image not found: {ref}{RESET}")
            return 1
        spec.reference_sha = pipeline.sha(ref)

    job = Job(root=root, spec=spec, name=args.job or spec.motion.action)
    job.prepare()

    prompt = prompts.sheet_prompt(spec, character=args.describe or "", from_reference=bool(args.ref))
    size = prompts.sheet_size(spec.sheet, cell=args.cell)
    (job.dir / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")

    if args.prompt_only:
        print(prompt)
        return 0

    if args.ref:
        _say(f"{DIM}reference: {args.ref} (sha {spec.reference_sha}){RESET}")
    _say(f"{DIM}job: {job.dir}{RESET}\n")
    print(
        prompts.agent_brief(
            str(job.sheet_path),
            prompt,
            size,
            next_command=f"mascotify ingest {job.sheet_path}",
            next_note=(
                "That validates the sheet and, if it passes, exports every platform bundle.\n"
                "If it reports problems it prints a repair prompt — regenerate with that and\n"
                "ingest again. Do not hand-edit the image."
            ),
        )
    )
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Validate a generated sheet and, if it passes, export every target."""
    root = Path(args.root).resolve()
    sheet = Path(args.sheet).resolve()
    if not sheet.exists():
        _say(f"{RED}sheet not found: {sheet}{RESET}")
        return 1

    name = args.job
    if name:
        job = Job.open(root, name)
    else:
        # Infer the job from where the sheet was written, so the agent can pass
        # back exactly the path `plan` handed it.
        parent = sheet.parent
        if (parent / "job.json").exists():
            job = Job.open(root, parent.name)
        else:
            job = Job(root=root, spec=_spec_from(args), name=args.name.lower())
            job.prepare()

    if sheet != job.sheet_path:
        job.sheet_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sheet, job.sheet_path)

    targets = tuple(args.targets.split(",")) if args.targets else TARGETS
    result = pipeline.process(job.sheet_path, job, targets=targets, strict=not args.force)
    rep = result.report

    _say(f"{BOLD}grid{RESET}  {rep.found}/{rep.expected} frames, {rep.rows}x{rep.cols}")
    _say(f"{BOLD}scale{RESET} spread {rep.scale_spread:.1%}  {DIM}(budget 15%){RESET}")
    if rep.clipped:
        _say(f"{BOLD}clip{RESET}  frames {', '.join(str(i + 1) for i in rep.clipped)}")

    # No bundle guard here, unlike the pose path: a motion sheet has no cell
    # whose position carries meaning, so `--force` really can export a loop
    # whose frames are merely a little uneven.
    if not rep.ok and not args.force:
        _say(f"\n{RED}validation failed{RESET}")
        for p in rep.problems():
            _say(f"  {RED}!{RESET} {p}")
        _say(f"\n{YELLOW}Regenerate with this follow-up, then ingest again:{RESET}\n")
        print(prompts.repair_prompt(rep.problems(), job.spec))
        return 2

    _say(f"{BOLD}seam{RESET}  {result.seam:.2f}x an average step"
         f"{'  → ping-pong applied' if result.ping_pong else ''}")
    _say(f"\n{GREEN}exported{RESET} {len(result.frames)} frames at {job.spec.sheet.fps} fps")
    for b in result.bundles:
        _say(f"  {BOLD}{b.target:<8}{RESET}{b.root}")
        _say(f"           {DIM}{b.snippet.splitlines()[0]}{RESET}")
        if b.install_hint:
            _say(f"           {DIM}{b.install_hint}{RESET}")
    if result.preview:
        _say(f"\npreview  {result.preview}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Measure a sheet without writing anything."""
    sheet = Path(args.sheet).resolve()
    if not sheet.exists():
        _say(f"{RED}not found: {sheet}{RESET}")
        return 1
    spec = _spec_from(args)
    _, rep = pipeline.analyse(Image.open(sheet), spec)
    print(
        json.dumps(
            {
                "ok": rep.ok,
                "frames_found": rep.found,
                "frames_expected": rep.expected,
                "rows": rep.rows,
                "cols": rep.cols,
                "scale_spread": round(rep.scale_spread, 4),
                "baseline_spread": round(rep.baseline_spread, 4),
                "clipped": [i + 1 for i in rep.clipped],
                "problems": rep.problems(),
            },
            indent=2,
        )
    )
    return 0 if rep.ok else 2


def cmd_compare(args: argparse.Namespace) -> int:
    """Build a size-matched strip of the anchor next to generated frames.

    Deliberately reports numbers as context and never passes or fails: the cheap
    identity metrics contradict each other on real drift, so the judgement is
    the caller's to make from the image.
    """
    from .imaging import compare as cmp_mod

    ref_path, gen_path = Path(args.ref).resolve(), Path(args.against).resolve()
    for p in (ref_path, gen_path):
        if not p.exists():
            _say(f"{RED}not found: {p}{RESET}")
            return 1

    spec = _spec_from(args)
    anchor_cut, _ = pipeline.analyse(Image.open(ref_path), spec)
    gen_cut, rep = pipeline.analyse(Image.open(gen_path), spec)

    from .imaging import grid as grid_mod, normalize as nz_mod

    if rep.found > 1:
        frames = nz_mod.normalize(grid_mod.slice_frames(gen_cut, rep))
    else:
        frames = nz_mod.normalize([gen_cut])

    result = cmp_mod.build(anchor_cut, frames, sample=args.sample)
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    result.strip.save(out)

    _say(f"{GREEN}wrote{RESET} {out}")
    for n in result.notes():
        _say(f"  {DIM}{n}{RESET}")
    _say(f"\n{YELLOW}Look at the strip.{RESET} Same character, or has it drifted?")
    return 0


def cmd_video(args: argparse.Namespace) -> int:
    from .video.runner import run_video

    return run_video(args)


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the local web UI.

    Unlike every other command this one needs a provider key, because the
    browser has no coding agent to generate with. The key stays in this
    process's memory.
    """
    try:
        import uvicorn
    except ImportError:
        _say(f"{RED}error:{RESET} the web UI needs: pip install 'mascotify[web]'")
        return 1
    from .web.app import create_app

    root = Path(args.root).resolve()
    url = f"http://{args.host}:{args.port}"
    _say(f"{BOLD}mascotify{RESET} serving {DIM}{root}{RESET}")
    _say(f"  {GREEN}{url}{RESET}\n")
    if not args.no_open:
        import threading
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    uvicorn.run(create_app(root), host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_poses(args: argparse.Namespace) -> int:
    """List the pose vocabulary a cursor-tracking mascot is built from."""
    if args.json:
        print(json.dumps({"directions": dict(DIRECTIONS), "reactions": REACTIONS}, indent=2))
        return 0
    _say(f"{BOLD}directions{RESET} {DIM}— read by compass position; this order is the contract{RESET}")
    for i, (key, desc) in enumerate(DIRECTIONS):
        _say(f"  {DIM}cell {i + 1}{RESET}  {key:<11} {desc}")
    _say(f"\n{BOLD}reactions{RESET} {DIM}— one is shown on a click; order does not matter{RESET}")
    for key, desc in REACTIONS.items():
        _say(f"  {key:<11} {desc}")
    return 0


def _pose_spec_from(args: argparse.Namespace) -> PoseJobSpec:
    return PoseJobSpec(
        name=args.job or "mascot",
        character=args.describe or "",
        pose=PoseSpec(base_px=args.size),
        cutout=CutoutSpec(
            method=args.cutout,
            key_color=args.key_color,
            auto_key="none" if args.no_auto_key else "corners",
        ),
    )


def cmd_pose_plan(args: argparse.Namespace) -> int:
    """Create a pose job and emit the prompts for both grids."""
    root = Path(args.root).resolve()
    spec = _pose_spec_from(args)

    ref = Path(args.ref).resolve()
    if not ref.exists():
        _say(f"{RED}reference image not found: {ref}{RESET}")
        return 1
    spec.anchor_sha = pipeline.sha(ref)

    job = PoseJob(root=root, spec=spec)
    job.prepare()

    texts = {
        "directions": prompts.directions_prompt(spec),
        "reactions": prompts.reactions_prompt(spec),
    }
    for which, text in texts.items():
        (job.dir / f"{which}-prompt.txt").write_text(text + "\n", encoding="utf-8")

    if args.prompt_only:
        for which, text in texts.items():
            print(f"--- {which} ---\n{text}\n")
        return 0

    size = f"{spec.pose.cols * args.cell}x{spec.pose.rows * args.cell}"
    ingest = (
        f"mascotify pose-ingest --directions {job.sheet_path('directions')}"
        f" --reactions {job.sheet_path('reactions')}"
    )

    _say(f"{DIM}reference: {args.ref} (sha {spec.anchor_sha}){RESET}")
    _say(f"{DIM}job: {job.dir}{RESET}\n")

    # Both prompts go out together because both sheets are ingested together —
    # they are normalised as one set, so there is no half-finished state worth
    # stopping at, and a reader who runs only the first gets nothing.
    for n, (which, text) in enumerate(texts.items(), start=1):
        print(
            prompts.agent_brief(
                str(job.sheet_path(which)),
                text,
                size,
                next_command=(
                    f"# sheet {n} of 2 — generate the other one too, then run:\n  {ingest}"
                    if n == 1
                    else ingest
                ),
                next_note=(
                    "Look at the sheet before moving on. The cell order carries the meaning "
                    "here:\ncell 5 must be the resting pose and the head must actually turn "
                    "towards the\ncorner its cell sits in. Nothing downstream can check that "
                    "— a grid with the\nnine directions shuffled validates perfectly and "
                    "tracks the cursor wrong."
                    if which == "directions"
                    else "That normalises both sheets together and exports the component "
                    "bundle.\nIf it reports problems it prints a repair prompt — regenerate "
                    "with that and\ningest again. Do not hand-edit the image."
                ),
            )
        )
        print()
    return 0


def cmd_pose_ingest(args: argparse.Namespace) -> int:
    """Validate both pose grids together and export the page-mascot bundle."""
    root = Path(args.root).resolve()
    paths = {}
    for which in ("directions", "reactions"):
        p = Path(getattr(args, which)).resolve()
        if not p.exists():
            _say(f"{RED}{which} sheet not found: {p}{RESET}")
            return 1
        paths[which] = p

    name = args.job
    if not name:
        # Infer from where the sheets were written, so the agent can pass back
        # exactly the paths `pose-plan` handed it.
        parent = paths["directions"].parent
        name = parent.name if (parent / "job.json").exists() else "mascot"

    try:
        job = PoseJob.open(root, name)
    except FileNotFoundError:
        job = PoseJob(root=root, spec=_pose_spec_from(args))
        job.spec.name = name
        job.prepare()

    for which, src in paths.items():
        dst = job.sheet_path(which)
        if src != dst:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    result = pose.process_pair(
        job.sheet_path("directions"),
        job.sheet_path("reactions"),
        job,
        strict=not args.force,
    )
    rep = result.report

    for which, r in (("directions", rep.directions), ("reactions", rep.reactions)):
        _say(f"{BOLD}{which:<11}{RESET}{r.found}/{r.expected} cells, {r.rows}x{r.cols}")
    _say(f"{BOLD}anchor{RESET}     drifts {rep.anchor_drift:.1%} of character width "
         f"{DIM}(budget {rep.max_drift:.0%}){RESET}")
    _say(f"{BOLD}scale{RESET}      reactions at {rep.scale_match:.0%} of directions "
         f"{DIM}(budget 90-110%){RESET}")

    if result.bundle is None or (not rep.ok and not args.force):
        _say(f"\n{RED}validation failed{RESET}")
        if result.bundle is None and args.force:
            _say(
                f"  {DIM}--force cannot bypass a wrong cell count — which cell holds which "
                f"pose is the contract, not a tolerance{RESET}"
            )
        for p in rep.problems():
            _say(f"  {RED}!{RESET} {p}")
        # Name the sheet that actually has to be redrawn. Falling back to a
        # substring search over the whole problem list sent the agent to
        # regenerate "reactions" for a fault measured on the directions sheet.
        which = next(
            (w for w in ("directions", "reactions")
             if any(p.startswith(f"{w}:") for p in rep.problems())),
            "reactions",
        )
        _say(f"\n{YELLOW}Regenerate with this follow-up, then ingest again:{RESET}\n")
        print(prompts.pose_repair_prompt(rep.problems(), job.spec, which))
        return 2

    b = result.bundle
    _say(f"\n{GREEN}exported{RESET} two 3x3 sheets to {b.root}")
    _say(f"  {DIM}{b.install_hint}{RESET}\n")
    print(b.snippet)
    return 0


def cmd_skill(args: argparse.Namespace) -> int:
    """Install the agent skill so a coding agent can drive mascotify itself."""
    src = Path(__file__).parent / "assets" / "SKILL.md"
    dests = {
        "claude": Path.home() / ".claude" / "skills" / "mascotify",
        "codex": Path.home() / ".codex" / "skills" / "mascotify",
    }
    chosen = dests if args.agent == "all" else {args.agent: dests[args.agent]}
    for agent, dest in chosen.items():
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / "SKILL.md")
        _say(f"{GREEN}installed{RESET} {agent}: {dest / 'SKILL.md'}")
    return 0


# ------------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mascotify",
        description="Turn one character image into production-ready animated mascot assets.",
        epilog="Default mode needs no API key: your coding agent does the generating.",
    )
    p.add_argument("--version", action="version", version=f"mascotify {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def shared(sp: argparse.ArgumentParser, *, motion: bool = True) -> None:
        if motion:
            sp.add_argument(
                "--action", default="wave", help=f"motion name ({', '.join(MOTIONS)}) or free text"
            )
            sp.add_argument("--describe", default="", help="character description, if no reference")
        sp.add_argument("--name", default="Mascot", help="display name used in generated code")
        sp.add_argument("--rows", type=int, default=3)
        sp.add_argument("--cols", type=int, default=4)
        sp.add_argument("--fps", type=int, default=12)
        sp.add_argument("--base-pt", type=int, default=120, help="rendered size in iOS points")
        sp.add_argument("--cutout", choices=["chroma", "matte", "alpha"], default="chroma")
        sp.add_argument("--key-color", default="#00FF00")
        sp.add_argument(
            "--no-auto-key",
            action="store_true",
            help="trust --key-color instead of sampling the real backdrop (rarely right)",
        )
        sp.add_argument("--ping-pong", choices=["auto", "on", "off"], default="auto")
        sp.add_argument("--root", default=".", help="project root holding .mascotify/")

    m = sub.add_parser("motions", help="list the motion vocabulary")
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_motions)

    a = sub.add_parser("anchor", help="prompt for the canonical still that locks identity")
    a.add_argument("character", help="what the mascot looks like")
    a.add_argument("--out", default="ref.png")
    a.add_argument("--size", type=int, default=1024)
    a.add_argument("--key-color", default="#00FF00")
    a.add_argument("--prompt-only", action="store_true")
    a.set_defaults(func=cmd_anchor)

    pl = sub.add_parser("plan", help="create a job and emit its generation prompt")
    pl.add_argument("--ref", help="approved anchor image to keep the character consistent")
    pl.add_argument("--job", help="job name (defaults to the action)")
    pl.add_argument("--cell", type=int, default=512, help="target pixels per grid cell")
    pl.add_argument("--prompt-only", action="store_true")
    shared(pl)
    pl.set_defaults(func=cmd_plan)

    i = sub.add_parser("ingest", help="validate a generated sheet and export every target")
    i.add_argument("sheet")
    i.add_argument("--job", help="job name to ingest into")
    i.add_argument("--targets", help=f"comma-separated subset of: {','.join(TARGETS)}")
    i.add_argument("--force", action="store_true", help="export even if validation failed")
    shared(i)
    i.set_defaults(func=cmd_ingest)

    d = sub.add_parser("doctor", help="measure a sheet and report, writing nothing")
    d.add_argument("sheet")
    shared(d)
    d.set_defaults(func=cmd_doctor)

    cp = sub.add_parser(
        "compare", help="size-matched strip of the anchor beside generated frames"
    )
    cp.add_argument("--ref", required=True, help="the approved anchor")
    cp.add_argument("--against", required=True, help="a generated sheet or single frame")
    cp.add_argument("--out", default="compare.png")
    cp.add_argument("--sample", type=int, default=4, help="how many frames to show")
    shared(cp, motion=False)
    cp.set_defaults(func=cmd_compare)

    v = sub.add_parser("video", help="image-to-video path (needs a provider key)")
    v.add_argument("--ref", required=True, help="approved anchor image")
    v.add_argument("--action", default="wave")
    v.add_argument("--describe", default="")
    v.add_argument("--provider", choices=["fal", "replicate"], default="fal")
    v.add_argument("--model", help="override the provider's default model")
    v.add_argument("--duration", type=int, default=5, help="seconds")
    v.add_argument("--fps", type=int, default=24)
    v.add_argument("--max-frames", type=int, default=120)
    v.add_argument("--name", default="Mascot")
    v.add_argument("--rows", type=int, default=0, help="unused; kept for spec compatibility")
    v.add_argument("--cols", type=int, default=0)
    v.add_argument("--base-pt", type=int, default=120)
    # The clip is generated from an anchor that is already on a flat key, and
    # the video prompt demands the backdrop stays that colour — so chroma is
    # the matching default here too. `matte` is the fallback when the model
    # lets the background drift, and it needs the matting extra.
    v.add_argument("--cutout", choices=["chroma", "matte", "alpha"], default="chroma")
    v.add_argument("--key-color", default="#00FF00")
    v.add_argument("--targets", help=f"comma-separated subset of: {','.join(TARGETS)}")
    v.add_argument("--root", default=".")
    v.add_argument("--job", help="job name")
    v.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    v.set_defaults(func=cmd_video)

    sv = sub.add_parser("serve", help="run the local web UI (needs a provider key)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--root", default=".", help="project root holding .mascotify/")
    sv.add_argument("--no-open", action="store_true", help="do not open a browser")
    sv.set_defaults(func=cmd_serve)

    def pose_shared(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--job", help="job name (defaults to 'mascot')")
        sp.add_argument("--describe", default="", help="character description, for the alt text")
        sp.add_argument("--size", type=int, default=140, help="rendered px box in the component")
        sp.add_argument("--cutout", choices=["chroma", "matte", "alpha"], default="chroma")
        sp.add_argument("--key-color", default="#00FF00")
        sp.add_argument("--no-auto-key", action="store_true")
        sp.add_argument("--root", default=".", help="project root holding .mascotify/")

    po = sub.add_parser("poses", help="list the direction and reaction vocabulary")
    po.add_argument("--json", action="store_true")
    po.set_defaults(func=cmd_poses)

    pp = sub.add_parser(
        "pose-plan", help="prompts for the two 3x3 grids a cursor-tracking mascot needs"
    )
    pp.add_argument("--ref", required=True, help="approved anchor image")
    pp.add_argument("--cell", type=int, default=512, help="target pixels per grid cell")
    pp.add_argument("--prompt-only", action="store_true")
    pose_shared(pp)
    pp.set_defaults(func=cmd_pose_plan)

    # Named flags rather than two positionals: swapped sheets validate perfectly
    # and produce a mascot that turns its head when poked and changes expression
    # when the cursor moves.
    pi = sub.add_parser("pose-ingest", help="validate both grids together and export")
    pi.add_argument("--directions", required=True, help="the nine head directions")
    pi.add_argument("--reactions", required=True, help="the nine expressions")
    pi.add_argument("--force", action="store_true", help="export even if validation failed")
    pose_shared(pi)
    pi.set_defaults(func=cmd_pose_ingest)

    s = sub.add_parser("install-skill", help="install the agent skill")
    s.add_argument("--agent", choices=["claude", "codex", "all"], default="all")
    s.set_defaults(func=cmd_skill)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        _say(f"{RED}error:{RESET} {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
