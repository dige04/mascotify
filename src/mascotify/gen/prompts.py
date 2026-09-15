"""Compiling a job into the prompt an image model actually needs.

Every clause here was earned by a failed generation. The margin rule in
particular: the first spike asked for baseline, scale and camera invariants but
said nothing about empty space, and the model filled the canvas edge to edge —
amputating the bottom row. Models allocate exactly the space you name.
"""

from __future__ import annotations

from ..spec import CutoutSpec, JobSpec, MotionSpec, PoseJobSpec, SheetSpec

# gpt-image-2 wants both edges on a multiple of 16 and the long:short ratio
# within 3:1. Generators ignore the request often enough that nothing
# downstream may assume it held.
SIZE_ALIGN = 16


def sheet_size(sheet: SheetSpec, cell: int = 512) -> str:
    """A canvas that fits the grid, rounded to what image models accept."""

    def align(v: int) -> int:
        return max(SIZE_ALIGN, round(v / SIZE_ALIGN) * SIZE_ALIGN)

    return f"{align(sheet.cols * cell)}x{align(sheet.rows * cell)}"


def anchor_prompt(character: str, cutout: CutoutSpec | None = None) -> str:
    """The single approved still that locks the character's identity.

    Generated and approved once, then referenced by every animation. Sheets
    drift from their reference; pinning one still is what keeps a mascot
    recognisably itself across separate generations.
    """
    key = (cutout or CutoutSpec()).key_color
    return f"""Use case: stylized-concept
Asset type: app mascot reference sheet, single canonical pose
Subject: {character}
Style/medium: flat vector-style 2D character illustration, clean bold outlines, cel shading
Composition/framing: full body, neutral idle pose facing the viewer, horizontally centered, \
the whole character visible with generous even margin on all four sides, feet near the lower third
Constraints: background must be a completely flat solid {key} filling the entire canvas; \
no drop shadow, no ground shadow, no reflection, no floor line; no text, no logo, no watermark, \
no border, no frame
Avoid: any gradient or texture in the background, any tint of the background colour on the \
character itself, the character touching any canvas edge"""


def sheet_prompt(
    job: JobSpec,
    *,
    character: str = "",
    from_reference: bool = True,
    margin_pct: int = 10,
) -> str:
    """The grid prompt.

    `margin_pct` is stated as a hard number because qualitative words like
    "generous" do not survive contact with a grid layout — the model spends the
    margin on gutters between cells and leaves none at the canvas edge.
    """
    s, m = job.sheet, job.motion
    key = job.cutout.key_color
    n = s.frames
    subject = (
        "the EXACT SAME character as the reference image, whose design is the invariant"
        if from_reference
        else character
    )

    return f"""Use case: stylized-concept
Asset type: game sprite sheet, {n}-frame looping animation
Primary request: a {s.cols}-column by {s.rows}-row grid of {n} animation frames of \
{subject}, performing one smooth looping "{m.action}" — {m.phrase()}
{"Input images: Image 1: the reference character; its design is the invariant" if from_reference else ""}
Composition/framing: exactly {s.cols} equal columns and {s.rows} equal rows of equal-size cells. \
Frames read left-to-right then top-to-bottom, frame 1 top-left through frame {n} bottom-right. \
In EVERY cell the character is the identical character at the IDENTICAL scale, horizontally \
centered in its own cell, with its feet on the IDENTICAL vertical baseline. Frame {n} must flow \
back into frame 1 seamlessly.
Constraints: leave at least {margin_pct}% of the canvas height as empty background BELOW the \
bottom row of characters, and the same empty margin ABOVE the top row. Every character must sit \
fully inside its own cell with visible background on all four sides. NO character may touch or \
cross any canvas edge. The ONLY thing that changes between frames is the motion described above; \
keep the head, face, body, colours, outline weight and proportions identical across all {n} cells. \
Background must be completely flat solid {key} across the whole canvas and inside every cell; \
no drop shadow, no ground shadow.
Avoid: visible grid lines, cell borders, dividers, frame numbers, labels, captions, text, \
watermark, drop shadows; resizing or re-cropping the character between cells; changing the \
camera angle; any character touching a canvas edge"""


def repair_prompt(problems: list[str], job: JobSpec) -> str:
    """A targeted follow-up after validation failed.

    One change at a time, invariants repeated — an image model asked to fix
    three things at once usually regenerates a different character instead.
    """
    bullets = "\n".join(f"- {p}" for p in problems)
    return f"""The previous sprite sheet failed automated validation:

{bullets}

Regenerate the sheet fixing ONLY those problems. Keep the same character design, \
the same {job.sheet.cols}x{job.sheet.rows} grid, the same "{job.motion.action}" motion, and the \
same flat {job.cutout.key_color} background. The character must stay identical to the previous \
sheet in every other respect."""


