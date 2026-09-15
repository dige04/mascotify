"""Core value types and defaults.

Defaults mirror what a 12-frame mascot loop actually needs on a phone screen:
a 4x3 grid read left-to-right, played back at 12 fps, rendered at a 120pt box.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal

# The motion vocabulary an agent picks from. Kept small on purpose: each entry
# has to survive being drawn 12 times by an image model without the character
# drifting, which rules out anything involving locomotion or a camera change.
MOTIONS: dict[str, str] = {
    "wave": "waving hello with one arm, elbow pivoting, body otherwise still",
    "idle": "a subtle breathing idle, chest rising and settling, tiny head drift",
    "bounce": "bouncing in place on both feet, squash on landing and stretch at apex",
    "nod": "nodding the head yes, chin dipping and returning",
    "blink": "blinking, eye closing and reopening, body completely still",
    "point": "raising one arm to point forward at the viewer, then holding",
    "typing": "typing on an unseen keyboard, both hands alternating up and down",
    "note-taking": "writing on a small notepad, writing hand moving in short strokes",
    "think": "one hand raised to the chin in thought, weight shifting slightly",
    "celebrate": "throwing both arms up in celebration, then settling back",
}

Engine = Literal["sheet", "video"]


@dataclass(frozen=True)
class SheetSpec:
    """Geometry of a generated sprite grid."""

    rows: int = 3
    cols: int = 4
    fps: int = 12
    # Rendered box on an iOS screen, in points. Drives @1x/@2x/@3x export.
    base_pt: int = 120
    # Play the loop forward then backward to hide a seam, at the cost of making
    # the motion symmetric. None measures the actual seam and decides; a grid
    # generated in one pass usually closes on its own and needs no help.
    ping_pong: bool | None = None
    # Cross-fade radius in frames when resampling; 0 disables.
    smooth: int = 1

    @property
    def frames(self) -> int:
        return self.rows * self.cols

    def validate(self) -> None:
        if self.rows < 1 or self.cols < 1:
            raise ValueError("rows and cols must both be >= 1")
        if self.frames > 64:
            raise ValueError(f"{self.frames} frames is beyond what one image can hold legibly")
        if self.fps < 1 or self.fps > 60:
            raise ValueError("fps must be between 1 and 60")


@dataclass(frozen=True)
class CutoutSpec:
    """How the character gets separated from its background."""

    # "chroma" keys out a flat generated backdrop; "matte" runs a local
    # segmentation model; "alpha" trusts the alpha the generator already wrote.
    method: Literal["chroma", "matte", "alpha"] = "chroma"
    key_color: str = "#00FF00"
    # Sample the real key from the image instead of trusting key_color. Image
    # models drift off a requested hex often enough that this is the default.
    auto_key: Literal["none", "corners", "border"] = "corners"
    tolerance: int = 12
    soft_matte: bool = True
    transparent_threshold: float = 12.0
    opaque_threshold: float = 96.0
    despill: bool = True
    edge_contract: int = 0
    edge_feather: float = 0.0


@dataclass(frozen=True)
class MotionSpec:
    """What the character should be doing."""

    action: str = "wave"
    description: str = ""
    name: str = "Mascot"

    def phrase(self) -> str:
        """The clause that goes into the image prompt."""
        return MOTIONS.get(self.action, self.description or self.action)


@dataclass
class JobSpec:
    """Everything needed to reproduce one animation, start to finish."""

    motion: MotionSpec = field(default_factory=MotionSpec)
    sheet: SheetSpec = field(default_factory=SheetSpec)
    cutout: CutoutSpec = field(default_factory=CutoutSpec)
    engine: Engine = "sheet"
    reference_sha: str = ""
    anchor_sha: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    def write(self, path: Path) -> None:
        path.write_text(self.to_json() + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "JobSpec":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            motion=MotionSpec(**raw.get("motion", {})),
            sheet=SheetSpec(**raw.get("sheet", {})),
            cutout=CutoutSpec(**raw.get("cutout", {})),
            engine=raw.get("engine", "sheet"),
            reference_sha=raw.get("reference_sha", ""),
            anchor_sha=raw.get("anchor_sha", ""),
        )


def parse_hex(value: str) -> tuple[int, int, int]:
    v = value.strip().lstrip("#")
    if len(v) != 6:
        raise ValueError(f"expected a 6-digit hex colour, got {value!r}")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


# --------------------------------------------------------------------------- poses

# A cursor-tracking mascot is not an animation. It is two 3x3 grids of poses:
# the pointer's angle picks a cell on the directions grid, a click shows a cell
# on the reactions grid. Nothing plays, so there is no fps and no loop seam —
# which is why this vocabulary sits apart from MOTIONS rather than joining it.
#
# Cell order on the directions grid is the contract. It is read left-to-right,
# top-to-bottom, and the consumer maps index to compass heading, so the order
# below is load-bearing in a way a motion loop's frame order is not. No
# validator can check it either: nine plausible head turns in the wrong cells
# measure perfectly and track backwards.
DIRECTIONS: tuple[tuple[str, str], ...] = (
    ("up-left", "head turned up and to its left, eyes looking up-left"),
    ("up", "head tilted up, eyes looking straight up"),
    ("up-right", "head turned up and to its right, eyes looking up-right"),
    ("left", "head turned to its left, eyes looking left"),
    ("center", "head straight ahead, eyes looking directly at the viewer, resting pose"),
    ("right", "head turned to its right, eyes looking right"),
    ("down-left", "head lowered and turned to its left, eyes looking down-left"),
    ("down", "head lowered, eyes looking straight down"),
    ("down-right", "head lowered and turned to its right, eyes looking down-right"),
)

# Cell order here is free — a click picks one at random or by name — so these
# are chosen for range rather than sequence.
REACTIONS: dict[str, str] = {
    "surprised": "eyes wide, mouth open in a small O, ears or hair perked up",
    "happy": "a wide closed-eye smile, cheeks raised",
    "laughing": "head tipped back mid-laugh, eyes squeezed shut, big open smile",
    "wink": "one eye closed in a wink, a small crooked smile",
    "sleepy": "eyes half closed, mid-yawn, drowsy",
    "shy": "looking away bashfully, blush on both cheeks, small smile",
    "curious": "one eyebrow raised, head tilted slightly, questioning look",
    "dizzy": "spiral or swirl eyes, dazed open mouth",
    "love": "heart-shaped eyes, delighted grin",
}

POSE_SETS = ("directions", "reactions")


@dataclass(frozen=True)
class PoseSpec:
    """Geometry of one page-mascot character.

    The 3x3 grid is fixed by the consuming component rather than chosen here,
    so unlike `SheetSpec` these are not tunable — they are recorded so a job can
    be reopened and reproduced.
    """

    rows: int = 3
    cols: int = 3
    # The component renders into a square box (its `size` prop is one number),
    # so the packed cell has to be square or the character gets letterboxed.
    base_px: int = 140
    # Fraction of the character's height, measured from the feet up, used as the
    # body anchor when seating frames. See `normalize.foot_anchor_x`.
    anchor_frac: float = 0.25
    # How far the anchor may wander across the eighteen cells, as a fraction of
    # character width, before the pair is rejected.
    max_anchor_drift: float = 0.06

    @property
    def cells(self) -> int:
        return self.rows * self.cols

    def validate(self) -> None:
        if (self.rows, self.cols) != (3, 3):
            raise ValueError("a page-mascot character is a 3x3 grid; other shapes are not read")
        if self.base_px < 16:
            raise ValueError("base_px below 16 renders the mascot illegibly small")


@dataclass
class PoseJobSpec:
    """Everything needed to reproduce one pose pair."""

    name: str = "mascot"
    character: str = ""
    pose: PoseSpec = field(default_factory=PoseSpec)
    cutout: CutoutSpec = field(default_factory=CutoutSpec)
    anchor_sha: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    def write(self, path: Path) -> None:
        path.write_text(self.to_json() + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "PoseJobSpec":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            name=raw.get("name", "mascot"),
            character=raw.get("character", ""),
            pose=PoseSpec(**raw.get("pose", {})),
            cutout=CutoutSpec(**raw.get("cutout", {})),
            anchor_sha=raw.get("anchor_sha", ""),
        )
