"""Provider adapter tests against a local stub.

These prove the half that is ours: that each adapter sends a well-formed request
and finds the image in the response shape each API documents. They cannot prove
the shapes themselves are current — only a live account does that, and the
README says so plainly rather than implying otherwise.

Worth having anyway, because response parsing is where adapter bugs live: a
wrong key name (`images` vs `video`, `inlineData` vs `inline_data`) fails at
runtime with a key error and no useful message.
"""

from __future__ import annotations

import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from PIL import Image

pytest.importorskip("httpx")

from mascotify import net
from mascotify.gen import images


def a_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), (200, 40, 60, 255)).save(buf, format="PNG")
    return buf.getvalue()


PNG = a_png()
B64 = base64.b64encode(PNG).decode()


class Stub(BaseHTTPRequestHandler):
    """Answers each provider's documented success shape."""

    seen: ClassVar[list[tuple[str, str, dict]]] = []

    def log_message(self, *_):  # keep pytest output clean
        pass

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if (self.headers.get("Content-Type") or "").startswith("application/json"):
            try:
                return json.loads(raw)
            except ValueError:
                return {}
        return {"_raw_len": len(raw)}  # multipart: only its presence matters here

    def _send(self, payload, content_type="application/json"):
        blob = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        Stub.seen.append(("GET", self.path, {}))
        if self.path.startswith("/blob"):
            return self._send(PNG, "image/png")
        if self.path.startswith("/fal-status"):
            return self._send({"status": "COMPLETED"})
        if self.path.startswith("/fal-result"):
            return self._send({"images": [{"url": self._base() + "/blob.png"}]})
        if self.path.startswith("/rep-get"):
            return self._send({"status": "succeeded", "output": [self._base() + "/blob.png"]})
        self.send_error(404)

    def do_POST(self):
        body = self._body()
        Stub.seen.append(("POST", self.path, body))

        if self.path.endswith("/images/generations") or self.path.endswith("/images/edits"):
            return self._send({"data": [{"b64_json": B64}]})

        if ":generateContent" in self.path:
            return self._send(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "here you go"},
                                    {"inlineData": {"mimeType": "image/png", "data": B64}},
                                ]
                            }
                        }
                    ]
                }
            )

        if self.path.startswith("/models/") and self.path.endswith("/predictions"):
            return self._send(
                {
                    "status": "succeeded",
                    "output": [self._base() + "/blob.png"],
                    "urls": {"get": self._base() + "/rep-get"},
                }
            )

        # Anything else is a fal queue submit.
        return self._send(
            {
                "status_url": self._base() + "/fal-status",
                "response_url": self._base() + "/fal-result",
            }
        )

    def _base(self) -> str:
        return f"http://{self.headers['Host']}"


