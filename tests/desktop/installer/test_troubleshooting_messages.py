"""Keep troubleshooting message quotes synchronized with installer source."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TROUBLESHOOTING = ROOT / "docs/desktop/troubleshooting.md"
INSTALLER_MODULES = (
    ROOT / "src/tongs/desktop/installer/status.py",
    ROOT / "src/tongs/desktop/installer/activation.py",
    ROOT / "src/tongs/desktop/installer/launcher.py",
    ROOT / "src/tongs/desktop/installer/commands.py",
    ROOT / "src/tongs/desktop/installer/download.py",
    ROOT / "src/tongs/desktop/installer/metadata.py",
    ROOT / "src/tongs/desktop/installer/menu.py",
)


def test_troubleshooting_installer_messages_exist_in_source() -> None:
    documentation = TROUBLESHOOTING.read_text(encoding="utf-8")
    source = "\n".join(path.read_text(encoding="utf-8") for path in INSTALLER_MODULES)
    source_text = " ".join(source.split())
    source_text = source_text.replace('" "', "").replace("' '", "")

    messages = [
        " ".join(message.split())
        for message in re.findall(r"`([A-Z][^`]*\.)`", documentation)
    ]

    assert messages, "expected installer messages in troubleshooting documentation"
    missing = [message for message in messages if message not in source_text]
    assert missing == [], (
        f"messages quoted in troubleshooting.md but not found in the installer: {missing}"
    )
