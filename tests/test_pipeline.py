"""Tests built around the failures the spike actually produced.

Fixtures are synthesised rather than checked in as PNGs so the suite stays fast
and the defect being tested is visible in the test itself.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from mascotify.imaging import grid, normalize as nz
from mascotify.imaging.cutout import alpha_bbox, chroma_key, sample_key
from mascotify.imaging.pack import pack
from mascotify.spec import CutoutSpec, JobSpec, SheetSpec

KEY = (3, 248, 8)  # what a generator actually produced when asked for #00FF00


def make_sheet(
    rows=3, cols=4, cell=100, margin=14, blob=44, clip_bottom=False, jitter=0
) -> Image.Image:
    """A fake sprite sheet: one ellipse per cell on a flat key colour."""
    h = rows * cell if not clip_bottom else rows * cell - (cell - blob) // 2 - 2
    img = Image.new("RGB", (cols * cell, h), KEY)
    d = ImageDraw.Draw(img)
    for r in range(rows):
        for c in range(cols):
            size = blob + (jitter if (r * cols + c) % 2 else 0)
            cx, cy = c * cell + cell // 2, r * cell + cell // 2
            d.ellipse(
                [cx - size // 2, cy - size // 2, cx + size // 2, cy + size // 2],
                fill=(200, 40, 60),
            )
    return img


def test_sample_key_finds_the_real_backdrop():
    # The whole reason auto-key is the default: asking for #00FF00 does not
    # mean you get it, and keying on the request leaves a fringe.
    assert sample_key(make_sheet(), "corners") == KEY


def test_chroma_key_keeps_the_character():
    cut = chroma_key(make_sheet(rows=1, cols=1), CutoutSpec())
    a = np.asarray(cut)[..., 3]
    assert a[50, 50] == 255, "centre of the blob should be opaque"
    assert a[2, 2] == 0, "corner should be fully transparent"


def test_chroma_key_survives_a_green_character():
    """A teal mascot has a strong green channel; distance alone punches holes."""
    img = Image.new("RGB", (100, 100), KEY)
    ImageDraw.Draw(img).ellipse([25, 25, 75, 75], fill=(60, 160, 140))
    a = np.asarray(chroma_key(img, CutoutSpec()))[..., 3]
    assert a[50, 50] == 255
    assert a[2, 2] == 0


def test_detect_finds_every_frame():
    rep = grid.detect(chroma_key(make_sheet(), CutoutSpec()), 3, 4)
    assert (rep.found, rep.rows, rep.cols) == (12, 3, 4)
    assert rep.ok
    assert rep.problems() == []


def test_detect_flags_a_clipped_bottom_row():
    """The exact defect the first real generation produced."""
    rep = grid.detect(chroma_key(make_sheet(clip_bottom=True), CutoutSpec()), 3, 4)
    assert not rep.ok
    assert rep.clipped, "bottom row touches the canvas edge"
    assert any("margin" in p for p in rep.problems())


def test_detect_flags_inconsistent_scale():
    rep = grid.detect(chroma_key(make_sheet(blob=40, jitter=24), CutoutSpec()), 3, 4)
    assert rep.scale_spread > 0.15
    assert any("varies" in p for p in rep.problems())


def test_detect_tolerates_unequal_row_heights():
    """Rows came out 481/477/459px in the real spike; equal division would cut."""
    img = Image.new("RGB", (400, 300), KEY)
    d = ImageDraw.Draw(img)
    for r, (top, size) in enumerate([(10, 60), (110, 50), (215, 55)]):
        for c in range(4):
            cx = c * 100 + 50
            d.ellipse([cx - size // 2, top, cx + size // 2, top + size], fill=(200, 40, 60))
    rep = grid.detect(chroma_key(img, CutoutSpec()), 3, 4)
    assert rep.found == 12
    assert rep.rows == 3


def test_normalize_puts_frames_on_one_canvas():
    rep = grid.detect(chroma_key(make_sheet(jitter=10), CutoutSpec()), 3, 4)
    frames = nz.normalize(grid.slice_frames(chroma_key(make_sheet(jitter=10), CutoutSpec()), rep))
    assert len({f.size for f in frames}) == 1, "every frame shares a canvas"


def test_normalize_aligns_baselines():
    a = Image.new("RGBA", (60, 80), (0, 0, 0, 0))
    ImageDraw.Draw(a).rectangle([10, 10, 50, 70], fill=(255, 0, 0, 255))
    b = Image.new("RGBA", (60, 80), (0, 0, 0, 0))
    ImageDraw.Draw(b).rectangle([10, 30, 50, 70], fill=(255, 0, 0, 255))

    out = nz.normalize([a, b], align="baseline")
    bottoms = {alpha_bbox(f)[3] for f in out}
    assert len(bottoms) == 1, "feet land on one line regardless of height"


def test_seam_score_needs_a_common_canvas():
    with pytest.raises(ValueError, match="common canvas"):
        nz.seam_score([Image.new("RGBA", (10, 10)), Image.new("RGBA", (20, 20))])


def test_seam_score_sees_colour_only_motion():
    """A blink changes no silhouette; an alpha-only score would read 0."""
    frames = []
    for i in range(4):
        f = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        ImageDraw.Draw(f).rectangle([5, 5, 35, 35], fill=(10 + i * 60, 20, 30, 255))
        frames.append(f)
    assert nz.seam_score(frames) > 0


def test_ping_pong_is_skipped_when_the_loop_already_closes():
    frames = []
    for i in list(range(4)) + list(range(2, 0, -1)):
        f = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        ImageDraw.Draw(f).rectangle([5, 5 + i, 35, 35], fill=(200, 40, 60, 255))
        frames.append(f)
    assert nz.apply_loop(frames, ping_pong=None) == frames or not nz.should_ping_pong(frames)


def test_pack_grid_is_uniform():
    frames = [Image.new("RGBA", (30, 40), (255, 0, 0, 255)) for _ in range(12)]
    atlas = pack(frames, fps=12)
    assert atlas.count == 12
    assert atlas.image.size == (atlas.cols * 30, atlas.rows * 40)
    assert atlas.frame_rect(0) == (0, 0, 30, 40)
    assert atlas.frame_rect(atlas.cols) == (0, 40, 30, 40)


def test_sheet_spec_rejects_impossible_grids():
    with pytest.raises(ValueError):
        SheetSpec(rows=0, cols=4).validate()
    with pytest.raises(ValueError):
        SheetSpec(rows=9, cols=9).validate()


def test_job_spec_round_trips(tmp_path):
    spec = JobSpec(sheet=SheetSpec(rows=4, cols=6, fps=24))
    spec.write(tmp_path / "job.json")
    assert JobSpec.load(tmp_path / "job.json").sheet.frames == 24


def test_alpha_bbox_ignores_matte_noise():
    img = Image.new("RGBA", (50, 50), (0, 0, 0, 0))
    img.putpixel((1, 1), (255, 0, 0, 4))  # fringe left over from keying
    ImageDraw.Draw(img).rectangle([20, 20, 30, 30], fill=(255, 0, 0, 255))
    assert alpha_bbox(img) == (20, 20, 31, 31)


# --------------------------------------------------------------- video tier
# The paid provider call cannot be exercised in tests, but everything after it
# can: a locally-encoded clip runs the same extract -> cutout -> normalize ->
# pack -> export path the real one does.

import shutil
import subprocess

from mascotify.export.targets import export_all
from mascotify.imaging.cutout import cut_out
from mascotify.imaging.pack import MAX_TEXTURE_EDGE

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not on PATH")


@pytest.fixture
def clip(tmp_path):
    """A short clip of a blob moving on a flat key, encoded with ffmpeg."""
    src = tmp_path / "src"
    src.mkdir()
    for i in range(12):
        f = Image.new("RGB", (256, 256), KEY)
        ImageDraw.Draw(f).ellipse([100, 60 + i * 4, 156, 116 + i * 4], fill=(200, 40, 60))
        f.save(src / f"{i:03d}.png")
    out = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-framerate", "12",
         "-i", str(src / "%03d.png"), "-pix_fmt", "yuv420p", str(out)],
        check=True,
    )
    return out


@needs_ffmpeg
def test_extract_frames_respects_the_cap(clip, tmp_path):
    from mascotify.video.runner import extract_frames

    frames = extract_frames(clip, tmp_path / "raw", fps=12, max_frames=5)
    assert len(frames) == 5, "max_frames bounds the keying and export cost"


@needs_ffmpeg
def test_video_path_runs_end_to_end_after_the_paid_call(clip, tmp_path):
    from mascotify.video.runner import extract_frames

    paths = extract_frames(clip, tmp_path / "raw", fps=12, max_frames=12)
    cut = [cut_out(Image.open(p), CutoutSpec()) for p in paths]
    frames = nz.normalize(cut, align="baseline")
    atlas = pack(frames, fps=12)
    bundles = export_all(atlas, frames, tmp_path / "export", "clip", base_pt=120)

    assert {b.target for b in bundles} == {"web", "ios", "android", "unity", "godot", "lottie"}
    for b in bundles:
        assert b.files, f"{b.target} wrote nothing"
        for f in b.files:
            assert f.exists() and f.stat().st_size > 0


@needs_ffmpeg
def test_video_cutout_preflight_rejects_matte_without_the_extra():
    from mascotify.video.runner import _check_cutout

    try:
        import rembg  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="matting extra"):
            _check_cutout(CutoutSpec(method="matte"))
    _check_cutout(CutoutSpec(method="chroma"))  # never raises


def test_pack_stays_under_the_texture_cap():
    """120 video frames at sprite-path cell sizes would otherwise blow past 4096."""
    frames = [Image.new("RGBA", (391, 452), (255, 0, 0, 255)) for _ in range(120)]
    atlas = pack(frames, fps=24)
    assert max(atlas.image.size) <= MAX_TEXTURE_EDGE
    assert atlas.count == 120
    assert atlas.image.size == (atlas.cols * atlas.cell_w, atlas.rows * atlas.cell_h)


# ------------------------------------------------------------- identity compare
# There is no automated identity gate, on purpose. These tests pin the two
# properties that make the manual check trustworthy: everything is size-matched,
# and nothing returns a pass/fail.

from mascotify.imaging import compare as cmp_mod


def _character(h=200, body=(240, 230, 200), accent=(60, 150, 170), antenna=0.07):
    """A crude mascot, every dimension proportional to `h`.

    Proportional matters: with pixel-fixed features, the same call at two sizes
    produces two different designs, and a scale-invariance test would be
    measuring the fixture rather than the code.
    """
    w, pad = round(h * 0.6), round(h * 0.1)
    ant = round(h * antenna)
    img = Image.new("RGBA", (w + 2 * pad, h + ant + 2 * pad), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = (w + 2 * pad) // 2
    d.line([cx, pad, cx, pad + ant], fill=(40, 40, 50, 255), width=max(1, round(h * 0.025)))
    d.ellipse([pad, pad + ant, pad + w, pad + ant + h], fill=(*body, 255))
    belly = round(h * 0.125)
    d.ellipse(
        [cx - belly, pad + ant + h // 2, cx + belly, pad + ant + h // 2 + 2 * belly],
        fill=(*accent, 255),
    )
    return img


def test_compare_scales_everything_to_one_height():
    """The scale mismatch is what made real drift unreadable; it must be gone."""
    anchor = _character(h=400)                    # a large anchor
    frames = [_character(h=120) for _ in range(4)]  # small sheet frames
    strip = cmp_mod.build(anchor, frames).strip
    assert strip.height == cmp_mod.STRIP_HEIGHT + cmp_mod.LABEL_BAND
    assert strip.width > cmp_mod.STRIP_HEIGHT, "anchor plus four frames laid out in a row"


def test_compare_reports_context_never_a_verdict():
    result = cmp_mod.build(_character(), [_character() for _ in range(3)])
    assert not hasattr(result, "ok"), "a pass/fail would encode a judgement the metrics cannot make"
    assert any("not a verdict" in n for n in result.notes())


def test_palette_distance_ignores_pure_scale():
    """Drift must not be confused with resolution, which is what confounded the probe."""
    big, small = _character(h=400), _character(h=100)
    fitted = [cmp_mod._fit(im, 200) for im in (big, small)]
    assert cmp_mod.palette_distance(*fitted) < 0.05


def test_palette_distance_sees_a_recoloured_character():
    a = cmp_mod._fit(_character(accent=(60, 150, 170)), 200)
    b = cmp_mod._fit(_character(accent=(200, 90, 40)), 200)
    assert cmp_mod.palette_distance(a, b) > 0.05


def test_compare_needs_something_to_compare_against():
    with pytest.raises(ValueError, match="nothing to compare"):
        cmp_mod.build(_character(), [])


def test_content_bbox_ignores_a_stray_speck():
    """One leftover pixel used to stretch the box and silently break scale-matching."""
    from mascotify.imaging.cutout import content_bbox

    img = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([140, 60, 260, 340], fill=(240, 230, 200, 255))
    clean = content_bbox(img)
    img.putpixel((3, 3), (255, 0, 0, 255))
    assert content_bbox(img) == clean
    assert alpha_bbox(img) != clean, "alpha_bbox is still the everything-visible box"


def test_content_bbox_keeps_a_deliberate_detached_part():
    """Largest-region-only would drop a floating hat or a held object."""
    from mascotify.imaging.cutout import content_bbox

    img = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([140, 60, 260, 340], fill=(240, 230, 200, 255))
    d.ellipse([170, 15, 230, 45], fill=(200, 60, 60, 255))  # detached, but substantial
    assert content_bbox(img)[1] < 60, "the hat is inside the box"


def test_compare_scale_matching_survives_a_speck():
    """The regression that matters: a mismatched strip that still looks matched."""
    def char(speck):
        im = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
        ImageDraw.Draw(im).ellipse([140, 60, 260, 340], fill=(240, 230, 200, 255))
        if speck:
            im.putpixel((3, 3), (255, 0, 0, 255))
        return im

    assert cmp_mod._fit(char(False), 420).width == cmp_mod._fit(char(True), 420).width


def test_detect_survives_a_gutter_narrower_than_the_old_threshold():
    """The agent end-to-end run: 1086px tall, 15px row gutters, read as 1x4.

    The old threshold was 2% of image height (21px here), so it swallowed real
    gutters on any sheet laid out tightly. Sizing the split off the expected row
    count instead makes it scale-free.
    """
    h, w, gap, cell_h = 1086, 1448, 15, 322
    img = Image.new("RGB", (w, h), KEY)
    d = ImageDraw.Draw(img)
    for r in range(3):
        top = 44 + r * (cell_h + gap)
        for c in range(4):
            cx = 180 + c * 362
            d.ellipse([cx - 90, top, cx + 90, top + cell_h], fill=(200, 40, 60))

    rep = grid.detect(chroma_key(img, CutoutSpec()), 3, 4)
    assert (rep.found, rep.rows, rep.cols) == (12, 3, 4)
    assert rep.ok, rep.problems()


def test_bands_are_trimmed_at_both_ends():
    """A final band running to the image edge faked a 60% baseline drift."""
    img = Image.new("RGB", (400, 400), KEY)  # 100px of empty margin at the bottom
    d = ImageDraw.Draw(img)
    for r in range(3):
        for c in range(4):
            cx, cy = c * 100 + 50, r * 100 + 50
            d.ellipse([cx - 22, cy - 22, cx + 22, cy + 22], fill=(200, 40, 60))

    rep = grid.detect(chroma_key(img, CutoutSpec()), 3, 4)
    last = [c for c in rep.cells if c.row == 2][0]
    assert last.band[3] == last.bbox[3], "band hugs content, not the canvas edge"
    assert rep.baseline_spread < 0.05, "aligned feet must not read as drift"


def test_baseline_spread_catches_real_within_row_drift():
    img = Image.new("RGB", (400, 300), KEY)
    d = ImageDraw.Draw(img)
    for r in range(3):
        for c in range(4):
            cx = c * 100 + 50
            drop = 20 if (r == 1 and c == 2) else 0  # one character sags
            top = r * 100 + 28 + drop
            d.ellipse([cx - 22, top, cx + 22, top + 44], fill=(200, 40, 60))

    rep = grid.detect(chroma_key(img, CutoutSpec()), 3, 4)
    assert rep.baseline_spread > 0.10
    assert any("drift" in p for p in rep.problems())


def test_detect_handles_gaps_that_tie_with_the_gutter_width():
    """An evenly laid out sheet makes every gap the same width.

    Deriving a threshold and re-splitting cuts at *every* gap of that width, so
    one internal gap matching the gutter turns 6 columns into 7 and produces a
    repair prompt for a grid that was fine. Measured at six 40px gaps.
    """
    img = Image.new("RGB", (600, 200), KEY)
    d = ImageDraw.Draw(img)
    for c in range(6):
        x0 = c * 100 + 20
        if c == 2:  # one character drawn as two lobes, internal gap == gutter
            d.rectangle([x0, 60, x0 + 9, 140], fill=(200, 40, 60))
            d.rectangle([x0 + 50, 60, x0 + 59, 140], fill=(200, 40, 60))
        else:
            d.rectangle([x0, 60, x0 + 59, 140], fill=(200, 40, 60))

    rep = grid.detect(chroma_key(img, CutoutSpec()), 1, 6)
    assert (rep.found, rep.cols) == (6, 6), rep.problems()


def test_detect_prefers_gutters_on_the_grid_over_internal_gaps():
    """Tie-break: a gap inside a character sits mid-cell, a gutter sits on a boundary."""
    img = Image.new("RGB", (400, 200), KEY)
    d = ImageDraw.Draw(img)
    for c in range(4):
        x0 = c * 100 + 15
        if c == 1:
            d.rectangle([x0, 60, x0 + 14, 140], fill=(200, 40, 60))
            d.rectangle([x0 + 55, 60, x0 + 69, 140], fill=(200, 40, 60))
        else:
            d.rectangle([x0, 60, x0 + 69, 140], fill=(200, 40, 60))

    rep = grid.detect(chroma_key(img, CutoutSpec()), 1, 4)
    assert rep.found == 4
    widths = [c.width for c in rep.cells]
    assert max(widths) - min(widths) < 40, f"cells should be comparable, got {widths}"


def test_animation_defaults_to_lossy_and_is_much_smaller(tmp_path):
    """A 46-frame loop was 2.55MB lossless vs 0.66MB at quality 90.

    Lossless is the wrong default for a generated raster mascot headed for a
    web page — it preserves detail the source never had.
    """
    from mascotify.imaging.pack import write_animation

    # Shaded, not flat: a flat fill is the one case lossless wins, and a
    # generated mascot is never flat.
    rng = np.random.default_rng(7)
    frames = []
    for i in range(12):
        noise = rng.integers(0, 255, (200, 200, 3), dtype=np.uint8)
        f = Image.fromarray(noise, "RGB").convert("RGBA")
        mask = Image.new("L", (200, 200), 0)
        ImageDraw.Draw(mask).ellipse([20 + i, 20, 180, 180 - i], fill=255)
        f.putalpha(mask)
        frames.append(f)

    lossy = write_animation(frames, tmp_path / "a.webp", fps=12)
    big = write_animation(frames, tmp_path / "b.webp", fps=12, lossless=True)
    assert lossy.stat().st_size < big.stat().st_size

    out = Image.open(lossy)
    assert out.n_frames == 12
    assert out.mode in {"RGBA", "RGBX", "P"}, "alpha must survive the lossy path"
