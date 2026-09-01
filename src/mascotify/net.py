"""HTTP behaviour shared by every provider adapter.

Generation is slow, queued, and billed before it succeeds. That combination is
what makes naive request code expensive: a single transient 502 five minutes
into a poll loses work the user has already paid for, and a poll response that
comes back as an HTML error page raises `JSONDecodeError` from somewhere deep
in the adapter rather than saying what happened.

So retries, status checks and redaction live here instead of being written
four times, once per provider, slightly differently.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import __version__

USER_AGENT = f"mascotify/{__version__} (+https://github.com/dige04/mascotify)"

# Worth another attempt: the request never landed, or the service asked us to
# come back. Everything else — 400, 401, 403, 404, 422 — is a request that will
# fail identically no matter how many times it is sent.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

MAX_ATTEMPTS = 4
BASE_DELAY = 1.0
MAX_DELAY = 20.0
# A generated image is a few MB. Anything past this is an error page, a
# redirect loop, or a provider having a very bad day.
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


class HttpError(RuntimeError):
    """Raised with a message safe to show a user and safe to put in a log."""


@dataclass(frozen=True)
class Redactor:
    """Removes secrets from anything on its way into an error message.

    Providers echo request context in error bodies often enough that this
    matters: without it, a 400 from a misconfigured call can put the user's key
    into a terminal, a log file, or a bug report.
    """

    secrets: tuple[str, ...] = ()

    def __call__(self, text: str) -> str:
        out = text
        for s in self.secrets:
            if s and len(s) >= 8:
                out = out.replace(s, "***")
        return out

    @classmethod
    def of(cls, *secrets: str | None) -> Redactor:
        return cls(tuple(s for s in secrets if s))


def backoff_delay(attempt: int, retry_after: str | None = None) -> float:
    """Seconds to wait before attempt N, honouring Retry-After when sent.

    Jittered so that several adapters retrying together do not resynchronise
    into a thundering herd against the same queue.
    """
    if retry_after:
        try:
            return max(0.0, min(float(retry_after), MAX_DELAY))
        except ValueError:
            pass  # Retry-After may be an HTTP date; fall through to backoff
    return min(MAX_DELAY, BASE_DELAY * (2**attempt)) * (0.5 + random.random() * 0.5)


def check(resp: Any, provider: str, model: str, redact: Redactor) -> None:
    """Turn an HTTP failure into a message that names the cause.

    A bare status number sends people to the wrong fix: 401 means the key, 404
    almost always means a model id that has moved, and 429 means wait rather
    than debug.
    """
    code = resp.status_code
    if code < 400:
        return
    if code in (401, 403):
        raise HttpError(f"{provider} rejected the key ({code}). Check it and try again.")
    if code == 404:
        raise HttpError(
            f"{provider} does not recognise model {model!r}. Model catalogues move — "
            f"pick a current id in settings, or pass --model."
        )
    if code == 429:
        raise HttpError(f"{provider} rate-limited the request. Wait a moment and retry.")
    body = redact((resp.text or "")[:300]).strip()
    raise HttpError(f"{provider} error {code}{': ' + body if body else ''}")


def send(
    call: Callable[[], Any],
    *,
    provider: str,
    model: str,
    redact: Redactor,
    attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], None] | None = None,
) -> Any:
    """Run a request, retrying only what is worth retrying.

    `call` is re-invoked per attempt rather than a prepared request being
    resent, because multipart bodies are consumed on send and would arrive
    empty the second time.
    """
    import httpx

    sleep = sleep or time.sleep
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = call()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            if attempt == attempts - 1:
                break
            sleep(backoff_delay(attempt))
            continue

        if resp.status_code in RETRY_STATUS and attempt < attempts - 1:
            sleep(backoff_delay(attempt, resp.headers.get("Retry-After")))
            continue

        check(resp, provider, model, redact)
        return resp

    raise HttpError(
        f"{provider} unreachable after {attempts} attempts: {redact(str(last))}"
    ) from last


def json_of(resp: Any, provider: str, redact: Redactor) -> dict:
    """Decode a JSON body, saying so when it is not JSON.

    A queue that answers a poll with an HTML error page would otherwise raise
    JSONDecodeError from inside the adapter, which reads like a mascotify bug.
    """
    try:
        return resp.json()
    except ValueError as exc:
        snippet = redact((resp.text or "")[:200]).strip()
        raise HttpError(
            f"{provider} returned {resp.status_code} with a non-JSON body: {snippet!r}"
        ) from exc


def poll(
    fetch: Callable[[], Any],
    *,
    done: Callable[[dict], bool],
    failed: Callable[[dict], str | None],
    provider: str,
    model: str,
    redact: Redactor,
    timeout: float,
    interval: float = 2.0,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
) -> dict:
    """Poll a queued job until it finishes, fails, or the deadline passes.

    Every poll goes through `send`, so a transient error mid-queue costs a
    retry rather than the whole generation.
    """
    sleep = sleep or time.sleep
    now = now or time.monotonic
    deadline = now() + timeout
    while now() < deadline:
        state = json_of(
            send(fetch, provider=provider, model=model, redact=redact, sleep=sleep),
            provider,
            redact,
        )
        if reason := failed(state):
            raise HttpError(f"{provider} generation failed: {redact(reason)}")
        if done(state):
            return state
        sleep(interval)
    raise HttpError(f"{provider} did not finish within {timeout:.0f}s")


def download(
    client: Any,
    url: str,
    *,
    provider: str,
    redact: Redactor,
    timeout: float,
    max_bytes: int | None = None,
) -> bytes:
    """Fetch a result asset, refusing anything that is not one.

    Without the status check an error page gets written out as a `.png`, and
    the failure surfaces much later as an unreadable image.
    """
    # Read the cap at call time. As a default argument it would freeze at import
    # and quietly ignore any later override — the same binding trap that made
    # patching `sleep` a no-op.
    max_bytes = MAX_DOWNLOAD_BYTES if max_bytes is None else max_bytes

    with client.stream("GET", url, timeout=timeout, follow_redirects=True) as r:
        if r.status_code >= 400:
            raise HttpError(f"{provider} result URL returned {r.status_code}")

        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype and not (ctype.startswith("image/") or ctype == "application/octet-stream"):
            raise HttpError(f"{provider} result URL served {ctype!r} rather than an image")

        chunks, total = [], 0
        for chunk in r.iter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise HttpError(
                    f"{provider} result exceeded {max_bytes // 1024 // 1024}MB; refusing it"
                )
            chunks.append(chunk)

    if not total:
        raise HttpError(f"{provider} result URL returned an empty body")
    return b"".join(chunks)


def headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Standard headers. Identifying the client helps providers diagnose."""
    return {"User-Agent": USER_AGENT, **(extra or {})}
