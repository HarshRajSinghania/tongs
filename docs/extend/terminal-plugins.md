---
title: Terminal plugins
description: "Add commands and lifecycle hooks to the tongs terminal app with a Python package."
---

A terminal plugin adds commands and lifecycle hooks to the tongs terminal app.
tongs finds plugins through Python entry points, so each one ships as an
ordinary installable package. To add a surface to the desktop app (beta), see
[Desktop plugin providers](/extend/desktop-providers/).

## What plugins can do

A plugin can contribute any combination of:

- **Commands**: entries in the command palette (++ctrl+p++).
- **Lifecycle hooks**: async callbacks that run when the app starts
  (`on_app_ready`) or shuts down (`on_app_shutdown`).
- **Screens**: Textual `Screen` classes that a command pushes onto the
  screen stack.

Each lifecycle hook receives a `PluginContext`. It is the supported interface
to forge clients, configuration, the cache and notifications.

:::caution[Plugins are trusted code]
A terminal plugin is installed Python code that runs with your permissions.
`PluginContext` narrows the supported API, but it is not a sandbox. Install a
plugin only when you trust its publisher.
:::

## Writing a plugin

Every plugin subclasses `TongsPlugin` and implements at least the `name`
property. tongs skips an entry point whose class is not a `TongsPlugin`.

### The TongsPlugin base class

This is an abridged view of `tongs.plugins.base.TongsPlugin`:

```python
class TongsPlugin(ABC):
    """Base class: the methods you can override."""

    @property
    def name(self) -> str:
        """Unique plugin identifier (required)."""
        ...

    @property
    def version(self) -> str:
        """Semver string. Defaults to '0.0.0'."""
        ...

    async def on_app_ready(self, ctx: PluginContext) -> None:
        """Called after the TUI app is mounted."""

    async def on_app_shutdown(self, ctx: PluginContext) -> None:
        """Called before app exit."""

    def get_commands(self) -> list[tuple[str, str, object]]:
        """Return (display, help_text, callback) tuples."""
        return []

    def get_screens(self) -> dict[str, type]:
        """Return screen_name -> Screen class mappings."""
        return {}
```

### Minimal example

A plugin that adds a single command to the palette:

```python
from tongs.plugins.base import TongsPlugin
from tongs.plugins.context import PluginContext


class HelloPlugin(TongsPlugin):
    def __init__(self) -> None:
        self._ctx: PluginContext | None = None

    @property
    def name(self) -> str:
        return "hello"

    @property
    def version(self) -> str:
        return "0.1.0"

    async def on_app_ready(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def on_app_shutdown(self, ctx: PluginContext) -> None:
        del ctx
        self._ctx = None

    def get_commands(self) -> list[tuple[str, str, object]]:
        return [
            ("Say Hello", "Print a greeting notification", self._greet),
        ]

    def _greet(self) -> None:
        if self._ctx is not None:
            self._ctx.notify("Hello from the plugin!")
```

## Registering via entry points

tongs discovers plugins through the `tongs.plugins` entry point group. Register
your plugin class in your package's `pyproject.toml`:

```toml
[project.entry-points."tongs.plugins"]
hello = "my_tongs_hello.plugin:HelloPlugin"
```

The key (`hello`) is the name tongs uses to find the plugin's configuration.
Keep it the same as the plugin's `name` property. The value is the import path
to the class.

Install the package into the same Python environment as tongs, with
`python -m pip install .` or, during development, `python -m pip install -e .`.
tongs picks it up on the next launch. There is no other registration step.
A plugin installed into a different environment, such as another pipx
environment, is not visible to tongs.

If you installed tongs with pipx, inject the plugin into the tongs environment
with `pipx inject tongs ./my-tongs-hello`, where `./my-tongs-hello` is your
plugin's project directory (add `--editable` during development). With uv, run
`uv tool install tongs --with ./my-tongs-hello` (or `--with-editable`), and
keep any extras you already use, such as `"tongs[mcp]"`.

:::tip
During development, install in editable mode so changes take effect
without reinstalling:

```bash
python -m pip install -e .
```

:::

## Configuration

Plugins are enabled by default. To disable a plugin or pass it plugin-specific
settings, add a `[plugins.<name>]` section to your `config.toml`:

```toml
# Disable the built-in MCP plugin
[plugins.mcp]
enabled = false
```

```toml
# Keep a plugin enabled and pass it custom settings
[plugins.fleet]
enabled = true
monitor_interval = 30
```

The plugin registry reads `enabled` itself and does not import a disabled
plugin. The plugin can read the whole table as a dictionary, so each plugin
defines its own settings.

A plugin with no `[plugins.<name>]` section is loaded with its own defaults.

## What the PluginContext provides

Both `on_app_ready` and `on_app_shutdown` receive a `PluginContext`. It exposes
this part of the application:

