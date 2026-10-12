"""The OpenCode installer: copies skills, bakes the plugin root in, writes commands."""
import shlex
import shutil
from pathlib import Path

import pytest

import install_opencode as inst

SKILLS = inst.SKILLS


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    """A copy of the plugin under a path containing a space."""
    real = inst.PLUGIN_ROOT
    root = tmp_path / "my plugin" / "cadra-trace-tracker"
    shutil.copytree(real / "skills", root / "skills")
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "cadra_connect.py").write_text("# stub\n")
    monkeypatch.setattr(inst, "PLUGIN_ROOT", root)
    return root


def run(*args):
    return inst.main(list(args))


def test_copies_three_skills_and_substitutes_root(plugin, tmp_path, capsys):
    target = tmp_path / "cfg"
    assert run("--target", str(target)) == 0
    for name in SKILLS:
        text = (target / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert inst.TOKEN not in text
        assert plugin.as_posix() in text or str(plugin) in text
        assert text.startswith(f"---\nname: {name}\ndescription: ")
    out = capsys.readouterr().out
    assert "/cadra-connect" in out and "opencode debug skill" in out


def test_original_skill_untouched(plugin, tmp_path):
    before = (plugin / "skills" / "cadra-connect" / "SKILL.md").read_bytes()
    run("--target", str(tmp_path / "cfg"))
    assert (plugin / "skills" / "cadra-connect" / "SKILL.md").read_bytes() == before
    assert inst.TOKEN.encode() in before


def test_commands_written_utf8_lf(plugin, tmp_path):
    target = tmp_path / "cfg"
    run("--target", str(target))
    for name in SKILLS:
        raw = (target / "commands" / f"{name}.md").read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
        text = raw.decode("utf-8")
        assert text.startswith("---\ndescription: ")
        assert f"Load the `{name}` skill" in text
        assert text.rstrip().endswith("$ARGUMENTS")


def test_idempotent(plugin, tmp_path):
    target = tmp_path / "cfg"
    run("--target", str(target))
    snap = {p: p.read_bytes() for p in target.rglob("*") if p.is_file()}
    run("--target", str(target))
    assert snap == {p: p.read_bytes() for p in target.rglob("*") if p.is_file()}


def test_dry_run_writes_nothing(plugin, tmp_path, capsys):
    target = tmp_path / "cfg"
    run("--target", str(target), "--dry-run")
    assert not target.exists()
    assert "[dry-run]" in capsys.readouterr().out


def test_uninstall_keeps_foreign_skill(plugin, tmp_path):
    target = tmp_path / "cfg"
    run("--target", str(target))
    foreign = target / "skills" / "other" / "SKILL.md"
    foreign.parent.mkdir()
    foreign.write_text("keep")
    (target / "commands" / "mine.md").write_text("keep")
    run("--target", str(target), "--uninstall")
    assert foreign.read_text() == "keep"
    assert (target / "commands" / "mine.md").exists()
    assert not any((target / "skills" / n).exists() for n in SKILLS)
    assert not any((target / "commands" / f"{n}.md").exists() for n in SKILLS)


def test_project_target(plugin, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    run("--project", str(proj))
    assert (proj / ".opencode" / "skills" / "cadra-submit" / "SKILL.md").is_file()
    assert (proj / ".opencode" / "commands" / "cadra-submit.md").is_file()


def test_default_is_global_under_home(plugin, tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    run()
    home = tmp_path / "home"
    assert (home / ".config" / "opencode" / "skills" / "cadra-traces" / "SKILL.md").is_file()


def test_config_dir_env_override(plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "envcfg"))
    run()
    assert (tmp_path / "envcfg" / "skills" / "cadra-connect").is_dir()


def test_substituted_command_parses_to_existing_script(plugin, tmp_path):
    target = tmp_path / "cfg"
    run("--target", str(target))
    text = (target / "skills" / "cadra-connect" / "SKILL.md").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if "cadra_connect.py" in ln and "python3" in ln)
    # Strip the shell default-expansion so only the baked-in fallback is left to parse.
    line = line.strip().replace("${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-", "").replace("}}", "", 1)
    script = shlex.split(line)[1]
    assert " " in script and Path(script).is_file()
