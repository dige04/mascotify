"""The key-free provider, driven against a fake agent CLI.

This route is what makes the web app usable by someone who has no API key, so
the properties worth pinning are the ones a user would hit first: it finds an
agent, it hands over the reference, it reads back only the file it asked for,
and every way it can fail says something actionable instead of a traceback.

A stub shell script stands in for the real CLI — invoking a signed-in agent
would make the suite slow, networked, and dependent on someone's quota.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from mascotify.gen import agent, images
from mascotify.net import HttpError

PNG = b"\x89PNG\r\n\x1a\n" + b"drawn-by-agent" * 4


def fake_cli(tmp_path: Path, name: str = "codex", body: str = "") -> Path:
    """A stand-in agent CLI on a throwaway PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / name
    script.write_text(
        "#!/bin/sh\n"
        + (body or f'printf %s "{PNG.decode("latin-1")}" > ./mascot.png\n')
        + "exit 0\n",
        encoding="latin-1",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return bindir


def use(monkeypatch, bindir: Path) -> None:
    """Put the fake first on PATH. Prepending rather than replacing keeps
    coreutils reachable for the stub script, and shutil.which takes the first
    match so a real agent further down the path is never selected."""
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


@pytest.fixture
def only_fake(monkeypatch, tmp_path):
    bindir = fake_cli(tmp_path)
    use(monkeypatch, bindir)
    return bindir


def test_it_draws_without_any_key(only_fake):
    out = images.generate("agent", prompt="a robot", size="512x512")
    assert out.png.startswith(b"\x89PNG")
    assert out.provider == "agent"


def test_the_agent_provider_is_the_default_and_free():
    assert images.PROVIDERS[0] == "agent"
    assert "agent" not in images.NEEDS_KEY
    assert images.ROUGH_COST_USD["agent"] == 0


def test_no_key_is_required_or_sent(only_fake, monkeypatch):
    for env in images.ENV_KEYS.values():
        if env:
            monkeypatch.delenv(env, raising=False)
    assert images.resolve_key("agent") == ""
    assert images.generate("agent", prompt="p").png


def test_readiness_follows_whether_a_cli_is_installed(monkeypatch, tmp_path, only_fake):
    assert images.is_ready("agent") is True
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert images.is_ready("agent") is False


def test_a_missing_agent_says_what_to_do(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(HttpError, match="no coding agent found"):
        images.generate("agent", prompt="p")


def test_the_reference_is_handed_to_the_agent(monkeypatch, tmp_path):
    """A sheet has to be drawn from the approved anchor or the mascot drifts."""
    seen = tmp_path / "seen.txt"
    bindir = fake_cli(
        tmp_path,
        body=(f'ls > "{seen}"\nprintf %s "{PNG.decode("latin-1")}" > ./mascot.png\n'),
    )
    use(monkeypatch, bindir)
    images.generate("agent", prompt="a grid", reference=PNG)
    assert "reference.png" in seen.read_text()


def test_it_works_in_a_scratch_directory_not_the_project(monkeypatch, tmp_path):
    """The agent gets write access, so it must not be pointed at the user's repo."""
    cwd = tmp_path / "where.txt"
    bindir = fake_cli(
        tmp_path,
        body=(f'pwd > "{cwd}"\nprintf %s "{PNG.decode("latin-1")}" > ./mascot.png\n'),
    )
    use(monkeypatch, bindir)
    before = Path.cwd()
    images.generate("agent", prompt="p")
    ran_in = cwd.read_text().strip()
    assert "mascotify-agent-" in ran_in
    assert Path.cwd() == before, "the caller's working directory must be untouched"


def test_an_agent_that_draws_nothing_reports_its_own_words(monkeypatch, tmp_path):
    bindir = fake_cli(tmp_path, body='echo "I will not draw that" >&2\n')
    use(monkeypatch, bindir)
    with pytest.raises(HttpError, match="will not draw that"):
        images.generate("agent", prompt="p")


def test_an_empty_file_is_not_accepted_as_an_image(monkeypatch, tmp_path):
    bindir = fake_cli(tmp_path, body="> ./mascot.png\n")
    use(monkeypatch, bindir)
    with pytest.raises(HttpError, match="empty file"):
        images.generate("agent", prompt="p")


def test_a_hung_agent_times_out_with_advice(monkeypatch, tmp_path):
    bindir = fake_cli(tmp_path, body="sleep 5\n")
    use(monkeypatch, bindir)
    with pytest.raises(HttpError, match="did not finish"):
        agent.generate("p", timeout=0.4)


def test_naming_a_cli_that_is_not_installed_lists_what_is(only_fake):
    with pytest.raises(HttpError, match="not on PATH"):
        agent.generate("p", cli="definitely-not-installed")


def test_the_brief_forbids_the_key_requiring_fallback(only_fake):
    """An agent's own image skill usually has an API-key fallback path, which is
    the exact thing this route exists to avoid."""
    brief = agent._brief("draw a robot", "mascot.png", "1024x1024", has_reference=True)
    assert "built-in" in brief
    assert "API-key fallback" in brief
    assert "reference.png" in brief
    assert "exactly one" in brief


def test_known_clis_have_a_non_interactive_invocation():
    for name in agent.AGENT_CLIS:
        argv = agent._argv(agent.AgentCLI(name=name, path=f"/usr/bin/{name}"), "brief")
        assert argv[0].endswith(name)
        assert any("brief" == a for a in argv), f"{name} never receives the prompt"


def test_an_unknown_cli_is_refused_rather_than_guessed():
    with pytest.raises(HttpError, match="no non-interactive invocation"):
        agent._argv(agent.AgentCLI(name="something-else", path="/bin/true"), "brief")


@pytest.mark.skipif(os.name == "nt", reason="the fake CLI is a shell script")
def test_generation_leaves_no_scratch_behind(only_fake, tmp_path):
    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("mascotify-agent-*"))
    images.generate("agent", prompt="p")
    after = set(Path(tempfile.gettempdir()).glob("mascotify-agent-*"))
    assert after == before
