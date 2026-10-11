"""The terminal's external editor: one lookup and one temp-file round trip.

The comment editor (F2), the pipeline log viewer (F2) and the suggestion
editor (F3) all start the editor through this module, so they honour the same
``[editor]`` settings.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress

from textual.app import App

from tongs.config import Config

FALLBACK_EDITORS: tuple[str, ...] = ("nvim", "vim", "vi", "nano")
"""Editors tried on ``PATH``, in order, when nothing else names one."""

EDITOR_DISABLED_MESSAGE = (
    "The external editor is turned off: [editor] external_editor_enabled = false."
)
NO_EDITOR_MESSAGE = (
    "No external editor found. Set [editor] command in config.toml, "
    "or $VISUAL or $EDITOR."
)
BAD_COMMAND_MESSAGE = "The [editor] command cannot be read as a command line."
WINDOWS_MESSAGE = "The external editor is not supported on Windows."


class EditorUnavailableError(Exception):
    """No editor can be started; ``message`` says why, for a notification."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def find_editor(
    config: Config,
    *,
    fallbacks: tuple[str, ...] = FALLBACK_EDITORS,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return the editor command line to start, or None when there is none.

    ``[editor] command`` comes first, then ``$VISUAL``, then ``$EDITOR``, then
    the first of ``fallbacks`` found on ``PATH``.
    """
    command = config.editor_command.strip()
    if command:
        return command
    env = os.environ if environ is None else environ
    for name in ("VISUAL", "EDITOR"):
        value = env.get(name, "").strip()
        if value:
            return value
    for name in fallbacks:
        if shutil.which(name):
            return name
    return None


def editor_argv(
    config: Config, *, fallbacks: tuple[str, ...] = FALLBACK_EDITORS
) -> list[str]:
    """Return the editor's argument list, or raise ``EditorUnavailableError``."""
    if not config.external_editor_enabled:
        raise EditorUnavailableError(EDITOR_DISABLED_MESSAGE)
    if platform.system() == "Windows":
        raise EditorUnavailableError(WINDOWS_MESSAGE)
    command = find_editor(config, fallbacks=fallbacks)
    if command is None:
        raise EditorUnavailableError(NO_EDITOR_MESSAGE)
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = []
    if not argv:
        raise EditorUnavailableError(BAD_COMMAND_MESSAGE)
    return argv


@contextmanager
def _private_file(text: str, *, suffix: str, prefix: str) -> Iterator[str]:
    """Write ``text`` to a file only the user can read, and remove it after."""
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=suffix, prefix=prefix)
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        yield path
    finally:
        if path:
            with suppress(OSError):
                os.unlink(path)


def _run(app: App, argv: list[str], path: str) -> None:
    with app.suspend():
        subprocess.run([*argv, path], check=False)


def edit_text(app: App, argv: list[str], text: str, *, suffix: str, prefix: str) -> str:
    """Open ``text`` in the editor with the app suspended, and return the result."""
    with _private_file(text, suffix=suffix, prefix=prefix) as path:
        _run(app, argv, path)
        with open(path) as handle:
            return handle.read()


def view_text(
    app: App, argv: list[str], text: str, *, suffix: str, prefix: str
) -> None:
    """Open ``text`` in the editor with the app suspended; changes are dropped."""
    with _private_file(text, suffix=suffix, prefix=prefix) as path:
        _run(app, argv, path)