def agent_brief(
    out_path: str,
    prompt: str,
    size: str,
    *,
    next_command: str,
    next_note: str,
    size_note: str = "approximate is fine — mascotify measures the real grid, "
    "it does not assume the requested resolution",
) -> str:
    """Instructions handed to a coding agent that will do the generating.

    The agent is the provider in this mode: mascotify compiles the prompt and
    processes the result, the agent's own image tool does the drawing. No API
    key is involved anywhere in that loop.

    The follow-up step is passed in rather than patched into the text
    afterwards. An earlier version rewrote the command with a string replace and
    left the paragraph below it describing a different step — telling the agent
    that `plan` validates a sheet, which it does not.
    """
    return f"""Generate this image with your built-in image generation tool, then save it to:
  {out_path}

Target size: {size} ({size_note})

--- prompt ---
{prompt}
--- end prompt ---

When the file is saved, run:
  {next_command}

{next_note}"""


# --------------------------------------------------------------------------- poses


def _pose_frame(job: "PoseJobSpec", subject: str, cells: list[tuple[str, str]], what: str) -> str:
    """Shared scaffolding for the two pose grids.

    Deliberately close to `sheet_prompt` — same margin rule, same flat backdrop,
    same identity invariants — because those clauses were earned the same way.
    What differs is that the cells are enumerated rather than described as a
    motion: there is no "smooth loop" to interpolate, each cell is a distinct
    named pose and the model has to be told which cell holds which.
    """
    p = job.pose
    key = job.cutout.key_color
    listing = "\n".join(
        f"  cell {i + 1} ({'top' if i < 3 else 'middle' if i < 6 else 'bottom'}-"
        f"{('left', 'centre', 'right')[i % 3]}): {desc}"
        for i, (_, desc) in enumerate(cells)
    )
    return f"""Use case: stylized-concept
Asset type: character pose sheet, {p.cells} poses, {what}
Primary request: a {p.cols}-column by {p.rows}-row grid of {p.cells} poses of {subject}
Input images: Image 1: the reference character; its design is the invariant
Composition/framing: exactly {p.cols} equal columns and {p.rows} equal rows of equal-size cells, \
read left-to-right then top-to-bottom. The cells are, in that order:
{listing}
In EVERY cell it is the identical character at the IDENTICAL scale, drawn from the IDENTICAL \
camera angle, with its body in the IDENTICAL position and its feet on the IDENTICAL vertical \
baseline. The body does not move, shift sideways, lean, or change size between cells — only \
{what} changes.
Constraints: leave at least 10% of the canvas height as empty background below the bottom row \
and above the top row. Every character must sit fully inside its own cell with visible \
background on all four sides. NO character may touch or cross any canvas edge. Background must \
be completely flat solid {key} across the whole canvas and inside every cell; no drop shadow, \
no ground shadow.
Avoid: visible grid lines, cell borders, dividers, arrows, frame numbers, labels, captions, \
text, watermark; resizing, re-cropping or re-posing the body between cells; changing the camera \
angle; any character touching a canvas edge"""


def directions_prompt(job: "PoseJobSpec") -> str:
    """The nine head directions a cursor-tracker reads by compass position."""
    from ..spec import DIRECTIONS

    return _pose_frame(
        job,
        "the EXACT SAME character as the reference image, whose design is the invariant",
        list(DIRECTIONS),
        "the direction the head and eyes are looking",
    )


def reactions_prompt(job: "PoseJobSpec", reactions: list[tuple[str, str]]) -> str:
    """The nine expressions shown on a click."""
    return _pose_frame(
        job,
        "the EXACT SAME character as the reference image, whose design is the invariant",
        reactions,
        "the facial expression",
    )


def pose_repair_prompt(problems: list[str], job: "PoseJobSpec", which: str) -> str:
    """A targeted follow-up after a pose grid failed validation."""
    bullets = "\n".join(f"- {p}" for p in problems)
    return f"""The previous {which} sheet failed automated validation:

{bullets}

Regenerate the sheet fixing ONLY those problems. Keep the same character design, the same \
{job.pose.cols}x{job.pose.rows} grid, the same pose in the same cell, and the same flat \
{job.cutout.key_color} background. The character must stay identical to the previous sheet in \
every other respect."""
