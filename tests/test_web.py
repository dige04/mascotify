"""Web app tests.

The provider call is the only part that needs a key, so it is the only part left
out here; test_providers.py covers it against a stub. Everything else runs for
real against the same pipeline the CLI uses — which is the property worth
protecting: if the web path ever grows its own keying or export logic, these
tests still pass while the outputs diverge.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from PIL import Image, ImageDraw

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from mascotify.gen import images  # noqa: E402
from mascotify.web.app import create_app  # noqa: E402

KEY = (3, 248, 8)


def sheet_png(rows=3, cols=4, cell=100, blob=44, clip=False) -> bytes:
    h = rows * cell if not clip else rows * cell - (cell - blob) // 2 - 2
    img = Image.new("RGB", (cols * cell, h), KEY)
    d = ImageDraw.Draw(img)
    for r in range(rows):
        for c in range(cols):
            cx, cy = c * cell + cell // 2, r * cell + cell // 2
            d.ellipse([cx - blob // 2, cy - blob // 2, cx + blob // 2, cy + blob // 2],
                      fill=(200, 40, 60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path))


def test_config_reports_what_the_ui_needs(client):
    c = client.get("/api/config").json()
    assert set(c["providers"]) == set(images.PROVIDERS)
    assert "wave" in c["motions"]
    assert c["has_anchor"] is False


def test_config_never_returns_a_key(client):
    client.post("/api/settings", json={"provider": "fal", "key": "sk-secret-value"})
    body = client.get("/api/config").text
    assert "sk-secret-value" not in body, "a key must never reach the browser"
    assert client.get("/api/config").json()["keyed"]["fal"] is True


def test_saving_settings_without_a_key_keeps_the_existing_one(client):
    client.post("/api/settings", json={"provider": "fal", "key": "sk-abc"})
    client.post("/api/settings", json={"provider": "fal", "model": "some/other-model"})
    c = client.get("/api/config").json()
    assert c["keyed"]["fal"] is True, "re-saving other settings must not wipe the key"
    assert c["model"] == "some/other-model"


def test_anchor_upload_then_keyed_readback(client):
    img = Image.new("RGB", (200, 200), KEY)
    ImageDraw.Draw(img).ellipse([60, 60, 140, 140], fill=(200, 40, 60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    r = client.post("/api/anchor/upload", files={"file": ("ref.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200
    assert client.get("/api/config").json()["has_anchor"] is True

    cut = client.get("/api/ref?cut=1")
    assert cut.status_code == 200
    alpha = Image.open(io.BytesIO(cut.content)).convert("RGBA").getchannel("A")
    assert alpha.getpixel((2, 2)) == 0, "the backdrop should come back keyed out"


def test_anchor_upload_rejects_a_non_image(client):
    r = client.post("/api/anchor/upload", files={"file": ("x.png", b"not an image", "image/png")})
    assert r.status_code == 400


def test_ingest_exports_every_target(client):
    r = client.post(
        "/api/ingest?action=wave&rows=3&cols=4&fps=12",
        files={"file": ("sheet.png", sheet_png(), "image/png")},
    )
    body = r.json()
    assert body["ok"] is True, body
    assert body["frames"] >= 12
    assert {e["target"] for e in body["exports"]} == {
        "web", "ios", "android", "unity", "godot", "lottie"
    }
    assert all(e["snippet"] for e in body["exports"]), "each bundle ships usable code"


def test_ingest_reports_problems_instead_of_erroring(client):
    """A failed sheet is an ordinary outcome the caller re-rolls, not a 500."""
    r = client.post(
        "/api/ingest?action=wave&rows=3&cols=4&fps=12",
        files={"file": ("sheet.png", sheet_png(clip=True), "image/png")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert any("margin" in p for p in body["problems"])


def test_ingest_rejects_an_impossible_grid(client):
    r = client.post(
        "/api/ingest?action=wave&rows=0&cols=4&fps=12",
        files={"file": ("sheet.png", sheet_png(), "image/png")},
    )
    assert r.status_code == 400


def test_zip_carries_the_bundles(client):
    client.post(
        "/api/ingest?action=wave&rows=3&cols=4&fps=12",
        files={"file": ("sheet.png", sheet_png(), "image/png")},
    )
    r = client.get("/api/export/wave")
    assert r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert {n.split("/")[0] for n in names} >= {"web", "ios", "godot"}


def test_zip_can_be_narrowed_to_one_target(client):
    client.post(
        "/api/ingest?action=wave&rows=3&cols=4&fps=12",
        files={"file": ("sheet.png", sheet_png(), "image/png")},
    )
    names = zipfile.ZipFile(
        io.BytesIO(client.get("/api/export/wave?targets=web").content)
    ).namelist()
    assert {n.split("/")[0] for n in names} == {"web"}


def test_job_survives_a_reload(client):
    """Reopening the page must not lose finished work."""
    client.post(
        "/api/ingest?action=wave&rows=3&cols=4&fps=12",
        files={"file": ("sheet.png", sheet_png(), "image/png")},
    )
    restored = client.get("/api/jobs/wave").json()
    assert restored["ok"] is True
    assert restored["frames"] >= 12
    assert restored["seam"] >= 0
    assert all(e["snippet"] for e in restored["exports"]), "snippets survive the round trip"


def test_generation_without_a_key_explains_itself(client, monkeypatch):
    for env in images.ENV_KEYS.values():
        monkeypatch.delenv(env, raising=False)
    r = client.post("/api/anchor", json={"character": "a robot"})
    assert r.status_code == 400
    assert "key" in r.json()["detail"].lower()


def test_animate_requires_an_anchor_first(client):
    r = client.post("/api/animate", json={"action": "wave"})
    assert r.status_code == 400
    assert "anchor" in r.json()["detail"].lower()


@pytest.mark.parametrize("name", ["..", "..%2F..%2Fprecious", "%2e%2e"])
def test_deleting_a_job_cannot_escape_the_workspace(client, tmp_path, name):
    """Two layers have to hold: the router rejects encoded separators (405),
    and the handler's own containment check catches anything that gets through."""
    victim = tmp_path / "precious"
    victim.mkdir()
    r = client.delete(f"/api/jobs/{name}")
    assert r.status_code in (400, 404, 405), r.status_code
    assert victim.exists(), "a crafted job name must not reach outside .mascotify"
    assert tmp_path.exists()


def test_index_and_assets_are_served(client):
    assert client.get("/").status_code == 200
    assert "text/css" in client.get("/app.css").headers["content-type"]
    assert client.get("/app.js").status_code == 200