| Member | Type | Description |
|-----------|------|-------------|
| `ctx.forge_registry` | `ForgeRegistry` | Creates and reuses the GitHub and GitLab clients. Use it for authenticated API calls to any configured forge. |
| `ctx.cache` | `CacheStore` | Key-value cache with expiry. Read and write it to avoid repeating API calls. |
| `ctx.config` | `Config` | The resolved configuration object. |
| `ctx.repos` | `list[Repo]` | The git repositories found under the scan root. Often empty during `on_app_ready`, because discovery runs in the background after the hook. |
| `ctx.notify(message, severity)` | method | Show a notification in the terminal app. `severity` defaults to `information`. |
| `ctx.push_screen(screen)` | method | Push a screen instance onto the screen stack. |
| `ctx.pop_screen()` | method | Pop the current screen. |
| `ctx.plugin_config(name)` | method | Return a copy of the `[plugins.<name>]` table as a dictionary, including `enabled` when it is set. |

### Accessing your plugin config

```python
async def on_app_ready(self, ctx) -> None:
    my_conf = ctx.plugin_config(self.name)
    interval = my_conf.get("monitor_interval", 60)
    # use interval...
```

## Built-in plugins

tongs ships one built-in plugin, registered through the same entry points.

### MCP server (`mcp`)

The `mcp` plugin adds an **MCP Server Setup** command to the palette when the
optional MCP dependency is installed (`pip install "tongs[mcp]"`). Without
that extra, it adds no command. The command shows how to register `tongs-mcp`
with an MCP client and does not start a server, because MCP clients start it
themselves; see [MCP server](/extend/mcp/) for setup and the tools it offers.

tongs registers it in its own `pyproject.toml`:

```toml
[project.entry-points."tongs.plugins"]
mcp = "tongs.mcp.plugin:MCPPlugin"
```

Disable it with:

```toml
[plugins.mcp]
enabled = false
```

## Complete example: a "Quick Stats" plugin

This walkthrough builds a plugin that adds one palette command. The command
shows a notification with the number of discovered repositories and the number
of forge hosts they live on.

### 1. Create the package

```text
tongs-stats-plugin/
    pyproject.toml
    src/
        tongs_stats/
            __init__.py
            plugin.py
```

### 2. Write the plugin

```python title="src/tongs_stats/plugin.py"
"""Quick Stats plugin for tongs."""

from __future__ import annotations

from tongs.plugins.base import TongsPlugin
from tongs.plugins.context import PluginContext


class StatsPlugin(TongsPlugin):
    """Shows a quick summary of discovered repos."""

    @property
    def name(self) -> str:
        return "stats"

    @property
    def version(self) -> str:
        return "1.0.0"

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None

    async def on_app_ready(self, ctx: PluginContext) -> None:
        """Keep the context for the command callback."""
        self._ctx = ctx

    def get_commands(self) -> list[tuple[str, str, object]]:
        return [
            ("Quick Stats", "Show repo and host counts", self._show_stats),
        ]

    def _show_stats(self) -> None:
        if self._ctx is None:
            return
        repos = self._ctx.repos
        hosts = {repo.hostname for repo in repos if repo.hostname}
        self._ctx.notify(f"Tracking {len(repos)} repos on {len(hosts)} forge hosts")
```

### 3. Configure pyproject.toml

```toml title="pyproject.toml"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "tongs-stats-plugin"
version = "1.0.0"
requires-python = ">=3.12"
dependencies = ["tongs"]

[project.entry-points."tongs.plugins"]
stats = "tongs_stats.plugin:StatsPlugin"

[tool.hatch.build.targets.wheel]
packages = ["src/tongs_stats"]
```

### 4. Install and test

```bash
cd tongs-stats-plugin
python -m pip install -e .
tongs
```

Open the command palette with ++ctrl+p++ and search for **Quick Stats**. The
notification shows the number of repositories and forge hosts, for example
"Tracking 12 repos on 2 forge hosts". Wait for the repository scan to finish
first, or the counts are zero.

### 5. Optional: add a screen

To go further, open a full Textual screen from a command. Build the screen
instance and push it through the context:

```python
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static


class StatsScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Static("Stats screen placeholder")


class StatsPlugin(TongsPlugin):
    # ...existing code...

    def get_commands(self) -> list[tuple[str, str, object]]:
        return [
            ("Quick Stats", "Show repo and host counts", self._show_stats),
            ("Stats Screen", "Open the stats screen", self._open_screen),
        ]

    def _open_screen(self) -> None:
        if self._ctx is not None:
            self._ctx.push_screen(StatsScreen())
```

Key bindings, such as ++escape++ to close the screen, belong to the screen
class, as in any Textual app.

`get_screens()` returns a name-to-class mapping, and the plugin registry
collects it, but the terminal app does not yet install those names. Pushing a
plugin screen by name, as in `app.push_screen("stats")`, fails. Push an
instance with `ctx.push_screen()` instead.

:::note
Plugin errors are logged and do not stop the app. If a plugin fails to load,
or raises in a lifecycle hook, `get_commands` or `get_screens`, tongs logs a
warning and carries on. If a palette command callback raises, tongs also shows
an error notice.
:::
