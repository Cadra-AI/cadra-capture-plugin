"""The root plugin.json follows Agent Plugins 1.0 and agrees with the Claude manifests."""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "cadra-trace-tracker"
ROOT_MANIFEST = PLUGIN / "plugin.json"
CLAUDE_MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
ALLOWED_KEYS = {
    "$schema", "name", "version", "description", "author", "homepage", "repository",
    "license", "keywords", "extensions",
}
NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,62}[a-z0-9])?$")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_top_level_keys_are_the_closed_set():
    assert set(_load(ROOT_MANIFEST)) <= ALLOWED_KEYS


def test_schema_is_agent_plugins_1_0():
    assert _load(ROOT_MANIFEST)["$schema"] == SCHEMA


def test_name_is_valid_and_matches_the_folder():
    name = _load(ROOT_MANIFEST)["name"]
    assert NAME.match(name)
    assert "--" not in name and ".." not in name
    assert name == PLUGIN.name == "cadra-trace-tracker"


def test_author_has_only_allowed_keys():
    assert set(_load(ROOT_MANIFEST)["author"]) <= {"name", "email", "url"}


def test_extension_paths_exist_inside_the_plugin():
    paths = [s for s in _strings(_load(ROOT_MANIFEST)["extensions"]) if s.startswith("./")]
    assert paths, "expected at least one ./ path under extensions"
    for rel in paths:
        target = (PLUGIN / rel).resolve()
        assert target.is_file(), rel
        assert target.is_relative_to(PLUGIN.resolve()), rel


def test_onboarding_skill_is_cadra_connect():
    openai = _load(ROOT_MANIFEST)["extensions"]["com.openai"]
    assert openai["onboardingSkill"] == "./skills/cadra-connect/SKILL.md"


def test_versions_agree_across_manifests():
    assert _load(ROOT_MANIFEST)["version"] == _load(CLAUDE_MANIFEST)["version"] == "3.1.0"


def test_claude_description_matches_root():
    assert _load(CLAUDE_MANIFEST)["description"] == _load(ROOT_MANIFEST)["description"]


def test_no_legacy_codex_manifest():
    assert not (PLUGIN / ".codex-plugin").exists()


def test_descriptions_are_host_neutral():
    entries = [_load(ROOT_MANIFEST), _load(CLAUDE_MANIFEST), *_load(MARKETPLACE)["plugins"]]
    for entry in entries:
        assert "Claude Code work sessions" not in entry["description"]


def test_root_manifest_is_formatted():
    text = ROOT_MANIFEST.read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n"