@pytest.fixture
def stub(monkeypatch):
    Stub.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    # serve_forever polls at 0.5s by default and shutdown() waits for the next
    # tick, which is half a second of pure teardown on every test in the file.
    threading.Thread(target=lambda: srv.serve_forever(poll_interval=0.01), daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(images, "BASE_URLS", {p: base for p in images.PROVIDERS}, raising=True)
    # Retries and queue polling both sleep. net resolves time.sleep at call
    # time precisely so this patch lands; binding it as a default would freeze
    # the real one at import and make every retry test wait for real.
    monkeypatch.setattr(net.time, "sleep", lambda *_: None)
    yield base
    srv.shutdown()


@pytest.mark.parametrize("provider", images.PROVIDERS)
def test_every_adapter_extracts_a_png(stub, provider):
    out = images.generate(provider, prompt="a robot", key="test-key", model="m/1")
    assert out.png.startswith(b"\x89PNG"), f"{provider} did not return decodable PNG bytes"
    assert Image.open(io.BytesIO(out.png)).size == (8, 8)
    assert out.provider == provider
    assert out.model == "m/1"


@pytest.mark.parametrize("provider", images.PROVIDERS)
def test_every_adapter_accepts_a_reference(stub, provider):
    out = images.generate(
        provider, prompt="a grid", key="k", model="m/1", reference=PNG, size="2048x1536"
    )
    assert out.png.startswith(b"\x89PNG")


def test_openai_switches_to_edits_when_given_a_reference(stub):
    images.generate("openai", prompt="p", key="k", reference=PNG)
    paths = [p for _, p, _ in Stub.seen]
    assert any(p.endswith("/images/edits") for p in paths), paths
    images.generate("openai", prompt="p", key="k")
    assert any(p.endswith("/images/generations") for _, p, _ in Stub.seen)


def test_gemini_sends_the_reference_inline_and_asks_for_image_output(stub):
    images.generate("gemini", prompt="p", key="k", model="m", reference=PNG)
    body = next(b for m, p, b in Stub.seen if m == "POST" and ":generateContent" in p)
    assert body["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
    parts = body["contents"][0]["parts"]
    assert parts[0]["text"]
    assert parts[1]["inlineData"]["mimeType"] == "image/png"
    assert base64.b64decode(parts[1]["inlineData"]["data"]) == PNG


def test_gemini_reports_a_text_refusal_rather_than_a_key_error(stub, monkeypatch):
    """A refusal comes back 200 with prose and no image."""
    monkeypatch.setattr(
        Stub,
        "do_POST",
        lambda self: self._send(
            {"candidates": [{"content": {"parts": [{"text": "I cannot draw that"}]}}]}
        ),
    )
    with pytest.raises(images.ProviderError, match="cannot draw that"):
        images.generate("gemini", prompt="p", key="k")


def test_fal_polls_then_follows_the_result_url(stub):
    images.generate("fal", prompt="p", key="k", model="m/1")
    paths = [p for _, p, _ in Stub.seen]
    assert any("fal-status" in p for p in paths), paths
    assert any("fal-result" in p for p in paths), paths
    assert any("blob" in p for p in paths), "the image URL must actually be fetched"


def test_fal_sends_the_reference_as_a_data_uri(stub):
    images.generate("fal", prompt="p", key="k", model="m/1", reference=PNG)
    body = next(b for m, p, b in Stub.seen if m == "POST" and b.get("image_urls"))
    assert body["image_urls"][0].startswith("data:image/png;base64,")


@pytest.mark.parametrize(
    "code,expect",
    [(401, "rejected the key"), (404, "does not recognise model"), (429, "rate-limited")],
)
def test_http_failures_name_their_cause(stub, monkeypatch, code, expect):
    """A bare status number sends people to the wrong fix."""
    monkeypatch.setattr(Stub, "do_POST", lambda self: self.send_error(code))
    with pytest.raises(images.ProviderError, match=expect):
        images.generate("openai", prompt="p", key="k", model="bad/model")


def test_unknown_provider_lists_the_known_ones():
    with pytest.raises(images.ProviderError, match="openai"):
        images.generate("nope", prompt="p", key="k")


def test_missing_key_points_at_the_keyless_path(monkeypatch):
    for env in images.ENV_KEYS.values():
        monkeypatch.delenv(env, raising=False)
    with pytest.raises(images.ProviderError, match="coding agent"):
        images.generate("fal", prompt="p")


# ─── robustness ────────────────────────────────────────────────────────────
# Generation is queued and billed before it succeeds, so a transient failure
# mid-poll destroys work the caller has already paid for. These pin the
# behaviour that makes that survivable.


def test_a_transient_failure_is_retried_rather_than_losing_the_job(stub, monkeypatch):
    calls = {"n": 0}
    real = Stub.do_POST

    def flaky(self):
        calls["n"] += 1
        if calls["n"] < 3:
            return self.send_error(502)
        return real(self)

    monkeypatch.setattr(Stub, "do_POST", flaky)
    out = images.generate("openai", prompt="p", key="k", model="m")
    assert out.png.startswith(b"\x89PNG")
    assert calls["n"] == 3, "should have retried twice before succeeding"


def test_a_request_error_is_not_retried(stub, monkeypatch):
    """400 means the request is wrong; sending it again wastes the user's time."""
    calls = {"n": 0}

    def bad(self):
        calls["n"] += 1
        self.send_error(400)

    monkeypatch.setattr(Stub, "do_POST", bad)
    with pytest.raises(images.ProviderError):
        images.generate("openai", prompt="p", key="k", model="m")
    assert calls["n"] == 1, "a 400 must not be retried"


def test_giving_up_still_names_the_provider(stub, monkeypatch):
    monkeypatch.setattr(Stub, "do_POST", lambda self: self.send_error(503))
    with pytest.raises(images.ProviderError, match="openai"):
        images.generate("openai", prompt="p", key="k", model="m")


def test_the_key_never_reaches_an_error_message(stub, monkeypatch):
    """Providers echo request context in error bodies; a key in a traceback
    outlives the session that produced it."""
    secret = "sk-live-abcdef0123456789"

    def echo(self):
        blob = json.dumps({"error": f"bad request for key {secret}"}).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    monkeypatch.setattr(Stub, "do_POST", echo)
    with pytest.raises(images.ProviderError) as err:
        images.generate("openai", prompt="p", key=secret, model="m")
    assert secret not in str(err.value)
    assert "***" in str(err.value)


def test_a_non_json_poll_body_says_so(stub, monkeypatch):
    """An HTML error page would otherwise surface as JSONDecodeError from deep
    inside the adapter, which reads like a mascotify bug."""

    def html(self):
        blob = b"<html><body>502 Bad Gateway</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    monkeypatch.setattr(Stub, "do_POST", html)
    with pytest.raises(images.ProviderError, match="non-JSON"):
        images.generate("openai", prompt="p", key="k", model="m")


def test_a_result_url_serving_html_is_refused(stub, monkeypatch):
    """Without the check an error page gets written out as a .png and the
    failure surfaces much later as an unreadable image."""

    original = Stub.do_GET  # captured before patching, or the fallback recurses

    def not_an_image(self):
        if self.path.startswith("/blob"):
            blob = b"<html>gone</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            return self.wfile.write(blob)
        return original(self)

    monkeypatch.setattr(Stub, "do_GET", not_an_image)
    with pytest.raises(images.ProviderError, match="rather than image/"):
        images.generate("fal", prompt="p", key="k", model="m/1")


def test_an_oversized_result_is_refused(stub, monkeypatch):
    monkeypatch.setattr(net, "MAX_DOWNLOAD_BYTES", 16)
    with pytest.raises(images.ProviderError, match="exceeded"):
        images.generate("fal", prompt="p", key="k", model="m/1")


def test_retry_after_is_honoured_over_backoff():
    assert net.backoff_delay(0, retry_after="7") == 7.0
    assert net.backoff_delay(0, retry_after="not-a-number") <= net.MAX_DELAY
    assert 0 < net.backoff_delay(3) <= net.MAX_DELAY


def test_redactor_ignores_values_too_short_to_be_secrets():
    """Redacting a 3-character string would scrub unrelated words out of an
    error and make it unreadable."""
    r = net.Redactor.of("abc", "sk-longenoughsecret")
    assert r("abc and sk-longenoughsecret") == "abc and ***"


def test_every_request_identifies_the_client(stub):
    images.generate("gemini", prompt="p", key="k", model="m")
    assert net.USER_AGENT.startswith("mascotify/")
