"""Image generation for callers that are not a coding agent.

The CLI and the skill do not need this: there, the agent's own image tool draws
the picture and mascotify only compiles the prompt and processes the result. A
browser has no such tool, so the web UI needs a provider and therefore a key.

Two calls cover the whole pipeline: one with no reference (the anchor) and one
with a reference (the sheet, which must stay on model). Every adapter maps both
onto whatever its API calls them.
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from typing import Callable

PROVIDERS = ("openai", "gemini", "fal", "replicate")

ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "fal": "FAL_KEY",
    "replicate": "REPLICATE_API_TOKEN",
}

# Catalogues move faster than any pinned default, so every one of these is
# overridable and a rejected id produces an error that says so.
DEFAULT_MODELS = {
    "openai": "gpt-image-1.5",
    "gemini": "gemini-3.1-flash-image",
    "fal": "fal-ai/nano-banana/edit",
    "replicate": "google/nano-banana",
}

# Shown before spending, per image. Deliberately rough.
ROUGH_COST_USD = {"openai": 0.04, "gemini": 0.04, "fal": 0.04, "replicate": 0.04}

# Split out so a proxy or self-hosted gateway can be pointed at, and so the
# adapters can be exercised against a stub. Response parsing is where adapter
# bugs actually live, and it is testable without a key.
BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "fal": "https://queue.fal.run",
    "replicate": "https://api.replicate.com/v1",
}


class ProviderError(RuntimeError):
    pass


@dataclass
class Generated:
    png: bytes
    provider: str
    model: str


def _httpx():
    try:
        import httpx
    except ImportError as exc:
        raise ProviderError(
            "image providers need httpx: pip install 'mascotify[web]'"
        ) from exc
    return httpx


def resolve_key(provider: str, key: str | None = None) -> str:
    if key:
        return key
    env = ENV_KEYS.get(provider)
    found = os.environ.get(env, "") if env else ""
    if not found:
        raise ProviderError(
            f"{provider} needs a key — set {env}, or paste one into the web UI's "
            f"settings. The CLI does not need this: there your coding agent draws "
            f"the image and no key is involved."
        )
    return found


def generate(
    provider: str,
    *,
    prompt: str,
    key: str | None = None,
    model: str | None = None,
    size: str = "1024x1024",
    reference: bytes | None = None,
    timeout: float = 300.0,
) -> Generated:
    """One image. `reference` keeps the character on model for a sheet."""
    if provider not in PROVIDERS:
        raise ProviderError(f"unknown provider {provider!r}; known: {', '.join(PROVIDERS)}")
    api_key = resolve_key(provider, key)
    model = model or DEFAULT_MODELS[provider]

    fn: Callable[..., bytes] = {
        "openai": _openai,
        "gemini": _gemini,
        "fal": _fal,
        "replicate": _replicate,
    }[provider]
    png = fn(api_key, model, prompt, size, reference, timeout)
    return Generated(png=png, provider=provider, model=model)


def _data_uri(png: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(png).decode()


# --------------------------------------------------------------------- OpenAI


def _openai(key, model, prompt, size, reference, timeout) -> bytes:
    """`/v1/images/generations`, or `/v1/images/edits` when there is a reference.

    Asks for a transparent background where the model supports it; the chroma
    key still runs afterwards, and handles the case where it comes back opaque.
    """
    httpx = _httpx()
    headers = {"Authorization": f"Bearer {key}"}

    with httpx.Client(timeout=timeout) as c:
        if reference is None:
            r = c.post(
                f"{BASE_URLS['openai']}/images/generations",
                headers={**headers, "Content-Type": "application/json"},
                json={"model": model, "prompt": prompt, "size": size, "n": 1},
            )
        else:
            r = c.post(
                f"{BASE_URLS['openai']}/images/edits",
                headers=headers,
                data={"model": model, "prompt": prompt, "size": size, "n": "1"},
                files={"image": ("reference.png", reference, "image/png")},
            )
        _raise_for(r, "openai", model)
        payload = r.json()

    try:
        item = payload["data"][0]
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"openai returned no image: {payload}") from exc

    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            return c.get(item["url"]).content
    raise ProviderError(f"openai returned neither b64_json nor url: {item}")


# --------------------------------------------------------------------- Gemini


def _gemini(key, model, prompt, size, reference, timeout) -> bytes:
    """`generateContent` with `responseModalities` including IMAGE.

    Size is not a parameter here — the model decides. That is fine: nothing
    downstream assumes a resolution, because generators ignore the request
    anyway.
    """
    httpx = _httpx()
    parts: list[dict] = [{"text": prompt}]
    if reference is not None:
        parts.append(
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(reference).decode()}}
        )

    with httpx.Client(timeout=timeout) as c:
        r = c.post(
            f"{BASE_URLS['gemini']}/models/{model}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
            },
        )
        _raise_for(r, "gemini", model)
        payload = r.json()

    for cand in payload.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            blob = part.get("inlineData") or part.get("inline_data")
            if blob and blob.get("data"):
                return base64.b64decode(blob["data"])

    # A refusal comes back as text rather than an error status.
    text = " ".join(
        p.get("text", "")
        for cand in payload.get("candidates", [])
        for p in cand.get("content", {}).get("parts", [])
    ).strip()
    raise ProviderError(f"gemini returned no image{': ' + text if text else ''}")


# ------------------------------------------------------------------ fal.ai


def _fal(key, model, prompt, size, reference, timeout, poll: float = 2.0) -> bytes:
    """Queue submit then poll. One key reaches many models, which is why it is
    the recommended provider for an open-source tool."""
    httpx = _httpx()
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    payload: dict = {"prompt": prompt, "num_images": 1}
    if reference is not None:
        payload["image_urls"] = [_data_uri(reference)]

    with httpx.Client(timeout=timeout) as c:
        r = c.post(f"{BASE_URLS['fal']}/{model}", headers=headers, json=payload)
        _raise_for(r, "fal", model)
        job = r.json()
        status_url, result_url = job.get("status_url"), job.get("response_url")
        if not status_url:
            raise ProviderError(f"fal returned no status_url: {job}")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = c.get(status_url, headers=headers).json()
            if state.get("status") == "COMPLETED":
                data = c.get(result_url, headers=headers).json()
                images = data.get("images") or []
                url = images[0].get("url") if images else None
                if not url:
                    raise ProviderError(f"fal completed with no image: {data}")
                if url.startswith("data:"):
                    return base64.b64decode(url.split(",", 1)[1])
                return c.get(url, follow_redirects=True).content
            if state.get("status") in {"FAILED", "ERROR"}:
                raise ProviderError(f"fal generation failed: {state}")
            time.sleep(poll)
    raise ProviderError(f"fal did not finish within {timeout:.0f}s")


# --------------------------------------------------------------- Replicate


def _replicate(key, model, prompt, size, reference, timeout, poll: float = 2.0) -> bytes:
    httpx = _httpx()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    inp: dict = {"prompt": prompt}
    if reference is not None:
        inp["image_input"] = [_data_uri(reference)]

    with httpx.Client(timeout=timeout) as c:
        r = c.post(
            f"{BASE_URLS['replicate']}/models/{model}/predictions",
            headers=headers,
            json={"input": inp},
        )
        _raise_for(r, "replicate", model)
        pred = r.json()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = pred.get("status")
            if status == "succeeded":
                out = pred.get("output")
                url = out[0] if isinstance(out, list) and out else out
                if not isinstance(url, str):
                    raise ProviderError(f"replicate returned no image url: {out}")
                return c.get(url, follow_redirects=True).content
            if status in {"failed", "canceled"}:
                raise ProviderError(f"replicate {status}: {pred.get('error')}")
            time.sleep(poll)
            pred = c.get(pred["urls"]["get"], headers=headers).json()
    raise ProviderError(f"replicate did not finish within {timeout:.0f}s")


def _raise_for(r, provider: str, model: str) -> None:
    if r.status_code == 401 or r.status_code == 403:
        raise ProviderError(f"{provider} rejected the key ({r.status_code}). Check it and retry.")
    if r.status_code == 404:
        raise ProviderError(
            f"{provider} does not recognise model {model!r}. Model catalogues move — "
            f"pick a current id in settings."
        )
    if r.status_code == 429:
        raise ProviderError(f"{provider} rate-limited the request. Wait and retry.")
    if r.status_code >= 400:
        raise ProviderError(f"{provider} error {r.status_code}: {r.text[:300]}")
