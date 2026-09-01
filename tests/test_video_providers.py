"""Video provider tests against a local stub.

This is the only tier that charges per call, so the properties worth pinning are
the ones that decide whether a paid clip survives a bad minute: a transient
failure is retried rather than discarded, a queue answering with HTML says so,
and a result that is not a video is refused before it lands on disk as one.

The provider call itself still cannot be proved current without a live account —
the README says so rather than implying these tests cover it.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

pytest.importorskip("httpx")

from mascotify import net
from mascotify.video import providers

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


@pytest.fixture
def ref(tmp_path):
    p = tmp_path / "ref.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return p


class Stub(BaseHTTPRequestHandler):
    """Answers each provider's documented success shape."""

    seen: ClassVar[list[tuple[str, str]]] = []

    def log_message(self, *_):
        pass

    def _send(self, payload, content_type="application/json"):
        blob = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _base(self) -> str:
        return f"http://{self.headers['Host']}"

    def do_GET(self):
        Stub.seen.append(("GET", self.path))
        if self.path.startswith("/clip"):
            return self._send(MP4, "video/mp4")
        if self.path.startswith("/fal-status"):
            return self._send({"status": "COMPLETED"})
        if self.path.startswith("/fal-result"):
            return self._send({"video": {"url": self._base() + "/clip.mp4"}})
        if self.path.startswith("/rep-get"):
            return self._send({"status": "succeeded", "output": self._base() + "/clip.mp4"})
        self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        Stub.seen.append(("POST", self.path))

        if self.path.endswith("/predictions"):
            return self._send({"status": "processing", "urls": {"get": self._base() + "/rep-get"}})
        return self._send(
            {
                "status_url": self._base() + "/fal-status",
                "response_url": self._base() + "/fal-result",
            }
        )


@pytest.fixture
def stub(monkeypatch):
    Stub.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=lambda: srv.serve_forever(poll_interval=0.01), daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(providers, "BASE_URLS", {p: base for p in providers.DEFAULT_MODELS})
    # Retries and queue polling both sleep; net resolves time.sleep at call time
    # so this patch lands instead of the suite waiting out real backoff.
    monkeypatch.setattr(net.time, "sleep", lambda *_: None)
    yield base
    srv.shutdown()


def run(provider, out, **kw):
    return providers.generate(
        provider,
        image=kw.pop("image"),
        prompt="wave",
        duration=5,
        model="m/1",
        out=out,
        key="test-key",
        poll=0.0,
        **kw,
    )


@pytest.mark.parametrize("provider", ["fal", "replicate"])
def test_a_clip_is_queued_polled_and_written(stub, ref, tmp_path, provider):
    out = tmp_path / "clip.mp4"
    clip = run(provider, out, image=ref)
    assert clip.path == out
    assert out.read_bytes() == MP4
    assert clip.provider == provider and clip.model == "m/1"


@pytest.mark.parametrize("provider", ["fal", "replicate"])
def test_polling_actually_happens(stub, ref, tmp_path, provider):
    run(provider, tmp_path / "c.mp4", image=ref)
    paths = [p for _, p in Stub.seen]
    assert any("status" in p or "rep-get" in p for p in paths), paths
    assert any("clip" in p for p in paths), "the clip URL must actually be fetched"


def test_a_transient_failure_does_not_discard_a_paid_clip(stub, ref, tmp_path, monkeypatch):
    """The clip is billed when it is queued, so losing it to one 502 is the
    single most expensive failure in the project."""
    calls = {"n": 0}
    original = Stub.do_GET

    def flaky(self):
        if self.path.startswith("/fal-status"):
            calls["n"] += 1
            if calls["n"] < 3:
                return self.send_error(502)
        return original(self)

    monkeypatch.setattr(Stub, "do_GET", flaky)
    out = tmp_path / "c.mp4"
    run("fal", out, image=ref)
    assert out.read_bytes() == MP4
    assert calls["n"] >= 3, "should have retried the poll rather than giving up"


