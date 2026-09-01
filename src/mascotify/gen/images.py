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
from collections.abc import Callable
from dataclasses import dataclass

from .. import net
from ..net import Redactor

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


# One class under two names rather than a translation layer. The web app catches
# `images.ProviderError`, and anything raised from `net` has to be caught by
# that same clause or an ordinary provider failure becomes a 500. Aliasing makes
# that true by construction; a translate-on-the-way-out wrapper only stays true
# until someone adds a code path that forgets to wrap.
ProviderError = net.HttpError


@dataclass
class Generated:
    png: bytes
    provider: str
    model: str


def _httpx():
    try:
        import httpx
    except ImportError as exc:
        raise ProviderError("image providers need httpx: pip install 'mascotify[web]'") from exc
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
    # Everything an adapter puts into an error passes through this first: a 400
    # body can echo the request, and a key in a traceback outlives the session.
    redact = Redactor.of(api_key)
    png = fn(api_key, model, prompt, size, reference, timeout, redact)
    if not png:
        raise ProviderError(f"{provider} returned an empty image")
    return Generated(png=png, provider=provider, model=model)


def _data_uri(png: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(png).decode()


# --------------------------------------------------------------------- OpenAI


def _openai(key, model, prompt, size, reference, timeout, redact) -> bytes:
    """`/v1/images/generations`, or `/v1/images/edits` when there is a reference.

    The request is built inside the retry callback rather than prepared once:
    the edits call is multipart, and a multipart body is consumed on send, so a
    resent request would arrive empty.
    """
    httpx = _httpx()
    auth = net.headers({"Authorization": f"Bearer {key}"})

    with httpx.Client(timeout=timeout) as c:

        def call():
            if reference is None:
                return c.post(
                    f"{BASE_URLS['openai']}/images/generations",
                    headers={**auth, "Content-Type": "application/json"},
                    json={"model": model, "prompt": prompt, "size": size, "n": 1},
                )
            return c.post(
                f"{BASE_URLS['openai']}/images/edits",
                headers=auth,
                data={"model": model, "prompt": prompt, "size": size, "n": "1"},
                files={"image": ("reference.png", reference, "image/png")},
            )

        resp = net.send(call, provider="openai", model=model, redact=redact)
        payload = net.json_of(resp, "openai", redact)

        try:
            item = payload["data"][0]
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"openai returned no image: {redact(str(payload))[:300]}") from exc

        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            return net.download(c, item["url"], provider="openai", redact=redact, timeout=timeout)

    raise ProviderError("openai returned neither b64_json nor a url")


# --------------------------------------------------------------------- Gemini


def _gemini(key, model, prompt, size, reference, timeout, redact) -> bytes:
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
        resp = net.send(
            lambda: c.post(
                f"{BASE_URLS['gemini']}/models/{model}:generateContent",
                headers=net.headers({"x-goog-api-key": key, "Content-Type": "application/json"}),
                json={
                    "contents": [{"role": "user", "parts": parts}],
                    "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
                },
            ),
            provider="gemini",
            model=model,
            redact=redact,
        )
        payload = net.json_of(resp, "gemini", redact)

    for cand in payload.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            blob = part.get("inlineData") or part.get("inline_data")
            if blob and blob.get("data"):
                return base64.b64decode(blob["data"])

    # A refusal comes back 200 with prose and no image, so it has to be read out
    # of the body rather than off the status line.
    text = " ".join(
        p.get("text", "")
        for cand in payload.get("candidates", [])
        for p in cand.get("content", {}).get("parts", [])
    ).strip()
    raise ProviderError(f"gemini returned no image{': ' + redact(text)[:300] if text else ''}")


# ------------------------------------------------------------------ fal.ai


def _fal(key, model, prompt, size, reference, timeout, redact, poll: float = 2.0) -> bytes:
    """Queue submit then poll. One key reaches many models, which is why it is
    the recommended provider for an open-source tool.

    Every poll goes through `net.send`, so a 502 partway through a queued job
    costs a retry rather than the whole generation — which the caller has
    already been billed for by the time polling starts.
    """
    httpx = _httpx()
    hdr = net.headers({"Authorization": f"Key {key}", "Content-Type": "application/json"})
    payload: dict = {"prompt": prompt, "num_images": 1}
    if reference is not None:
        payload["image_urls"] = [_data_uri(reference)]

    with httpx.Client(timeout=timeout) as c:
        job = net.json_of(
            net.send(
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
        images = data.get("images") or []
        url = images[0].get("url") if images else None
        if not url:
            raise ProviderError(f"fal completed with no image: {redact(str(data))[:300]}")
        if url.startswith("data:"):
            return base64.b64decode(url.split(",", 1)[1])
        return net.download(c, url, provider="fal", redact=redact, timeout=timeout)


# --------------------------------------------------------------- Replicate


def _replicate(key, model, prompt, size, reference, timeout, redact, poll: float = 2.0) -> bytes:
    httpx = _httpx()
    hdr = net.headers({"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    inp: dict = {"prompt": prompt}
    if reference is not None:
        inp["image_input"] = [_data_uri(reference)]

    with httpx.Client(timeout=timeout) as c:
        pred = net.json_of(
            net.send(
                lambda: c.post(
                    f"{BASE_URLS['replicate']}/models/{model}/predictions",
                    headers=hdr,
                    json={"input": inp},
                ),
                provider="replicate",
                model=model,
                redact=redact,
            ),
            "replicate",
            redact,
        )

        # The submit response may already be terminal, so check before polling.
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
            raise ProviderError(f"replicate returned no image url: {redact(str(out))[:300]}")
        return net.download(c, url, provider="replicate", redact=redact, timeout=timeout)
