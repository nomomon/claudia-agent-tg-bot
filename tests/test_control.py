from pathlib import Path

import pytest
from claudia_harness_claude.control import control_tool_names, resolve_sendable


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    (work / "photo.jpg").write_bytes(b"jpg")
    (work / "notes.txt").write_text("hello")
    (work / ".env").write_text("SECRET=1")
    (work / "sub").mkdir()
    (work / "sub" / "deep.pdf").write_bytes(b"%PDF")
    return work


def test_file_inside_the_folder_is_sendable(folder: Path) -> None:
    assert resolve_sendable("photo.jpg", [folder]) == (folder / "photo.jpg").resolve()
    assert resolve_sendable(str(folder / "sub" / "deep.pdf"), [folder]).name == "deep.pdf"


def test_file_outside_the_folders_is_refused(folder: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    with pytest.raises(ValueError, match="outside the folders"):
        resolve_sendable(str(outside), [folder])
    with pytest.raises(ValueError, match="outside the folders"):
        resolve_sendable("../secret.txt", [folder])


def test_dotfiles_are_never_sent(folder: Path) -> None:
    with pytest.raises(ValueError, match="dotfiles"):
        resolve_sendable(".env", [folder])


def test_symlink_out_of_the_folder_is_refused(folder: Path, tmp_path: Path) -> None:
    target = tmp_path / "db.sqlite3"
    target.write_text("data")
    (folder / "link.txt").symlink_to(target)
    with pytest.raises(ValueError, match="outside the folders"):
        resolve_sendable("link.txt", [folder])


def test_missing_file_and_no_folder_are_refused(folder: Path) -> None:
    with pytest.raises(ValueError, match="no file"):
        resolve_sendable("missing.txt", [folder])
    with pytest.raises(ValueError, match="no folder"):
        resolve_sendable("photo.jpg", [])


def test_control_tool_names_follow_the_flags() -> None:
    assert control_tool_names(self_control=False, outbox=False) == []
    assert control_tool_names(self_control=False, outbox=True) == [
        "mcp__claudia__send_message",
        "mcp__claudia__send_file",
    ]
