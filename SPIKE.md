# Spike: can a coding agent's built-in image tool produce a usable sprite sheet?

Run on 2026-09-01 against Codex CLI 0.152.0, built-in `image_gen`, no API key.
Artifacts in `spike/`. Two generations, 86k tokens, zero provider spend.

## Why this was the first thing built

The whole product rests on one assumption: an image model can draw the same
character twelve times in one picture. Every export format, every CLI verb and
the entire agent workflow are downstream of that. If it fails, the architecture
is per-frame generation or video, not grids.

## Result: it works, with one reproducible defect

`spike/sheet.png`, a 4x3 wave loop generated from `spike/ref.png`:

| check | measured | verdict |
| --- | --- | --- |
| frames located | 12/12, rows=3 cols=4 | pass |
| character size spread | 4.6% | pass (budget 15%) |
| baseline agreement | exact within each row (y=533/1043/1536) | pass |
| loop seam | 1.0x an average frame step | pass |
| **canvas clipping** | **frames 9-12 cut off at y=1536** | **fail** |

The bottom row is amputated at the canvas edge — legs and feet are simply gone.
After baseline normalisation those frames seat their cut edge as the "feet", so
the character pops legless for a third of the loop. Everything else about the
sheet is shippable.

## The reroll closed it

The failure was a prompt gap, not a model limit. The first sheet prompt
specified baseline, scale and camera invariants but never asked for empty
space, so the model spent every pixel: 52 + 481 + 33 + 477 + 34 + 459 = 1536,
exactly the canvas. It allocated precisely what was named.

Adding one numeric constraint — "leave at least 10% of the canvas height as
empty background below the bottom row, no character may touch any canvas edge" —
and regenerating from the same reference:

| check | before | after |
| --- | --- | --- |
| frames located | 12/12 | 12/12 |
| character size spread | 4.6% | **0.5%** |
| clipped frames | **9, 10, 11, 12** | **none** |
| loop seam | 1.0x | **0.14x** |
| `GridReport.ok` | False | **True** |

Size spread tightened by an order of magnitude and the seam came out well below
one average step, meaning the wrap from frame 12 back to frame 1 is smoother
than a typical step and needs no ping-pong. That is the whole QA loop running
end to end: measure, hand the numbers back, regenerate, pass.

## What this changes about the design

**Equal-division slicing is wrong.** The three rows came out 481, 477 and 459px
tall. Dividing the canvas into equal rectangles would have sliced through
characters. `imaging/grid.py` finds gutters — runs of pure background — and
measures the cells that fall out of that instead. This is why Motif ships a
`/api/detect-grid` endpoint.

**Never trust the requested key colour.** The prompt asked for `#00FF00`; the
generator produced `(3, 248, 8)`. Keying on the requested value leaves a green
fringe on every edge. Sampling the actual key off the image border is the
default, not an option.

**Never trust the requested canvas size.** 2048x1536 was requested; the model
emitted 1448x1086 and Codex upscaled it after the fact. Nothing downstream may
assume a resolution.

**Validation is a pipeline stage, not a nicety.** The defect here is invisible
to any check that only looks at whether generation succeeded, and obvious to one
that measures alpha bounds. A hosted product pays a human or a heuristic to
catch this; an agent-driven tool can hand the numbers back to the agent, which
already knows how to reroll.

## The consistency finding worth keeping

Character identity drifts between `ref.png` and a generated sheet. Judged
size-matched and side by side, the clearest tell is the antenna: a short stub on
the anchor, noticeably longer and thinner in every generated frame. The head is
rounder, the ear pods smaller, the belly patch larger. Recognisably the same
mascot, not the same drawing.

The twelve characters *within* a sheet, by contrast, are near-identical to each
other — same antenna, same head, frame to frame.

That asymmetry decides the product shape. One sheet is internally coherent, so
any single animation looks right. Drift only becomes visible across *separate*
generations of the same mascot. Which is exactly what an anchor step is for:
lock one approved still, then treat it as the identity source for every
subsequent sheet — and expect to re-approve rather than assume.

## End-to-end: an agent driving the whole thing, unassisted

Fixture: an empty web project (`package.json`, `public/`). Prompt: *"Make me an
animated waving robot mascot for this web app."* No further instruction, no
mention of any command. Codex CLI, `mascotify` installed from the built wheel,
the skill installed from that same wheel.

It read the skill, ran `anchor` with a far richer character description than the
skill's own example, ran `plan`, generated, hit a validation failure, **rerolled
rather than forcing** (`--force` appears zero times in the transcript), ingested
clean at 12/12, ran `compare`, then `doctor` to double-check, copied only the
web bundle into `public/mascot/`, and wrote a demo page with a
`prefers-reduced-motion` block it was never asked for.

