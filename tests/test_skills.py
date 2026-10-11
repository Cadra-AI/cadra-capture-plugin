"""The three skills must load in every host: Agent Skills front matter, portable commands."""
import re
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "cadra-trace-tracker"
SKILL_DIRS = sorted((PLUGIN / "skills").iterdir())
COMMAND = re.compile(
    r'python3 "\$\{CLAUDE_PLUGIN_ROOT:-\$\{PLUGIN_ROOT:-<PLUGIN_ROOT>\}\}/scripts/'
    r'cadra_[a-z]+\.py"'
)


def _text(skill_dir: Path) -> str:
    return (skill_dir / "SKILL.md").read_text(encoding="utf-8")


def _front_matter(text: str) -> dict:
    lines = text.splitlines()
    end = lines.index("---", 1)
    pairs = (line.split(": ", 1) for line in lines[1:end] if ": " in line)
    return {key: value for key, value in pairs}


def test_there_are_three_skills():
    assert [d.name for d in SKILL_DIRS] == ["cadra-connect", "cadra-submit", "cadra-traces"]


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=lambda d: d.name)
def test_front_matter_starts_on_line_one(skill_dir):
    assert _text(skill_dir).splitlines()[0] == "---"


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=lambda d: d.name)
def test_name_matches_folder(skill_dir):
    name = _front_matter(_text(skill_dir))["name"]
    assert name == skill_dir.name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name)
    assert len(name) <= 64


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=lambda d: d.name)
def test_description_present_and_bounded(skill_dir):
    description = _front_matter(_text(skill_dir)).get("description", "")
    assert description.strip()
    assert len(description) <= 1024
    assert "Claude Code" not in description


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=lambda d: d.name)
def test_every_command_uses_the_portable_root(skill_dir):
    lines = _text(skill_dir).splitlines()
    commands = [line for line in lines if "scripts/cadra_" in line]
    assert commands
    for line in commands:
        assert COMMAND.search(line), line
    for line in lines:
        assert '"$CLAUDE_PLUGIN_ROOT/' not in line
        assert not line.strip().startswith('python "')


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=lambda d: d.name)
def test_plugin_root_placeholder_is_defined(skill_dir):
    body = _text(skill_dir)
    assert "<PLUGIN_ROOT>" in body
    assert "two directories above" in body
    assert "py -3" in body


def test_no_claude_binary_instruction():
    body = _text(PLUGIN / "skills" / "cadra-submit")
    assert "starting `claude`" not in body
    assert "starting your agent from the workspace root" in body
