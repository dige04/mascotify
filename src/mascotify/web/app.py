"""Local web app.

Same pipeline as the CLI — this module only adds an HTTP surface. Nothing here
re-implements keying, grid detection, normalisation or export; if it did, the
web output and the CLI output would drift apart and neither would be
reproducible.

The default provider is "agent", which drives the user's own coding agent and
needs no key. Keyed providers are offered for speed, and a key pasted into
settings stays in this process's memory — never written to disk, never returned
to the browser.
"""

from __future__ import annotations

import io
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

try:
    from fastapi import Body, FastAPI, HTTPException, UploadFile
    from fastapi.responses import FileResponse, JSONResponse, Response
    from fastapi.staticfiles import StaticFiles
except ImportError as exc:  # pragma: no cover - optional extra
    raise SystemExit("the web app needs: pip install 'mascotify[web]'") from exc

from .. import __version__, pipeline, pose
from ..export.targets import TARGETS
from ..gen import images, prompts
from ..imaging import compare as cmp_mod
from ..imaging import grid as grid_mod
from ..imaging import normalize as nz
from ..imaging.cutout import cut_out
from ..pipeline import Job
from ..spec import (
    BLINK_POSE,
    MOTIONS,
    REACTIONS,
    POSE_SETS,
    CutoutSpec,
    JobSpec,
    MotionSpec,
    PoseJobSpec,
    SheetSpec,
)

STATIC = Path(__file__).parent / "static"
# Only this machine's own browser may talk to the server. Binding elsewhere is
# possible but has to be asked for, because there is no authentication here —
# the security model is "it is on your loopback".
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000


@dataclass
class Session:
    """In-memory state for one browser. Keys live here and nowhere else."""

    root: Path
    keys: dict[str, str] = field(default_factory=dict)
    provider: str = "agent"
    model: str = ""

    def key_for(self, provider: str) -> str | None:
        return self.keys.get(provider) or None


