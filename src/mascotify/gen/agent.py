"""Generating through a coding agent instead of an API key.

This is the premise the rest of the project rests on: the agent already has an
image tool, so mascotify can compile the prompt and let the agent draw. The CLI
and the skill have always worked that way. The web app could not, which meant
the one interface a non-developer would reach for was the one that demanded a
key — so this closes that gap by driving the agent's CLI as a subprocess.

Slower than an HTTP call and it needs an agent installed and signed in, but it
costs nothing per image and needs no key at all.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..net import HttpError

# Which CLIs can be driven this way, in the order they are tried. Codex is first
# because its built-in image tool needs no key of its own; the others are
# accepted when present but are not guaranteed to have image generation.
AGENT_CLIS = ("codex", "gemini")

# Generation through an agent is a whole model turn plus tool use, so it is much
# slower than an HTTP round trip. A 4x6 grid at high quality can genuinely take
# a few minutes.
DEFAULT_TIMEOUT = 420.0


@dataclass(frozen=True)
class AgentCLI:
    name: str
    path: str


def available() -> list[AgentCLI]:
    """Agent CLIs on PATH, best first."""
    return [AgentCLI(name=n, path=p) for n in AGENT_CLIS if (p := shutil.which(n))]


def find(preferred: str = "") -> AgentCLI:
    found = available()
    if preferred:
        for a in found:
            if a.name == preferred:
                return a
        raise HttpError(
            f"{preferred!r} is not on PATH. Available: {', '.join(a.name for a in found) or 'none'}"
        )
    if not found:
        raise HttpError(
            "no coding agent found on PATH. Install one (codex, gemini) and sign in, "
            "or pick a provider with an API key in settings."
        )
    return found[0]


def _brief(prompt: str, out_name: str, size: str, has_reference: bool) -> str:
    """The instruction handed to the agent.

    Deliberately explicit about using the built-in tool: the fallback CLI path
    inside an agent's own image skill usually wants an API key, which is the
    thing this route exists to avoid.
    """
    look = (
        "First load ./reference.png with your image-viewing tool so the character is in context.\n"
        if has_reference
        else ""
    )
    return (
        "Use your built-in image generation tool. Do not use an API-key fallback "
        "path, and do not install anything.\n"
        f"{look}"
        f"Generate exactly one image at approximately {size} and save it to ./{out_name}\n"
        "Do exactly one generation, then stop. Do not write any other files, and do "
        "not edit the image afterwards.\n\n"
        "--- prompt ---\n"
        f"{prompt}\n"
        "--- end prompt ---\n"
    )


def generate(
    prompt: str,
    *,
    reference: bytes | None = None,
    size: str = "1024x1024",
    cli: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> bytes:
    """Run the agent in a scratch directory and read back what it drew.

    A temporary working directory keeps the agent from touching the user's
    project: it is told to write one file, and only that file is read back.
    """
    agent = find(cli)
    out_name = "mascot.png"

    with tempfile.TemporaryDirectory(prefix="mascotify-agent-") as tmp:
        work = Path(tmp)
        if reference is not None:
            (work / "reference.png").write_bytes(reference)

        brief = _brief(prompt, out_name, size, reference is not None)
        argv = _argv(agent, brief)

        try:
            proc = subprocess.run(
                argv,
                cwd=work,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise HttpError(
                f"{agent.name} did not finish within {timeout:.0f}s. Generation through "
                f"an agent is slow; try a smaller grid, or use a provider key."
            ) from exc
        except OSError as exc:
            raise HttpError(f"could not run {agent.name}: {exc}") from exc

        produced = work / out_name
        if not produced.exists():
            # The agent may have refused, or written somewhere else. Its own
            # last words are usually the most useful thing to show.
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
            detail = " ".join(t.strip() for t in tail if t.strip())[:400]
            raise HttpError(f"{agent.name} produced no image{': ' + detail if detail else ''}")

        data = produced.read_bytes()
        if not data:
            raise HttpError(f"{agent.name} wrote an empty file")
        return data


def _argv(agent: AgentCLI, brief: str) -> list[str]:
    """Each CLI spells non-interactive-with-write-access differently."""
    if agent.name == "codex":
        return [
            agent.path,
            "exec",
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            brief,
        ]
    if agent.name == "gemini":
        return [agent.path, "--yolo", "--prompt", brief]
    raise HttpError(f"no non-interactive invocation known for {agent.name!r}")
