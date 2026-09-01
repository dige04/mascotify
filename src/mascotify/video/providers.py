"""Image-to-video providers.

This is the one tier that cannot be delegated to a coding agent: Codex's
`image_gen` and Gemini CLI make images, not video. So unlike every other path in
mascotify, this one needs a key.

Aggregators are the deliberate choice over direct vendor SDKs — one key reaches
many models, and swapping models is a flag rather than a new integration. That
matters more for an open-source tool than shaving a hop.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

# Model catalogues move faster than any pinned default. These are starting
# points, not guarantees: pass --model to override, and `mascotify video` will
# say so plainly if the provider rejects the id.
DEFAULT_MODELS = {
    "fal": "fal-ai/kling-video/v1.6/standard/image-to-video",
    "replicate": "kwaivgi/kling-v1.6-standard",
}

ENV_KEYS = {"fal": "FAL_KEY", "replicate": "REPLICATE_API_TOKEN"}

# Rough per-clip prices, only ever shown as an estimate before spending.
ROUGH_COST_USD = {"fal": 0.25, "replicate": 0.25}


class ProviderError(RuntimeError):
    pass


@dataclass
class Clip:
    path: Path
    model: str
    provider: str


def _client():
    try:
        import httpx  # noqa: F401
    except ImportError as exc:
        raise ProviderError(
            "the video path needs httpx: pip install 'mascotify[byok]'"
        ) from exc
    import httpx

    return httpx


def require_key(provider: str) -> str:
    env = ENV_KEYS[provider]
    key = os.environ.get(env)
    if not key:
        raise ProviderError(
            f"{provider} needs {env} in the environment.\n"
            f"The video tier is the only part of mascotify that needs a key — "
            f"sprite-grid generation runs through your coding agent for free.\n"
            f"For a longer no-key animation instead, try a bigger grid:\n"
            f"  mascotify plan --rows 4 --cols 6 --fps 24 --action <motion>"
        )
    return key


def generate(
    provider: str,
    *,
    image: Path,
    prompt: str,
    duration: int,
    model: str | None,
    out: Path,
    timeout: float = 600.0,
    poll: float = 3.0,
) -> Clip:
    key = require_key(provider)
    model = model or DEFAULT_MODELS[provider]
    httpx = _client()

    if provider == "fal":
        clip_url = _fal(httpx, key, model, image, prompt, duration, timeout, poll)
    elif provider == "replicate":
        clip_url = _replicate(httpx, key, model, image, prompt, duration, timeout, poll)
    else:
        raise ProviderError(f"unknown provider {provider!r}")

    out.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", clip_url, timeout=timeout, follow_redirects=True) as r:
        r.raise_for_status()
        with out.open("wb") as fh:
            for chunk in r.iter_bytes():
                fh.write(chunk)
    return Clip(path=out, model=model, provider=provider)


def _data_uri(image: Path) -> str:
    import base64
    import mimetypes

    mime = mimetypes.guess_type(image.name)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(image.read_bytes()).decode()


def _fal(httpx, key, model, image, prompt, duration, timeout, poll) -> str:
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    payload = {"image_url": _data_uri(image), "prompt": prompt, "duration": str(duration)}

    with httpx.Client(timeout=timeout) as c:
        r = c.post(f"https://queue.fal.run/{model}", headers=headers, json=payload)
        if r.status_code == 404:
            raise ProviderError(
                f"fal rejected model {model!r}. Check the current id at fal.ai/models "
                f"and pass it with --model."
            )
        r.raise_for_status()
        job = r.json()
        status_url, result_url = job.get("status_url"), job.get("response_url")
        if not status_url:
            raise ProviderError(f"fal returned no status_url: {job}")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = c.get(status_url, headers=headers).json()
            state = s.get("status")
            if state == "COMPLETED":
                data = c.get(result_url, headers=headers).json()
                video = data.get("video") or {}
                url = video.get("url") if isinstance(video, dict) else None
                if not url:
                    raise ProviderError(f"fal completed but returned no video url: {data}")
                return url
            if state in {"FAILED", "ERROR"}:
                raise ProviderError(f"fal generation failed: {s}")
            time.sleep(poll)
    raise ProviderError(f"fal did not finish within {timeout:.0f}s")


def _replicate(httpx, key, model, image, prompt, duration, timeout, poll) -> str:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "input": {
            "start_image": _data_uri(image),
            "prompt": prompt,
            "duration": duration,
        }
    }

    with httpx.Client(timeout=timeout) as c:
        r = c.post(
            f"https://api.replicate.com/v1/models/{model}/predictions",
            headers=headers,
            json=payload,
        )
        if r.status_code == 404:
            raise ProviderError(
                f"replicate rejected model {model!r}. Check the current id at "
                f"replicate.com/explore and pass it with --model."
            )
        r.raise_for_status()
        pred = r.json()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = pred.get("status")
            if status == "succeeded":
                out = pred.get("output")
                url = out[0] if isinstance(out, list) and out else out
                if not isinstance(url, str):
                    raise ProviderError(f"replicate returned no video url: {out}")
                return url
            if status in {"failed", "canceled"}:
                raise ProviderError(f"replicate generation {status}: {pred.get('error')}")
            time.sleep(poll)
            pred = c.get(pred["urls"]["get"], headers=headers).json()
    raise ProviderError(f"replicate did not finish within {timeout:.0f}s")
