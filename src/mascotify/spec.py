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