def test_a_request_error_is_not_retried(stub, ref, tmp_path, monkeypatch):
    calls = {"n": 0}

    def bad(self):
        calls["n"] += 1
        self.send_error(400)

    monkeypatch.setattr(Stub, "do_POST", bad)
    with pytest.raises(providers.ProviderError):
        run("fal", tmp_path / "c.mp4", image=ref)
    assert calls["n"] == 1


def test_a_rejected_model_names_the_cause(stub, ref, tmp_path, monkeypatch):
    monkeypatch.setattr(Stub, "do_POST", lambda self: self.send_error(404))
    with pytest.raises(providers.ProviderError, match="does not recognise model"):
        run("replicate", tmp_path / "c.mp4", image=ref)


def test_the_key_never_reaches_an_error_message(stub, ref, tmp_path, monkeypatch):
    secret = "fal-live-abcdef0123456789"

    def echo(self):
        blob = json.dumps({"error": f"bad request for {secret}"}).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    monkeypatch.setattr(Stub, "do_POST", echo)
    with pytest.raises(providers.ProviderError) as err:
        providers.generate(
            "fal",
            image=ref,
            prompt="p",
            duration=5,
            model="m/1",
            out=tmp_path / "c.mp4",
            key=secret,
            poll=0.0,
        )
    assert secret not in str(err.value)
    assert "***" in str(err.value)


def test_an_html_error_page_is_not_written_out_as_a_clip(stub, ref, tmp_path, monkeypatch):
    """Without the content-type check this lands on disk as a .mp4 and fails
    much later inside ffmpeg."""
    original = Stub.do_GET

    def not_a_video(self):
        if self.path.startswith("/clip"):
            blob = b"<html>gone</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            return self.wfile.write(blob)
        return original(self)

    monkeypatch.setattr(Stub, "do_GET", not_a_video)
    out = tmp_path / "c.mp4"
    with pytest.raises(providers.ProviderError, match="rather than video/"):
        run("fal", out, image=ref)
    assert not out.exists(), "a refused download must not leave a file behind"


def test_a_partial_download_leaves_no_file(stub, ref, tmp_path, monkeypatch):
    """A truncated clip that survived as a .mp4 would be mistaken for a finished
    one on a later run."""
    monkeypatch.setattr(net, "MAX_DOWNLOAD_BYTES", 8)
    out = tmp_path / "c.mp4"
    with pytest.raises(providers.ProviderError, match="exceeded"):
        run("fal", out, image=ref)
    assert not out.exists()
    assert not list(tmp_path.glob("*.part")), "the temp file must be cleaned up"


def test_a_failed_generation_reports_the_provider_reason(stub, ref, tmp_path, monkeypatch):
    original = Stub.do_GET

    def failed(self):
        if self.path.startswith("/fal-status"):
            return self._send({"status": "FAILED", "error": "content policy"})
        return original(self)

    monkeypatch.setattr(Stub, "do_GET", failed)
    with pytest.raises(providers.ProviderError, match="content policy"):
        run("fal", tmp_path / "c.mp4", image=ref)


def test_a_non_json_queue_response_says_so(stub, ref, tmp_path, monkeypatch):
    def html(self):
        blob = b"<html>502</html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    monkeypatch.setattr(Stub, "do_POST", html)
    with pytest.raises(providers.ProviderError, match="non-JSON"):
        run("fal", tmp_path / "c.mp4", image=ref)


def test_unknown_provider_lists_the_known_ones(ref, tmp_path):
    with pytest.raises(providers.ProviderError, match="fal"):
        run("nope", tmp_path / "c.mp4", image=ref)


def test_missing_key_points_at_the_free_alternative(monkeypatch):
    for env in providers.ENV_KEYS.values():
        monkeypatch.delenv(env, raising=False)
    with pytest.raises(providers.ProviderError, match="rows 4 --cols 6"):
        providers.require_key("fal")


def test_the_error_contract_the_cli_relies_on():
    """cli.py catches RuntimeError; anything raised from net must be caught by
    `except providers.ProviderError` or a paid failure becomes a traceback."""
    assert providers.ProviderError is net.HttpError
    assert issubclass(providers.ProviderError, RuntimeError)
    with pytest.raises(providers.ProviderError):
        raise net.HttpError("boom")