The loop works. What makes the run worth keeping is that it found two real bugs
that every synthetic test had missed:

**Gutter threshold scaled off the wrong thing.** The agent's sheet was 1086px
tall with 15px row gutters. `min_gap` was 2% of image height — 21px — so both
gutters were swallowed and a valid 3x4 grid was reported as `1x4`. Every fixture
until then had used generous gutters. The fix stops guessing a size: the caller
already says how many rows to expect, so cut at the `rows - 1` widest interior
gaps, which is scale-free.

**Bands ran to the canvas edge.** Fixing the first bug exposed a second one that
had been there all along. `_bands` closed its final band at the end of the
array rather than at the last content pixel, so the bottom row's band included
the bottom margin while every other row's did not. `baseline_spread` measured
band-relative offsets, so a perfectly aligned sheet reported 60% baseline drift.
Bands are now trimmed both ends, and baseline drift is measured *within* a row —
comparing across rows was never meaningful, since rows sit at different heights
by construction.

Neither bug was reachable from a fixture written by the same person who wrote
the detector. Both were one real generation away.

### A second run, properly isolated

The first rerun was invalid: the fixture sat in `/tmp`, which is inside Codex's
sandbox, so the agent found the previous run's transcript and copied its output
instead of generating. Worth recording because the failure looked like a pass.
Redone outside `/tmp` with every prior artefact deleted — zero `/tmp` accesses in
the transcript, 25 commands in its own workdir.

Prompt: *"Add a friendly animated mascot that celebrates when a user completes
checkout in this web app."* No command named, no motion named.

It picked `celebrate` from the vocabulary, and raised the grid to 4x6 on its own
— 24 frames at 24fps, the smoother option the skill mentions but does not
recommend by default. Two rerolls, no `--force`, then clean at 24/24 with 9.6%
scale spread.

Two things came out of it:

- **Conditional ping-pong earns its keep.** The seam measurement bounced this
  loop to 46 frames, where it left the wave loop alone at 12. A celebrate ends
  somewhere different from where it starts; a wave does not. Defaulting either
  way would have been wrong for one of them.
- **Lossless WebP was the wrong default.** The exported loop came out at 2.55MB
  — unusable on a web page. At quality 90 it is 0.55MB with alpha intact, and
  the source is a generated raster that never held lossless detail to begin
  with. Only visible because an agent put the file in a real project and
  reported its size.

## Negative result: no cheap metric ranks identity drift

`spike/identity_probe.py` measured three candidates against real drift, using
within-sheet frame pairs as the noise floor. None of them can be a gate:

| | within-sheet (noise) | ref vs sheet1 | ref vs sheet2 |
| --- | --- | --- | --- |
| palette distance | 0.008 | 0.063 | 0.087 |
| silhouette IoU | 0.740 | 0.750 | 0.772 |
| aspect difference | 0.051 | 0.114 | 0.090 |

- **Silhouette IoU is useless.** The within-sheet noise floor (0.740) is *worse*
  than the drift cases (0.750, 0.772): an arm raised versus lowered changes the
  outline more than the character design does. Pose swamps identity.
- **Palette separates strongly but contradicts aspect.** Palette says sheet2 is
  farther from the anchor; aspect says sheet1 is. They rank the same two
  generations in opposite orders, so at most one is measuring identity.
- **Resolution was ruled out as the cause.** Re-measuring with both images
  scaled to a common height narrows the palette gap (0.087 → 0.069 at 452px,
  → 0.032 at 128px) but never flips the ordering.

Recorded because it corrects an earlier reading in this document: the geometry
scores improved so much on the reroll (4.6% → 0.5% scale spread, no clipping)
that the second sheet was assumed to be closer to the anchor in identity too.
Size-matched inspection says otherwise — sheet1 tracks the anchor's proportions
slightly better. Better geometry is not better likeness, and conflating the two
is exactly the error an automated gate would encode.

So there is no identity check in mascotify. There is `mascotify compare`, which
size-matches the anchor against sampled frames and hands the judgement to a
caller with eyes. Comparing a 1024px anchor against a 390px sheet frame is what
made this hard to see in the first place; fixing the scale mismatch is most of
the work.

## Prompt fixes this run earned

- Demand explicit empty margin: the character occupies the middle ~70% of its
  cell, with clear background on all four sides *including the canvas bottom*.
- State the frame ordering and the invariants (scale, baseline, camera) rather
  than hoping the grid is read the intended way.
- Ask for a flat key colour and no ground shadow; a shadow keys out as a hole.
