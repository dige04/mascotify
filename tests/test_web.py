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
from fastapi.testclient import TestClient

from mascotify.gen import images
from mascotify.web.app import create_app

KEY = (3, 248, 8)


def sheet_png(rows=3, cols=4, cell=100, blob=44, clip=False) -> bytes:
    h = rows * cell if not clip else rows * cell - (cell - blob) // 2 - 2
    img = Image.new("RGB", (cols * cell, h), KEY)
    d = ImageDraw.Draw(img)
    for r in range(rows):
        for c in range(cols):
            cx, cy = c * cell + cell // 2, r * cell + cell // 2
            d.ellipse(
                [cx - blob // 2, cy - blob // 2, cx + blob // 2, cy + blob // 2], fill=(200, 40, 60)
            )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path):
    # TestClient sends Host: testserver by default, which the origin guard
    # rejects — correctly. Point it at loopback so the suite exercises the same
    # path a real browser takes rather than bypassing the guard.
    return TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765")


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


def test_anchor_upload_rejects_an_oversized_body(client, monkeypatch):
    monkeypatch.setattr("mascotify.web.app.MAX_UPLOAD_BYTES", 3)
    r = client.post("/api/anchor/upload", files={"file": ("x.png", b"four", "image/png")})
    assert r.status_code == 413


def test_settings_reject_invalid_types_and_providers(client):
    assert client.post("/api/settings", json={"provider": "unknown"}).status_code == 400
    assert client.post("/api/settings", json={"model": 42}).status_code == 400


def test_animate_rejects_non_numeric_geometry(client):
    client.post(
        "/api/anchor/upload",
        files={"file": ("ref.png", sheet_png(rows=1, cols=1), "image/png")},
    )
    r = client.post("/api/animate", json={"rows": "many"})
    assert r.status_code == 400


def test_ingest_normalizes_a_free_form_action_to_a_safe_job(client, tmp_path):
    r = client.post(
        "/api/ingest?action=../../outside&rows=3&cols=4&fps=12",
        files={"file": ("sheet.png", sheet_png(), "image/png")},
    )
    assert r.status_code == 200
    assert r.json()["job"] == "outside"
    assert (tmp_path / ".mascotify" / "outside" / "job.json").exists()
    assert not (tmp_path.parent / "outside").exists()


def test_ingest_exports_every_target(client):
    r = client.post(
        "/api/ingest?action=wave&rows=3&cols=4&fps=12",
        files={"file": ("sheet.png", sheet_png(), "image/png")},
    )
    body = r.json()
    assert body["ok"] is True, body
    assert body["frames"] >= 12
    assert {e["target"] for e in body["exports"]} == {
        "web",
        "ios",
        "android",
        "unity",
        "godot",
        "lottie",
    }
    assert all(e["snippet"] for e in body["exports"]), "each bundle ships usable code"


def test_reingest_replaces_stale_export_files(client, tmp_path):
    first = client.post(
        "/api/ingest?action=wave&rows=3&cols=4&fps=12",
        files={"file": ("sheet.png", sheet_png(), "image/png")},
    ).json()
    second = client.post(
        "/api/ingest?action=wave&rows=1&cols=2&fps=12",
        files={"file": ("sheet.png", sheet_png(rows=1, cols=2), "image/png")},
    ).json()
    assert first["ok"] is True and second["ok"] is True
    imagesets = list(
        (tmp_path / ".mascotify" / "wave" / "export" / "ios" / "Mascot.xcassets").glob("*.imageset")
    )
    assert len(imagesets) == second["frames"]


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
    """Only for the keyed providers — the default one deliberately needs none."""
    for env in images.ENV_KEYS.values():
        if env:
            monkeypatch.delenv(env, raising=False)
    client.post("/api/settings", json={"provider": "fal"})
    r = client.post("/api/anchor", json={"character": "a robot"})
    assert r.status_code == 400
    assert "key" in r.json()["detail"].lower()


def test_the_default_provider_needs_no_key(client):
    """A first run must not dead-end behind a settings dialog."""
    c = client.get("/api/config").json()
    assert c["provider"] not in c["needs_key"]
    assert c["cost"][c["provider"]] == 0


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


# ─── origin guard ──────────────────────────────────────────────────────────
# The server holds an API key and can spend an agent quota, and it has no
# authentication — the security model is that only this machine can reach it.


def test_a_rebound_dns_host_is_refused(client):
    """An attacker's domain resolving to 127.0.0.1 becomes same-origin, so CORS
    stops applying. The browser still sends their name in Host, which is what
    makes this catchable at all."""
    r = client.get("/api/config", headers={"Host": "evil.example.com"})
    assert r.status_code == 403
    assert "refused" in r.json()["detail"]


def test_a_cross_origin_upload_is_refused(client):
    """multipart/form-data is CORS-safelisted, so this POST needs no preflight
    and any page could otherwise overwrite the anchor."""
    r = client.post(
        "/api/anchor/upload",
        headers={"Origin": "https://evil.example.com"},
        files={"file": ("x.png", b"nope", "image/png")},
    )
    assert r.status_code == 403


def test_the_browsers_own_requests_still_work(client):
    assert client.get("/api/config").status_code == 200
    r = client.post(
        "/api/settings",
        headers={"Origin": "http://127.0.0.1:8765"},
        json={"provider": "agent"},
    )
    assert r.status_code == 200


def test_reads_without_an_origin_header_are_allowed(client):
    """curl and the browser's own navigation send no Origin."""
    assert client.get("/api/config", headers={}).status_code == 200


# ─── the masthead cast ─────────────────────────────────────────────────────


def pose_sheet(rows=3, cols=3):
    return sheet_png(rows=rows, cols=cols)


def make_pose_job(client, tmp_path, name="fox"):
    from mascotify.pose import PoseJob, process_pair
    from mascotify.spec import PoseJobSpec

    d, r = tmp_path / f"{name}-d.png", tmp_path / f"{name}-r.png"
    d.write_bytes(pose_sheet())
    r.write_bytes(pose_sheet())
    job = PoseJob(root=tmp_path, spec=PoseJobSpec(name=name, character=f"a {name}"))
    return process_pair(d, r, job)


def test_the_cast_is_empty_before_any_pose_job(client):
    assert client.get("/api/cast").json() == []


def test_the_cast_lists_exported_pose_jobs(client, tmp_path):
    assert make_pose_job(client, tmp_path).ok
    body = client.get("/api/cast").json()
    assert [c["id"] for c in body] == ["fox"]
    assert body[0]["alt"] == "a fox", "the description becomes the alt text"


def test_a_pose_job_with_no_export_is_not_in_the_cast(client, tmp_path):
    """A job that failed validation has a directory but nothing to show."""
    from mascotify.pose import PoseJob
    from mascotify.spec import PoseJobSpec

    PoseJob(root=tmp_path, spec=PoseJobSpec(name="half")).prepare()
    assert client.get("/api/cast").json() == []


def test_cast_sheets_are_served(client, tmp_path):
    make_pose_job(client, tmp_path)
    for which in ("directions", "reactions"):
        r = client.get(f"/api/cast/fox/{which}")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/webp"
        assert r.content[:4] == b"RIFF"


def test_cast_rejects_a_sheet_that_is_not_one_of_the_two(client, tmp_path):
    make_pose_job(client, tmp_path)
    assert client.get("/api/cast/fox/secrets").status_code == 404


@pytest.mark.parametrize("name", ["..", "%2e%2e"])
def test_a_crafted_cast_name_cannot_escape_the_workspace(client, name):
    assert client.get(f"/api/cast/{name}/directions").status_code in (400, 404, 405)
