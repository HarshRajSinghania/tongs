"""Per-user desktop entry ownership and escaping tests."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

import tongs.desktop.installer.menu as menu_module
from tongs.desktop.installer.menu import (
    desktop_entry_digest,
    install_user_menu,
    remove_user_menu,
    render_desktop_entry,
)
from tongs.desktop.installer.models import InstallerError, InstallerErrorCode


def test_exec_escapes_hostile_reserved_characters_and_percent_fields() -> None:
    entry = render_desktop_entry(Path('/home/me/Tongs $`\\" %f/bin/tongs'))
    text = entry.decode("ascii")

    assert 'Exec="' in text
    assert "%%f" in text
    assert "\\\\$" in text
    assert "\\\\`" in text
    assert "\\\\\\\\" in text
    assert text.endswith("StartupNotify=true\n")


def test_icon_uses_an_absolute_utf8_desktop_entry_value() -> None:
    entry = render_desktop_entry(
        Path("/opt/tongs/bin/tongs"),
        icon_path=Path("/opt/Tongs Icons/Δ\\tongs.png"),
    )

    assert "Icon=/opt/Tongs\\sIcons/Δ\\\\tongs.png\n" in entry.decode("utf-8")


@pytest.mark.parametrize(
    "value",
    [
        "relative/tongs.png",
        "/tmp/newline\nicon.png",
        "/tmp/tab\ticon.png",
        "/tmp/control\x01icon.png",
        "/tmp/delete\x7ficon.png",
        "/tmp/surrogate\udcfficon.png",
    ],
)
def test_unrepresentable_icon_path_is_rejected(value: str) -> None:
    with pytest.raises(InstallerError) as raised:
        render_desktop_entry(Path("/opt/tongs/bin/tongs"), icon_path=Path(value))

    assert raised.value.code is InstallerErrorCode.INCOMPATIBLE


@pytest.mark.parametrize(
    "value",
    [
        "relative/tongs",
        "/tmp/a=b/tongs",
        "/tmp/é/tongs",
        "/tmp/tab\tpath/tongs",
        "/tmp/control\x01path/tongs",
        "/tmp/delete\x7fpath/tongs",
    ],
)
def test_unrepresentable_console_path_is_rejected(value: str) -> None:
    with pytest.raises(InstallerError) as raised:
        render_desktop_entry(Path(value))

    assert raised.value.code is InstallerErrorCode.INCOMPATIBLE


def test_install_replace_and_remove_require_exact_owned_content(tmp_path: Path) -> None:
    path = tmp_path / "data" / "applications" / "tongs.desktop"
    first_console = Path("/opt/Tongs One/bin/tongs")
    second_console = Path("/opt/Tongs Two/bin/tongs")

    first = install_user_menu(path, first_console, replace_digest=None)

    assert first == hashlib.sha256(path.read_bytes()).hexdigest()
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert install_user_menu(path, first_console, replace_digest=None) == first

    path.write_text("[Desktop Entry]\nName=Unrelated\n")
    with pytest.raises(InstallerError, match="another installation"):
        install_user_menu(path, second_console, replace_digest=first)
    with pytest.raises(InstallerError, match="changed"):
        remove_user_menu(path, expected_digest=first)
    assert path.exists()


def test_owned_entry_can_be_replaced_and_uninstall_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "applications" / "tongs.desktop"
    first = install_user_menu(path, Path("/opt/one/bin/tongs"), replace_digest=None)
    second = install_user_menu(path, Path("/opt/two/bin/tongs"), replace_digest=first)

    assert second != first
    assert remove_user_menu(path, expected_digest=second) is True
    assert remove_user_menu(path, expected_digest=second) is False


def test_symlinked_applications_folder_owned_by_the_user_installs(
    tmp_path: Path,
) -> None:
    dotfiles = tmp_path / "dotfiles" / "applications"
    dotfiles.mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    (data / "applications").symlink_to(dotfiles, target_is_directory=True)
    path = data / "applications" / "tongs.desktop"

    digest = install_user_menu(path, Path("/opt/one/bin/tongs"), replace_digest=None)

    assert (data / "applications").is_symlink()
    assert hashlib.sha256((dotfiles / "tongs.desktop").read_bytes()).hexdigest() == (
        digest
    )
    assert desktop_entry_digest(path) == digest
    assert remove_user_menu(path, expected_digest=digest) is True
    assert not (dotfiles / "tongs.desktop").exists()


def test_applications_folder_owned_by_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "other-user" / "applications"
    target.mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    (data / "applications").symlink_to(target, target_is_directory=True)
    path = data / "applications" / "tongs.desktop"
    real_uid = os.geteuid()
    monkeypatch.setattr(menu_module.os, "geteuid", lambda: real_uid + 1)

    with pytest.raises(InstallerError) as installed:
        install_user_menu(path, Path("/opt/one/bin/tongs"), replace_digest=None)
    with pytest.raises(InstallerError) as removed:
        remove_user_menu(path, expected_digest="0" * 64)

    for raised in (installed, removed):
        assert raised.value.code is InstallerErrorCode.STATE_CONFLICT
        assert str(raised.value) == (
            "The per-user applications directory has unsafe ownership."
        )
    assert tuple(target.iterdir()) == ()


@pytest.mark.parametrize("kind", ["dangling", "file"])
def test_applications_link_to_a_missing_folder_or_a_file_is_refused(
    tmp_path: Path, kind: str
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    target = tmp_path / "elsewhere"
    if kind == "file":
        target.write_text("not a folder")
    (data / "applications").symlink_to(target)
    path = data / "applications" / "tongs.desktop"

    with pytest.raises(InstallerError) as raised:
        install_user_menu(path, Path("/opt/one/bin/tongs"), replace_digest=None)

    assert raised.value.code is InstallerErrorCode.STATE_CONFLICT
    assert raised.value.retryable is (kind == "dangling")
    assert (data / "applications").is_symlink()
    assert not target.is_dir()


def test_symlinked_entry_file_is_still_refused(tmp_path: Path) -> None:
    applications = tmp_path / "applications"
    applications.mkdir()
    victim = tmp_path / "victim.desktop"
    victim.write_text("[Desktop Entry]\nName=Victim\n")
    path = applications / "tongs.desktop"
    path.symlink_to(victim)

    with pytest.raises(InstallerError) as raised:
        install_user_menu(path, Path("/opt/one/bin/tongs"), replace_digest=None)

    assert str(raised.value) == "The per-user Tongs menu entry is unsafe."
    assert victim.read_text() == "[Desktop Entry]\nName=Victim\n"
    assert path.is_symlink()
