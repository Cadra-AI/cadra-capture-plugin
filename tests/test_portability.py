"""Path handling that must hold on macOS, Linux and Windows.

Runs on any OS: Windows behaviour is exercised through PureWindowsPath and
string paths, not by running on Windows.
"""
from pathlib import Path, PureWindowsPath

import pytest

from cadra import adapt, antigravity, codex, collect, copilot, cursor, kiro, opencode

WIN_WS = Path("C:/Users/x/proj")


@pytest.mark.parametrize("value", [
    "C:\\Users\\x\\proj\\src\\a.py",
    "c:/users/x/proj/src/a.py",
    "C:/Users/x\\proj/src\\a.py",
    "C:\\Users\\x\\proj\\src\\..\\src\\a.py",
])
def test_windows_paths_rebase_to_relative_posix(value):
    assert adapt.rebase_path(value, WIN_WS) == "src/a.py"


def test_windows_trailing_slash_on_workspace():
    assert adapt.rebase_path("C:/Users/x/proj/a.py", Path("C:/Users/x/proj/")) == "a.py"


def test_windows_sibling_with_shared_prefix_is_not_inside():
    out = adapt.rebase_path("C:\\Users\\x\\proj-other\\a.py", WIN_WS)
    assert out == "C:/Users/x/proj-other/a.py"


def test_windows_path_on_another_drive_stays_absolute():
    assert adapt.rebase_path("D:\\proj\\a.py", WIN_WS) == "D:/proj/a.py"


def test_relative_windows_path_only_gets_forward_slashes():
    assert adapt.rebase_path("src\\a.py", WIN_WS) == "src/a.py"


def test_posix_paths_still_rebase(tmp_path):
    assert adapt.rebase_path(str(tmp_path / "src" / "a.py"), tmp_path) == "src/a.py"
    assert adapt.rebase_path("/elsewhere/a.py", tmp_path) == "/elsewhere/a.py"


def test_claude_dir_encoding_matches_for_windows_workspace():
    name = collect.encode_dir_name(PureWindowsPath("C:\\Users\\x\\proj"))
    assert name == "C--Users-x-proj"


def _windows_collect(monkeypatch):
    class FakePath(PureWindowsPath):
        def resolve(self):  # no filesystem here; keep the lexical value
            return self

    class FakeOs:
        name = "nt"

    monkeypatch.setattr(collect, "Path", FakePath)
    monkeypatch.setattr(collect, "os", FakeOs)


@pytest.mark.parametrize("cwd", ["/c/Users/x/proj", "/C/Users/x/proj/src", "C:\\Users\\x\\proj"])
def test_msys_and_native_cwds_are_in_scope_on_windows(monkeypatch, cwd):
    _windows_collect(monkeypatch)
    assert collect.in_scope(cwd, WIN_WS)


def test_windows_scope_is_case_blind_and_not_prefix_based(monkeypatch):
    _windows_collect(monkeypatch)
    assert collect.in_scope("c:/USERS/X/PROJ/sub", WIN_WS)
    assert not collect.in_scope("C:/Users/x/proj-other", WIN_WS)
    assert not collect.in_scope("D:/Users/x/proj", WIN_WS)


def test_posix_scope_via_symlink_and_trailing_slash(tmp_path):
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert collect.in_scope(str(link) + "/sub/", real)
    assert not collect.in_scope(str(tmp_path), real)


def test_default_roots_follow_home_and_env(monkeypatch, tmp_path):
    for var in ("CODEX_HOME", "CURSOR_HOME", "OPENCODE_HOME", "XDG_DATA_HOME",
                "ANTIGRAVITY_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert codex.default_root() == tmp_path / ".codex" / "sessions"
    assert kiro.default_root() == tmp_path / ".kiro" / "sessions"
    assert copilot.default_root() == tmp_path / ".copilot" / "session-state"
    assert cursor.default_root() == tmp_path / ".cursor"
    assert antigravity.default_root() == tmp_path / ".gemini" / "antigravity-cli"
    assert opencode.default_root() == tmp_path / ".local" / "share" / "opencode"


def test_env_overrides_win(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path / "u"))
    monkeypatch.setenv("ANTIGRAVITY_HOME", str(tmp_path / "a"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "x"))
    assert codex.default_root() == tmp_path / "c" / "sessions"
    assert cursor.default_root() == tmp_path / "u"
    assert antigravity.default_root() == tmp_path / "a"
    assert opencode.default_root() == tmp_path / "x" / "opencode"
    monkeypatch.setenv("OPENCODE_HOME", str(tmp_path / "o"))
    assert opencode.default_root() == tmp_path / "o"


@pytest.mark.parametrize("uri,expected", [
    ("file:///C:/Users/x/proj", "C:/Users/x/proj"),
    ("file:///Users/x/my%20proj", "/Users/x/my proj"),
])
def test_antigravity_file_uri_to_path(uri, expected):
    assert antigravity._uri_to_path(uri) == expected


def test_sqlite_uri_survives_spaces(tmp_path):
    import sqlite3
    db = tmp_path / "dir with space" / "o.db"
    db.parent.mkdir()
    sqlite3.connect(db).close()
    with opencode._connect(db) as conn:
        assert conn.execute("select 1").fetchone()[0] == 1


def test_kiro_copilot_claude_env_overrides(monkeypatch, tmp_path):
    import cadra_submit
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "k"))
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "p"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cl"))
    assert kiro.default_root() == tmp_path / "k" / "sessions"
    assert copilot.default_root() == tmp_path / "p" / "session-state"
    assert cadra_submit._claude_root() == tmp_path / "cl" / "projects"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert cadra_submit._claude_root() == tmp_path / ".claude" / "projects"


def test_windows_style_roots_join_under_userprofile():
    home = PureWindowsPath("C:/Users/x")
    assert str(home / ".claude" / "projects") == "C:\\Users\\x\\.claude\\projects"
    assert str(home / ".gemini" / "antigravity-cli") == "C:\\Users\\x\\.gemini\\antigravity-cli"
    assert str(home / ".local" / "share" / "opencode") == "C:\\Users\\x\\.local\\share\\opencode"
