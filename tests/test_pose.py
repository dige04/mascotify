"""Tests for the pose-pair path that feeds the page-mascot component.

The fixtures model the two things that go wrong in a pose set and nowhere else:
a head that turns (which widens the bbox on one side, and used to slide the body
the other way), and two sheets that disagree with each other.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from mascotify.gen.prompts import directions_prompt, pose_repair_prompt, reactions_prompt
from mascotify.imaging import normalize as nz
from mascotify.imaging.cutout import chroma_key
from mascotify.pose import PoseJob, process_pair
from mascotify.spec import DIRECTIONS, REACTIONS, CutoutSpec, PoseJobSpec, PoseSpec

KEY = (3, 248, 8)

# Where the head sits relative to the body, per cell, in the compass order the
# directions grid is read in. This is what makes the bbox asymmetric.
HEAD_DX = (-14, 0, 14) * 3
HEAD_DY = (-6, -6, -6, 0, 0, 0, 6, 6, 6)


def make_pose_sheet(
    *,
    cell: int = 140,
    body: int = 32,
    head: int = 24,
    head_dx: tuple[int, ...] = HEAD_DX,
    body_drift: int = 0,
    body_shift: int = 0,
    scale: float = 1.0,
) -> Image.Image:
    """A 3x3 grid: a planted body with a head that moves between cells.

    `body_drift` slides the *body* sideways cell by cell, which is the failure
    the anchor check exists to catch. `body_shift` moves it the same amount in
    every cell, which is invisible within one sheet and only shows up against
    the other one. `scale` draws the whole character larger, which is the
    failure the scale match exists to catch.
    """
    img = Image.new("RGB", (3 * cell, 3 * cell), KEY)
    d = ImageDraw.Draw(img)
    bw, hw = round(body * scale), round(head * scale)
    for i in range(9):
        r, c = divmod(i, 3)
        cx = c * cell + cell // 2 + body_drift * (i - 4) + body_shift
        base = r * cell + cell - 24
        d.ellipse([cx - bw // 2, base - bw, cx + bw // 2, base], fill=(200, 40, 60))
        hx, hy = cx + head_dx[i], base - bw - hw // 2 + HEAD_DY[i]
        d.ellipse([hx - hw // 2, hy - hw // 2, hx + hw // 2, hy + hw // 2], fill=(220, 90, 70))
    return img


@pytest.fixture
def pair(tmp_path: Path):
    def _write(**kw) -> tuple[Path, Path]:
        d, r = tmp_path / "directions.png", tmp_path / "reactions.png"
        make_pose_sheet().save(d)
        # Reactions move the head far less; expressions are a face change.
        make_pose_sheet(head_dx=(0,) * 9, **kw).save(r)
        return d, r

    return _write


def job_for(tmp_path: Path) -> PoseJob:
    return PoseJob(root=tmp_path, spec=PoseJobSpec(name="fox", character="a chibi fox"))


# ------------------------------------------------------------------ vocabulary


def test_the_grid_is_nine_cells_each_way():
    assert len(DIRECTIONS) == 9
    assert len(REACTIONS) == 9


def test_centre_cell_is_the_resting_pose():
    """Index 4 is what the head settles to when the cursor is close."""
    assert DIRECTIONS[4][0] == "center"


def test_pose_spec_refuses_a_grid_the_component_cannot_read():
    with pytest.raises(ValueError, match="3x3"):
        PoseSpec(rows=4, cols=4).validate()


# --------------------------------------------------------------------- prompts


def test_directions_prompt_enumerates_every_cell_by_position():
    """No validator can check cell order, so the prompt has to carry it."""
    text = directions_prompt(PoseJobSpec())
    for i, (_, desc) in enumerate(DIRECTIONS, start=1):
        assert f"cell {i} " in text
        assert desc in text
    assert "top-left" in text and "bottom-right" in text


def test_reactions_prompt_names_every_expression():
    text = reactions_prompt(PoseJobSpec())
    for desc in REACTIONS.values():
        assert desc in text


def test_pose_prompts_pin_the_body_still():
    text = directions_prompt(PoseJobSpec())
    assert "does not move, shift sideways" in text
    assert "IDENTICAL scale" in text
    assert "IDENTICAL vertical baseline" in " ".join(text.split())


def test_repair_prompt_repeats_the_invariants():
    text = pose_repair_prompt(["directions: 8 cells"], PoseJobSpec(), "directions")
    assert "directions: 8 cells" in text
    assert "3x3" in text
    assert "same pose in the same cell" in text


# ---------------------------------------------------------------- body anchor


def test_a_turned_head_does_not_slide_the_body():
    """The defect the foot anchor exists to fix.

    Centring each frame on its own bbox moves the body away from the side the
    head turned towards — a mascot that leans away from the cursor following it.
    """
    cut = chroma_key(make_pose_sheet(), CutoutSpec())
    from mascotify.imaging import grid

    frames = grid.slice_frames(cut, grid.detect(cut, 3, 3))
    seated = nz.normalize(frames, align="baseline")

    bbox_centred = [nz.foot_anchor_x(f) for f in seated]
    assert max(bbox_centred) - min(bbox_centred) > 8, "fixture should reproduce the slide"

    anchored = nz.anchor_horizontally(seated)
    after = [nz.foot_anchor_x(f) for f in anchored]
    assert max(after) - min(after) <= 1.0


def test_foot_anchor_ignores_what_the_head_does():
    body = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    d = ImageDraw.Draw(body)
    d.ellipse([40, 60, 60, 95], fill=(255, 0, 0, 255))  # planted body
    left, right = body.copy(), body.copy()
    ImageDraw.Draw(left).ellipse([20, 30, 45, 55], fill=(255, 0, 0, 255))
    ImageDraw.Draw(right).ellipse([55, 30, 80, 55], fill=(255, 0, 0, 255))
    assert abs(nz.foot_anchor_x(left) - nz.foot_anchor_x(right)) < 1.0


# ------------------------------------------------------------------- the pair


def test_a_clean_pair_exports_both_sheets(tmp_path, pair):
    d, r = pair()
    result = process_pair(d, r, job_for(tmp_path))
    assert result.ok, result.report.problems()
    assert len(result.directions) == 9 and len(result.reactions) == 9

    files = {p.name for p in result.bundle.files}
    assert "fox-directions.webp" in files
    assert "fox-reactions.webp" in files
    assert "page-mascot" in result.bundle.install_hint


def test_both_sheets_land_on_one_canvas(tmp_path, pair):
    """Two separate normalize calls would give two canvases and a click-jump."""
    d, r = pair()
    result = process_pair(d, r, job_for(tmp_path))
    sizes = {f.size for f in result.directions + result.reactions}
    assert len(sizes) == 1
    w, h = sizes.pop()
    assert w == h, "the component renders into a square box"


def test_the_two_sheets_share_a_baseline(tmp_path, pair):
    from mascotify.imaging.cutout import alpha_bbox

    d, r = pair()
    result = process_pair(d, r, job_for(tmp_path))
    bottoms = [alpha_bbox(f)[3] for f in result.directions + result.reactions]
    assert max(bottoms) - min(bottoms) <= 2


def test_a_drifting_body_is_rejected(tmp_path):
    """A body that wanders cell to cell, as opposed to a head that turns.

    Two pixels per cell is enough: the anchor is measured against the nominal
    cell pitch, so this is the generator moving the character, not the head
    widening its bounding box.
    """
    d, r = tmp_path / "d.png", tmp_path / "r.png"
    make_pose_sheet(body_drift=2).save(d)
    make_pose_sheet(head_dx=(0,) * 9).save(r)
    result = process_pair(d, r, job_for(tmp_path))
    assert not result.ok
    assert any(p.startswith("directions: the body shifts sideways") for p in result.report.problems())
    assert result.report.within["reactions"] <= 0.06, "only the directions sheet drifts"


def test_a_mismatched_scale_between_sheets_is_rejected(tmp_path, pair):
    d, r = pair(scale=1.4)
    result = process_pair(d, r, job_for(tmp_path))
    assert not result.ok
    assert any("height of the" in p for p in result.report.problems())


def test_a_short_grid_is_rejected_before_slicing(tmp_path, pair):
    """Eight poses where nine were asked for: every cell after the gap is the
    wrong pose, which measures perfectly and tracks the cursor wrong."""
    d, r = pair()
    sheet = Image.open(d)
    ImageDraw.Draw(sheet).rectangle([280, 280, 419, 419], fill=KEY)  # erase cell 9
    sheet.save(d)
    result = process_pair(d, r, job_for(tmp_path))
    assert not result.ok
    assert any("expected 9" in p for p in result.report.problems())
    assert result.bundle is None


def test_a_rejected_pair_still_leaves_a_report(tmp_path, pair):
    d, r = pair(scale=1.4)
    job = job_for(tmp_path)
    process_pair(d, r, job)
    assert (job.dir / "report.json").exists()


def test_the_sheets_are_lossless(tmp_path, pair):
    """Lossy edges bleed a sliver of the neighbouring cell into view.

    Checked by round-tripping pixels rather than by reading a flag: Pillow does
    not report `lossless` back in `Image.info`, so asserting on it passes
    whatever the encoder actually did.
    """
    import numpy as np

    d, r = pair()
    result = process_pair(d, r, job_for(tmp_path))
    sheet = next(p for p in result.bundle.files if p.name.endswith("-directions.webp"))
    with Image.open(sheet) as im:
        assert im.width == im.height, "3x3 of square cells stays square"
        assert im.width % 3 == 0
        loaded = np.asarray(im.convert("RGBA"))

    from mascotify.imaging.pack import pack

    expected = np.asarray(pack(result.directions, fps=1, cols=3).image.convert("RGBA"))
    assert np.array_equal(loaded, expected), "the sheet was re-encoded lossily"


def test_the_job_round_trips(tmp_path, pair):
    d, r = pair()
    job = job_for(tmp_path)
    process_pair(d, r, job)
    reopened = PoseJob.open(tmp_path, "fox")
    assert reopened.spec.character == "a chibi fox"
    assert reopened.spec.pose.cols == 3


def test_two_sheets_that_disagree_with_each_other_are_rejected(tmp_path):
    """Each sheet is internally consistent; they just disagree.

    Invisible until a click swaps sheets, and then the character jumps sideways
    — which is the whole reason the pair is ingested in one call.
    """
    d, r = tmp_path / "d.png", tmp_path / "r.png"
    make_pose_sheet().save(d)
    make_pose_sheet(head_dx=(0,) * 9, body_shift=12).save(r)
    result = process_pair(d, r, job_for(tmp_path))
    assert not result.ok
    assert all(v <= 0.06 for v in result.report.within.values()), "each sheet is fine alone"
    assert any("seat the body" in p for p in result.report.problems())


def test_a_repair_prompt_names_the_sheet_that_has_to_be_redrawn(tmp_path):
    d, r = tmp_path / "d.png", tmp_path / "r.png"
    make_pose_sheet(body_drift=2).save(d)
    make_pose_sheet(head_dx=(0,) * 9).save(r)
    result = process_pair(d, r, job_for(tmp_path))
    problems = result.report.problems()
    assert any(p.startswith("directions:") for p in problems)
    assert not any(p.startswith("reactions:") for p in problems)


def test_force_cannot_ship_a_grid_with_the_wrong_cell_count(tmp_path, pair):
    """`--force` loosens the placement budgets, not the cell contract.

    Nine poses are nine specific poses. Forcing an eight-cell grid through would
    shift every pose after the gap into the wrong compass direction — output
    that measures perfectly and tracks the cursor wrong.
    """
    d, r = pair()
    sheet = Image.open(d)
    ImageDraw.Draw(sheet).rectangle([280, 280, 419, 419], fill=KEY)
    sheet.save(d)

    result = process_pair(d, r, job_for(tmp_path), strict=False)
    assert result.bundle is None
    assert not result.ok


def test_force_still_ships_a_pair_whose_placement_is_loose(tmp_path):
    d, r = tmp_path / "d.png", tmp_path / "r.png"
    make_pose_sheet(body_drift=2).save(d)
    make_pose_sheet(head_dx=(0,) * 9).save(r)

    assert process_pair(d, r, job_for(tmp_path)).bundle is None
    forced = process_pair(d, r, job_for(tmp_path / "forced"), strict=False)
    assert forced.bundle is not None
    assert not forced.ok, "forcing exports, it does not make the report pass"


def test_an_oversized_pair_is_capped_and_stays_square(tmp_path):
    """The cap resizes to a square target, which is only safe after squarify."""
    from mascotify.pose import MAX_CELL

    d, r = tmp_path / "d.png", tmp_path / "r.png"
    make_pose_sheet(cell=900, body=400, head=300, head_dx=(-150, 0, 150) * 3).save(d)
    make_pose_sheet(cell=900, body=400, head=300, head_dx=(0,) * 9).save(r)

    result = process_pair(d, r, job_for(tmp_path))
    assert result.ok, result.report.problems()
    for f in result.directions + result.reactions:
        assert f.size == (MAX_CELL, MAX_CELL)

    sheet = next(p for p in result.bundle.files if p.name.endswith("-directions.webp"))
    with Image.open(sheet) as im:
        assert im.size == (3 * MAX_CELL, 3 * MAX_CELL)


# ─── job names ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "typed,lands_on",
    [("fox", "fox"), ("My_Fox", "my-fox"), ("../../outside", "outside"), ("..", "mascot")],
)
def test_a_pose_job_name_is_normalised_before_it_addresses_a_path(tmp_path, typed, lands_on):
    """The motion path sanitised at its boundary and this one did not.

    Two consequences, both real: `../../outside` addressed a directory outside
    the workspace, and a name like `My_Fox` was one the web app would list from
    disk but then refuse to serve, because listing reads directory names and
    serving validates them.
    """
    from mascotify.pose import PoseJob
    from mascotify.spec import PoseJobSpec

    job = PoseJob(root=tmp_path, spec=PoseJobSpec(name=typed))
    assert job.name == lands_on
    assert job.dir == tmp_path / ".mascotify" / "poses" / lands_on
    assert tmp_path in job.dir.parents, "never outside the workspace"


def test_a_pose_job_reopens_under_the_name_it_was_written_with(tmp_path):
    from mascotify.pose import PoseJob
    from mascotify.spec import PoseJobSpec

    PoseJob(root=tmp_path, spec=PoseJobSpec(name="My_Fox", character="a fox")).prepare()
    assert PoseJob.open(tmp_path, "My_Fox").spec.character == "a fox"
    assert PoseJob.open(tmp_path, "my-fox").spec.character == "a fox"
