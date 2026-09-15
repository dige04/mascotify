"""Image-to-video providers.

This is the one tier that cannot be delegated to a coding agent: Codex's
`image_gen` and Gemini CLI make images, not video. So unlike every other path in
mascotify, this one needs a key — and it is the only path that charges real
money per call, roughly $0.25 a clip.

That makes robustness worth more here than anywhere else. A clip is billed the
moment it is queued, so a transient 502 partway through a two-minute poll used
to throw away something the caller had already paid for. Everything shared with
the image adapters — retries, status-checked polling, redaction, bounded
downloads — lives in `mascotify.net` rather than being written twice and
drifting.

Aggregators are the deliberate choice over direct vendor SDKs: one key reaches
many models, and swapping models is a flag rather than a new integration.
"""

from __future__ import annotations

import base64
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path

from .. import net
from ..net import Redactor

# Model catalogues move faster than any pinned default. These are starting
# points, not guarantees: pass --model to override, and the error names the
# cause when a provider rejects the id.
DEFAULT_MODELS = {
    "fal": "fal-ai/kling-video/v1.6/standard/image-to-video",
    "replicate": "kwaivgi/kling-v1.6-standard",
}

ENV_KEYS = {"fal": "FAL_KEY", "replicate": "REPLICATE_API_TOKEN"}

# Rough per-clip prices, only ever shown as an estimate before spending.
ROUGH_COST_USD = {"fal": 0.25, "replicate": 0.25}

# Overridable so a proxy or gateway can be pointed at, and so the adapters can
# be exercised against a stub without a key.
BASE_URLS = {
    "fal": "https://queue.fal.run",
    "replicate": "https://api.replicate.com/v1",
}

# The same class under a local name, so `except providers.ProviderError` and
# anything raised inside `net` are one thing rather than two that have to be
# kept in sync. It stays a RuntimeError, which is what the CLI catches.
ProviderError = net.HttpError


@dataclass
class Clip:
    path: Path
    model: str
    provider: str


def _httpx():
    try:
        import httpx
    except ImportError as exc:
        raise ProviderError("the video path needs httpx: pip install 'mascotify[byok]'") from exc
    return httpx


def require_key(provider: str, key: str | None = None) -> str:
    if key:
        return key
    env = ENV_KEYS[provider]
    found = os.environ.get(env)
    if not found:
        raise ProviderError(
            f"{provider} needs {env} in the environment.\n"
            f"The video tier is the only part of mascotify that needs a key — "
            f"sprite-grid generation runs through your coding agent for free.\n"
            f"For a longer no-key animation instead, try a bigger grid:\n"
            f"  mascotify plan --rows 4 --cols 6 --fps 24 --action <motion>"
        )
    return found


def generate(
    provider: str,
    *,
    image: Path,
    prompt: str,
    duration: int,
    model: str | None,
    out: Path,
    key: str | None = None,
    timeout: float = 600.0,
    poll: float = 3.0,
) -> Clip:
    """Queue a clip, wait for it, and stream the result to `out`."""
    if provider not in DEFAULT_MODELS:
        raise ProviderError(f"unknown provider {provider!r}; known: {', '.join(DEFAULT_MODELS)}")
    api_key = require_key(provider, key)
    model = model or DEFAULT_MODELS[provider]
    redact = Redactor.of(api_key)
    httpx = _httpx()

    with httpx.Client(timeout=timeout) as c:
        fetch = _fal if provider == "fal" else _replicate
        url = fetch(c, api_key, model, image, prompt, duration, timeout, poll, redact)
        net.download_to(
            c,
            url,
            out,
            provider=provider,
            redact=redact,
            timeout=timeout,
            accept=net.VIDEO_TYPES,
        )

    return Clip(path=out, model=model, provider=provider)


def _data_uri(image: Path) -> str:
    mime = mimetypes.guess_type(image.name)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(image.read_bytes()).decode()


def _fal(c, key, model, image, prompt, duration, timeout, poll, redact) -> str:
    hdr = net.headers({"Authorization": f"Key {key}", "Content-Type": "application/json"})
    payload = {"image_url": _data_uri(image), "prompt": prompt, "duration": str(duration)}

    job = net.json_of(
        # submit, not send: this POST starts a clip that bills on arrival, so a
        # 502 here is ambiguous rather than transient. See net.submit.
        net.submit(
            lambda: c.post(f"{BASE_URLS['fal']}/{model}", headers=hdr, json=payload),
            provider="fal",
            model=model,
            redact=redact,
        ),
        "fal",
        redact,
    )
    status_url, result_url = job.get("status_url"), job.get("response_url")
    if not status_url or not result_url:
        raise ProviderError(f"fal returned no queue urls: {redact(str(job))[:300]}")

    net.poll(
        lambda: c.get(status_url, headers=hdr),
        done=lambda s: s.get("status") == "COMPLETED",
        failed=lambda s: str(s) if s.get("status") in {"FAILED", "ERROR"} else None,
        provider="fal",
        model=model,
        redact=redact,
        timeout=timeout,
        interval=poll,
    )

    data = net.json_of(
        net.send(
            lambda: c.get(result_url, headers=hdr),
            provider="fal",
            model=model,
            redact=redact,
        ),
        "fal",
        redact,
    )
    video = data.get("video") or {}
    url = video.get("url") if isinstance(video, dict) else None
    if not url:
        raise ProviderError(f"fal completed with no video url: {redact(str(data))[:300]}")
    return url


def _replicate(c, key, model, image, prompt, duration, timeout, poll, redact) -> str:
    hdr = net.headers({"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    payload = {"input": {"start_image": _data_uri(image), "prompt": prompt, "duration": duration}}

    pred = net.json_of(
        # submit, not send — same billing hazard as fal above.
        net.submit(
            lambda: c.post(
                f"{BASE_URLS['replicate']}/models/{model}/predictions",
                headers=hdr,
                json=payload,
            ),
            provider="replicate",
            model=model,
            redact=redact,
        ),
        "replicate",
        redact,
    )

    # The submit response can already be terminal, so check before polling.
    if pred.get("status") not in {"succeeded", "failed", "canceled"}:
        follow = (pred.get("urls") or {}).get("get")
        if not follow:
            raise ProviderError(f"replicate returned no polling url: {redact(str(pred))[:300]}")
        pred = net.poll(
            lambda: c.get(follow, headers=hdr),
            done=lambda s: s.get("status") == "succeeded",
            failed=lambda s: (
                f"{s.get('status')}: {s.get('error')}"
                if s.get("status") in {"failed", "canceled"}
                else None
            ),
            provider="replicate",
            model=model,
            redact=redact,
            timeout=timeout,
            interval=poll,
        )

    if pred.get("status") in {"failed", "canceled"}:
        raise ProviderError(f"replicate {pred.get('status')}: {redact(str(pred.get('error')))}")

    out = pred.get("output")
    url = out[0] if isinstance(out, list) and out else out
    if not isinstance(url, str):
        raise ProviderError(f"replicate returned no video url: {redact(str(out))[:300]}")
    return url