def _hostname(value: str) -> str:
    """Host or Origin header down to its bare hostname."""
    value = value.strip()
    if "//" in value:
        value = value.split("//", 1)[1]
    value = value.split("/", 1)[0]
    if value.startswith("["):  # bracketed IPv6
        return value[1 : value.index("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if ":" in value else value


def create_app(root: Path | None = None, *, allow_hosts: set[str] | None = None) -> FastAPI:
    app = FastAPI(title="mascotify", version=__version__)
    state = Session(root=(root or Path.cwd()).resolve())
    allowed = allow_hosts if allow_hosts is not None else set(LOCAL_HOSTS)

    @app.middleware("http")
    async def guard_origin(request, call_next):
        """Refuse requests that did not come from this machine's own browser tab.

        Two attacks matter for a local server that holds an API key and can
        spend money. DNS rebinding points an attacker's domain at 127.0.0.1 so
        their page becomes same-origin and CORS stops applying — checking the
        Host header is what stops it, because the browser still sends their
        domain there. And `multipart/form-data` is CORS-safelisted, so any page
        can POST an upload cross-origin with no preflight; checking Origin on
        state-changing methods closes that.
        """
        if allowed:
            host = _hostname(request.headers.get("host", ""))
            if host and host not in allowed:
                return JSONResponse(
                    {
                        "detail": (
                            f"refused a request for host {host!r}. mascotify serve only "
                            f"answers to {', '.join(sorted(allowed))}; this protects the "
                            f"key and the agent quota it can spend."
                        )
                    },
                    status_code=403,
                )

            origin = request.headers.get("origin")
            if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
                if _hostname(origin) not in allowed:
                    return JSONResponse(
                        {"detail": f"refused a cross-origin request from {origin!r}"},
                        status_code=403,
                    )
        return await call_next(request)

    def _job(name: str) -> Job:
        try:
            return Job.open(state.root, name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    async def _read_image(file: UploadFile) -> bytes:
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "image exceeds the 32 MiB upload limit")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
                    raise HTTPException(413, "image exceeds the 64 megapixel limit")
                image.verify()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, "that file is not a readable image") from exc
        return raw

    def _text(body: dict, field: str, default: str = "") -> str:
        value = body.get(field, default)
        if not isinstance(value, str):
            raise HTTPException(400, f"{field} must be a string")
        return value.strip()

    def _integer(body: dict, field: str, default: int) -> int:
        value = body.get(field, default)
        if isinstance(value, bool):
            raise HTTPException(400, f"{field} must be an integer")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"{field} must be an integer") from exc

    def _png(img: Image.Image) -> Response:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(buf.getvalue(), media_type="image/png")

    # ---------------------------------------------------------------- config

    @app.get("/api/config")
    def config() -> dict:
        return {
            "version": __version__,
            "root": str(state.root),
            "motions": MOTIONS,
            "providers": list(images.PROVIDERS),
            "provider": state.provider,
            "model": state.model or images.DEFAULT_MODELS[state.provider],
            "default_models": images.DEFAULT_MODELS,
            "targets": list(TARGETS),
            # Which providers already have a key from the environment, so the
            # UI can say "ready" without ever sending the key to the browser.
            "keyed": {p: bool(state.key_for(p)) or images.is_ready(p) for p in images.PROVIDERS},
            "cost": images.ROUGH_COST_USD,
            "needs_key": sorted(images.NEEDS_KEY),
            # FastAPI's @app.get does not answer HEAD, so the UI cannot probe
            # for the anchor with one. Report it here instead of adding a route.
            "has_anchor": (state.root / pipeline.WORKSPACE / "ref.png").exists(),
        }

    @app.post("/api/settings")
    def settings(body: dict = Body(...)) -> dict:
        p = _text(body, "provider", state.provider)
        if p not in images.PROVIDERS:
            raise HTTPException(400, f"unknown provider {p!r}")
        state.provider = p
        if "model" in body:
            state.model = _text(body, "model")
        # A blank key means "leave whatever is already set" rather than "clear",
        # so re-saving other settings does not silently wipe it.
        if k := _text(body, "key"):
            state.keys[p] = k
        return config()

    # ----------------------------------------------------------- generation

    @app.post("/api/anchor")
    def anchor(body: dict = Body(...)) -> dict:
        character = _text(body, "character")
        if not character:
            raise HTTPException(400, "describe the character first")

        prompt = prompts.anchor_prompt(character, CutoutSpec())
        try:
            out = images.generate(
                state.provider,
                prompt=prompt,
                key=state.key_for(state.provider),
                model=state.model or None,
                size="1024x1024",
            )
        except images.ProviderError as exc:
            raise HTTPException(400, str(exc)) from exc

        d = state.root / pipeline.WORKSPACE
        d.mkdir(parents=True, exist_ok=True)
        (d / "ref.png").write_bytes(out.png)
        return {"ref": "ref.png", "model": out.model, "provider": out.provider}

    @app.post("/api/anchor/upload")
    async def anchor_upload(file: UploadFile) -> dict:
        raw = await _read_image(file)
        d = state.root / pipeline.WORKSPACE
        d.mkdir(parents=True, exist_ok=True)
        (d / "ref.png").write_bytes(raw)
        return {"ref": "ref.png", "uploaded": True}

    @app.get("/api/ref")
    def ref_image(cut: int = 0):
        """The anchor, optionally with its backdrop keyed out.

        The UI asks for the cut version: showing the raw green plate on a
        transparency checkerboard reads as a bug, and the keyed one doubles as
        instant feedback on whether the cutout will hold before any money is
        spent on a sheet.
        """
        p = state.root / pipeline.WORKSPACE / "ref.png"
        if not p.exists():
            raise HTTPException(404, "no anchor yet")
        if not cut:
            return FileResponse(p, media_type="image/png")
        return _png(cut_out(Image.open(p), CutoutSpec()))

    @app.post("/api/animate")
    def animate(body: dict = Body(...)) -> dict:
        """Anchor to exported bundles, validating in between.

        Returns the validator's problems rather than raising on them: a failed
        sheet is a normal outcome that the caller re-rolls, not an error.
        """
        ref_path = state.root / pipeline.WORKSPACE / "ref.png"
        if not ref_path.exists():
            raise HTTPException(400, "make or upload an anchor first")

        action = _text(body, "action", "wave") or "wave"
        rows = _integer(body, "rows", 3)
        cols = _integer(body, "cols", 4)
        fps = _integer(body, "fps", 12)

        spec = JobSpec(
            motion=MotionSpec(action=action, description=_text(body, "describe")),
            sheet=SheetSpec(rows=rows, cols=cols, fps=fps),
        )
        try:
            spec.sheet.validate()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        spec.reference_sha = pipeline.sha(ref_path)
        job = Job(root=state.root, spec=spec, name=pipeline.job_name(action))
        job.prepare()

        prompt = prompts.sheet_prompt(spec, from_reference=True)
        try:
            out = images.generate(
                state.provider,
                prompt=prompt,
                key=state.key_for(state.provider),
                model=state.model or None,
                size=prompts.sheet_size(spec.sheet),
                reference=ref_path.read_bytes(),
            )
        except images.ProviderError as exc:
            raise HTTPException(400, str(exc)) from exc

        job.sheet_path.write_bytes(out.png)
        result = pipeline.process(job.sheet_path, job, strict=True)
        rep = result.report

        if not rep.ok:
            return {
                "ok": False,
                "job": job.name,
                "problems": rep.problems(),
                "frames_found": rep.found,
                "frames_expected": rep.expected,
                "model": out.model,
            }

        return {
            "ok": True,
            "job": job.name,
            "frames": len(result.frames),
            "fps": fps,
            "seam": round(result.seam, 3),
            "ping_pong": result.ping_pong,
            "scale_spread": round(rep.scale_spread, 4),
            "model": out.model,
            "exports": [
                {"target": b.target, "snippet": b.snippet, "files": len(b.files)}
                for b in result.bundles
            ],
        }

    @app.post("/api/ingest")
    async def ingest(
        file: UploadFile,
        action: str = "wave",
        rows: int = 3,
        cols: int = 4,
        fps: int = 12,
    ) -> dict:
        """Validate and export a sheet that was generated elsewhere.

        The key-free route, and the one that matches how the CLI is used: your
        coding agent draws the grid, this validates it and writes the bundles.
        Everything past this point is identical to the generated path.
        """
        raw = await _read_image(file)

        spec = JobSpec(
            motion=MotionSpec(action=action),
            sheet=SheetSpec(rows=int(rows), cols=int(cols), fps=int(fps)),
        )
        try:
            spec.sheet.validate()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        job = Job(root=state.root, spec=spec, name=pipeline.job_name(action))
        job.prepare()
        job.sheet_path.write_bytes(raw)

        result = pipeline.process(job.sheet_path, job, strict=True)
        rep = result.report
        if not rep.ok:
            return {
                "ok": False,
                "job": job.name,
                "problems": rep.problems(),
                "frames_found": rep.found,
                "frames_expected": rep.expected,
                "model": "uploaded",
            }
        return {
            "ok": True,
            "job": job.name,
            "frames": len(result.frames),
            "fps": spec.sheet.fps,
            "seam": round(result.seam, 3),
            "ping_pong": result.ping_pong,
            "scale_spread": round(rep.scale_spread, 4),
            "model": "uploaded",
            "exports": [
                {"target": b.target, "snippet": b.snippet, "files": len(b.files)}
                for b in result.bundles
            ],
        }

    # ----------------------------------------------------------------- cast

    @app.get("/api/cast")
    def cast() -> list[dict]:
        """The pose pairs this project has exported, for the masthead.

        Read off disk rather than bundled with the package. A wall of stock
        characters would weigh about a megabyte in the wheel and would be
        someone else's art; this way the masthead fills up with the mascots you
        made, and a fresh project falls back to the one that ships.
        """
        base = state.root / pose.WORKSPACE / "poses"
        if not base.is_dir():
            return []
        # Which cell the idle blink borrows. Reactions cell order is explicitly
        # free, so the client must be told rather than assume an index — a
        # hardcoded 4 turns the blink into heart-eyes the day REACTIONS is
        # reordered, and nothing validates which expression landed where.
        blink = list(REACTIONS).index(BLINK_POSE)
        out = []
        for d in sorted(base.iterdir()):
            spec = d / "job.json"
            export = d / "export" / "page-mascot"
            if not spec.exists() or not export.is_dir():
                continue
            name = d.name
            if not (export / f"{name}-directions.webp").exists():
                continue
            try:
                described = PoseJobSpec.load(spec).character
            except (OSError, ValueError):
                described = ""
            out.append(
                {"id": name, "alt": described or f"{name} mascot", "blink": blink}
            )
        return out

    @app.get("/api/cast/{job}/{which}")
    def cast_sheet(job: str, which: str):
        if which not in POSE_SETS:
            raise HTTPException(404, "no such sheet")
        try:
            pipeline.validate_job_name(job)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        p = (state.root / pose.WORKSPACE / "poses" / job / "export" / "page-mascot"
             / f"{job}-{which}.webp")
        if not p.exists():
            raise HTTPException(404, "no such sheet")
        return FileResponse(p, media_type="image/webp")

    # -------------------------------------------------------------- results

    @app.get("/api/preview/{job}")
    def preview(job: str):
        p = _job(job).dir / "preview.webp"
        if not p.exists():
            raise HTTPException(404, "no preview")
        return FileResponse(p, media_type="image/webp")

    @app.get("/api/sheet/{job}")
    def sheet(job: str):
        p = _job(job).dir / "sheet.png"
        if not p.exists():
            raise HTTPException(404, "no sheet")
        return FileResponse(p, media_type="image/png")

    @app.get("/api/compare/{job}")
    def compare(job: str):
        j = _job(job)
        ref_path = state.root / pipeline.WORKSPACE / "ref.png"
        if not ref_path.exists() or not j.sheet_path.exists():
            raise HTTPException(404, "need an anchor and a sheet")

        anchor_cut, _ = pipeline.analyse(Image.open(ref_path), j.spec)
        gen_cut, rep = pipeline.analyse(Image.open(j.sheet_path), j.spec)
        frames = (
            nz.normalize(grid_mod.slice_frames(gen_cut, rep))
            if rep.found > 1
            else nz.normalize([gen_cut])
        )
        return _png(cmp_mod.build(anchor_cut, frames).strip)

    @app.get("/api/export/{job}")
    def export_zip(job: str, targets: str = ""):
        """Everything the job produced, as one download."""
        j = _job(job)
        if not j.out_dir.exists():
            raise HTTPException(404, "nothing exported for this job")
        wanted = set(targets.split(",")) if targets else set(TARGETS)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for path in sorted(j.out_dir.rglob("*")):
                if path.is_file() and path.relative_to(j.out_dir).parts[0] in wanted:
                    z.write(path, path.relative_to(j.out_dir))
        return Response(
            buf.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{pipeline.asset_name(job)}-mascot.zip"'
                )
            },
        )

    @app.get("/api/jobs")
    def jobs() -> dict:
        base = state.root / pipeline.WORKSPACE
        if not base.exists():
            return {"jobs": []}
        return {
            "jobs": sorted(
                d.name for d in base.iterdir() if d.is_dir() and (d / "job.json").exists()
            )
        }

    @app.get("/api/jobs/{job}")
    def job_detail(job: str) -> dict:
        """Enough to redraw a finished job, so reloading does not lose it."""
        j = _job(job)
        report = j.dir / "report.json"
        if not report.exists() or not j.out_dir.exists():
            raise HTTPException(404, "that job has no finished result")

        import json as _json

        rep = _json.loads(report.read_text())
        if not rep.get("ok"):
            return {"ok": False, "job": job, "problems": rep.get("problems", [])}

        frames = len(list(j.frames_dir.glob("*.png"))) if j.frames_dir.exists() else 0
        snippets = rep.get("snippets", {})
        return {
            "ok": True,
            "job": job,
            "frames": frames,
            "fps": j.spec.sheet.fps,
            "seam": rep.get("seam", 0),
            "ping_pong": frames > j.spec.sheet.frames,
            "scale_spread": rep.get("scale_spread", 0),
            "model": "restored",
            "exports": [
                {
                    "target": d.name,
                    "snippet": snippets.get(d.name, ""),
                    "files": len(list(d.rglob("*"))),
                }
                for d in sorted(j.out_dir.iterdir())
                if d.is_dir()
            ],
        }

    @app.delete("/api/jobs/{job}")
    def delete_job(job: str) -> dict:
        try:
            pipeline.validate_job_name(job)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        d = (state.root / pipeline.WORKSPACE / job).resolve()
        base = (state.root / pipeline.WORKSPACE).resolve()
        # Never let a crafted name escape the workspace.
        if base not in d.parents or not d.is_dir():
            raise HTTPException(404, "no such job")
        shutil.rmtree(d)
        return {"deleted": job}

    @app.exception_handler(images.ProviderError)
    def _provider_error(_, exc: images.ProviderError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
