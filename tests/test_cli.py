"""End-to-end tests for the command surface.

There were none, and the gap had teeth: an edit meant for `pose-ingest`
matched the same lines in `cmd_ingest` and left the main verb raising
AttributeError on every successful run. The suite stayed green because
nothing here called a command.

These run the real `main()` with real files, so the assertion is the exit
code and what landed on disk — not a mock of either.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from mascotify.cli import main

KEY = (3, 248, 8)


def sheet(path, rows=3, cols=4, cell=120, blob=48, clip=False):
    h = rows * cell - ((cell - blob) // 2 + 2 if clip else 0)
    img = Image.new("RGB", (cols * cell, h), KEY)
    d = ImageDraw.Draw(img)
    for r in range(rows):
        for c in range(cols):
            cx, cy = c * cell + cell // 2, r * cell + cell // 2
            d.ellipse(
                [cx - blob // 2, cy - blob // 2, cx + blob // 2, cy + blob // 2],
                fill=(200, 40, 60),
            )
    img.save(path)
    return path


def pose_pair(tmp_path, drop_last=False):
    d, r = tmp_path / "d.png", tmp_path / "r.png"
    sheet(d, rows=3, cols=3)
    sheet(r, rows=3, cols=3)
    if drop_last:
        im = Image.open(d)
        ImageDraw.Draw(im).rectangle([240, 240, 359, 359], fill=KEY)
        im.save(d)
    return d, r


# ─── the vocabularies ──────────────────────────────────────────────────────


@pytest.mark.parametrize("argv", [["motions"], ["motions", "--json"], ["poses"], ["poses", "--json"]])
def test_the_listing_commands_run(argv, capsys):
    assert main(argv) == 0


def test_anchor_emits_a_prompt(capsys):
    assert main(["anchor", "a rounded robot", "--prompt-only"]) == 0
    assert "flat solid" in capsys.readouterr().out


# ─── the motion path ───────────────────────────────────────────────────────


def test_ingest_exports_a_good_sheet(tmp_path):
    """The regression: this raised AttributeError after validation passed."""
    s = sheet(tmp_path / "sheet.png")
    assert main(["ingest", str(s), "--root", str(tmp_path), "--name", "Wave"]) == 0
    out = tmp_path / ".mascotify" / "wave" / "export"
    assert (out / "web").is_dir() and (out / "ios").is_dir()


def test_ingest_narrowed_to_one_target(tmp_path):
    s = sheet(tmp_path / "sheet.png")
    assert main(["ingest", str(s), "--root", str(tmp_path), "--targets", "godot"]) == 0
    out = tmp_path / ".mascotify" / "mascot" / "export"
    assert (out / "godot").is_dir()
    assert not (out / "ios").exists()


def test_ingest_rejects_a_clipped_sheet_with_a_repair_prompt(tmp_path, capsys):
    s = sheet(tmp_path / "bad.png", clip=True)
    assert main(["ingest", str(s), "--root", str(tmp_path)]) == 2
    assert "Regenerate the sheet fixing ONLY those problems" in capsys.readouterr().out


def test_ingest_force_exports_a_sheet_that_failed(tmp_path):
    s = sheet(tmp_path / "bad.png", clip=True)
    assert main(["ingest", str(s), "--root", str(tmp_path), "--force"]) == 0


def test_ingest_reports_a_missing_file(tmp_path):
    assert main(["ingest", str(tmp_path / "nope.png"), "--root", str(tmp_path)]) == 1


# ─── the pose path ─────────────────────────────────────────────────────────


def test_pose_ingest_exports_a_good_pair(tmp_path):
    d, r = pose_pair(tmp_path)
    code = main(["pose-ingest", "--root", str(tmp_path), "--job", "fox",
                 "--directions", str(d), "--reactions", str(r)])
    assert code == 0
    out = tmp_path / ".mascotify" / "poses" / "fox" / "export" / "page-mascot"
    assert (out / "fox-directions.webp").exists()
    assert (out / "fox-reactions.webp").exists()


def test_pose_ingest_force_cannot_ship_a_short_grid(tmp_path, capsys):
    """--force loosens the placement budgets, never the cell contract."""
    d, r = pose_pair(tmp_path, drop_last=True)
    code = main(["pose-ingest", "--root", str(tmp_path), "--job", "fox", "--force",
                 "--directions", str(d), "--reactions", str(r)])
    assert code == 2
    assert "cannot bypass a wrong cell count" in capsys.readouterr().err


def test_pose_plan_emits_both_grids(tmp_path, capsys):
    ref = sheet(tmp_path / "ref.png", rows=1, cols=1)
    code = main(["pose-plan", "--root", str(tmp_path), "--ref", str(ref),
                 "--job", "fox", "--prompt-only"])
    assert code == 0
    out = capsys.readouterr().out
    assert "--- directions ---" in out and "--- reactions ---" in out
    assert "cell 5 (middle-centre)" in out


def test_pose_ingest_reports_a_missing_sheet(tmp_path):
    d, _ = pose_pair(tmp_path)
    code = main(["pose-ingest", "--root", str(tmp_path),
                 "--directions", str(d), "--reactions", str(tmp_path / "nope.png")])
    assert code == 1
