"""Local web app.

Same pipeline as the CLI — this module only adds a provider (because a browser
has no coding agent to draw with) and an HTTP surface. Nothing here re-implements
keying, grid detection, normalisation or export; if it did, the web output and
the CLI output would drift apart and neither would be reproducible.

Runs locally by default. Keys stay in this process's memory and are never
written to disk or logged.
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

from .. import __version__, pipeline
from ..export.targets import TARGETS
from ..gen import images, prompts
from ..imaging import compare as cmp_mod
from ..imaging.cutout import cut_out
from ..imaging import grid as grid_mod
from ..imaging import normalize as nz
from ..pipeline import Job
from ..spec import MOTIONS, CutoutSpec, JobSpec, MotionSpec, SheetSpec

STATIC = Path(__file__).parent / "static"


@dataclass
class Session:
    """In-memory state for one browser. Keys live here and nowhere else."""

    root: Path
    keys: dict[str, str] = field(default_factory=dict)
    provider: str = "fal"
    model: str = ""

    def key_for(self, provider: str) -> str | None:
        return self.keys.get(provider) or None


def create_app(root: Path | None = None) -> FastAPI:
    app = FastAPI(title="mascotify", version=__version__)
    state = Session(root=(root or Path.cwd()).resolve())

    def _job(name: str) -> Job:
        return Job.open(state.root, name)

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
            "keyed": {
                p: bool(state.key_for(p) or images.os.environ.get(images.ENV_KEYS[p]))
                for p in images.PROVIDERS
            },
            "cost": images.ROUGH_COST_USD,
            # FastAPI's @app.get does not answer HEAD, so the UI cannot probe
            # for the anchor with one. Report it here instead of adding a route.
            "has_anchor": (state.root / pipeline.WORKSPACE / "ref.png").exists(),
        }

    @app.post("/api/settings")
    def settings(body: dict = Body(...)) -> dict:
        if (p := body.get("provider")) in images.PROVIDERS:
            state.provider = p
        if "model" in body:
            state.model = (body.get("model") or "").strip()
        # A blank key means "leave whatever is already set" rather than "clear",
        # so re-saving other settings does not silently wipe it.
        if (k := (body.get("key") or "").strip()) and (p := body.get("provider")):
            state.keys[p] = k
        return config()

    # ----------------------------------------------------------- generation

    @app.post("/api/anchor")
    def anchor(body: dict = Body(...)) -> dict:
        character = (body.get("character") or "").strip()
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
        raw = await file.read()
        try:
            Image.open(io.BytesIO(raw)).verify()
        except Exception as exc:
            raise HTTPException(400, "that file is not a readable image") from exc
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

        action = (body.get("action") or "wave").strip()
        rows = int(body.get("rows") or 3)
        cols = int(body.get("cols") or 4)
        fps = int(body.get("fps") or 12)

        spec = JobSpec(
            motion=MotionSpec(action=action, description=body.get("describe") or ""),
            sheet=SheetSpec(rows=rows, cols=cols, fps=fps),
        )
        try:
            spec.sheet.validate()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        spec.reference_sha = pipeline.sha(ref_path)
        job = Job(root=state.root, spec=spec, name=action)
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
        raw = await file.read()
        try:
            Image.open(io.BytesIO(raw)).verify()
        except Exception as exc:
            raise HTTPException(400, "that file is not a readable image") from exc

        spec = JobSpec(
            motion=MotionSpec(action=action),
            sheet=SheetSpec(rows=int(rows), cols=int(cols), fps=int(fps)),
        )
        try:
            spec.sheet.validate()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        job = Job(root=state.root, spec=spec, name=action)
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
            headers={"Content-Disposition": f'attachment; filename="{job}-mascot.zip"'},
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
