---
title: MCP server
description: "Run tongs-mcp so an agent can read your reviews, post a comment and approve."
lead: "tongs-mcp is a Model Context Protocol server. It gives an AI assistant six tools for reviews on GitHub and GitLab, using the same forge configuration and credentials as the terminal app."
---

The server speaks MCP over stdio. Your MCP client starts it as a subprocess
and talks to it through standard input and output, so there is no port to open
and no daemon to keep running. It works with any MCP client, including Codex,
Claude Code and Claude Desktop.

## Install

The server needs the optional `mcp` extra:

```bash
pipx install "tongs[mcp]"
```

If tongs is already installed with pipx, reinstall it with the extra:
`pipx install --force "tongs[mcp]"`. `--force` keeps only the extras you name,
so include any you already use, for example `"tongs[mcp,keyring]"`. With uv,
run `uv tool install "tongs[mcp]"`, adding `--reinstall` if tongs is already
installed.

With plain pip, run `pip install "tongs[mcp]"` in the environment you use for
tongs. Every install puts a `tongs-mcp` command next to `tongs`, but it only
runs when the extra is installed.

On Fedora, the `python3-tongs+mcp` RPM attached to each GitHub Release
provides `/usr/bin/tongs-mcp`. Install it together with `python3-tongs` using
`dnf install ./*.rpm`.

Sign in to your forges first, as described in
[Install and sign in](/getting-started/). The server resolves a token for each
host the same way the terminal app does: the `gh` or `glab` credential store,
then a `~/.netrc` entry whose `machine` names the host, then the system
keyring. When a forge call fails, the tool returns a fixed message such as
"Forge authentication is unavailable." and never the underlying error text;
see [Errors](#errors).

## Connect a client

You do not run `tongs-mcp` by hand. Register it with your client, and the
client launches it when it needs a tool. The `tongs-mcp` executable, and the
`gh` or `glab` CLI if you sign in through them, must be available in the
environment where the client runs.

### Claude Code

```bash
claude mcp add tongs -- tongs-mcp
claude mcp list
```

Everything after the standalone `--` is the command Claude Code launches. Use
an absolute path when `tongs-mcp` is not on `PATH`:

```bash
claude mcp add tongs -- "$HOME/.local/bin/tongs-mcp"
```

`claude mcp add` uses the `local` scope by default, which applies to the
current project only. Add `--scope user` to make the server available in all
your projects, or `--scope project` to write a shared `.mcp.json` into the
repository.

Claude Desktop is a separate application. It reads its servers from its own
`claude_desktop_config.json`.

### Codex

```bash
codex mcp add tongs -- tongs-mcp
codex mcp list
```

You can also add the server to `~/.codex/config.toml`. Use the absolute path
to the executable:

```toml title="~/.codex/config.toml"
[mcp_servers.tongs]
command = "/home/you/.local/bin/tongs-mcp"
```

The [Codex MCP configuration guide](https://developers.openai.com/codex/mcp)
lists the other options.

### From a source checkout

Install the extra into the checkout's virtual environment and register the
executable by its absolute path:

```bash
source .venv/bin/activate
python -m pip install -e ".[dev,mcp]"
claude mcp add tongs -- "$PWD/.venv/bin/tongs-mcp"
```

The same path works with `codex mcp add`.

## Tools

Every tool takes a `repo_path` in `hostname/owner/repo` form, for example
`github.com/acme/app`. GitLab subgroups add segments:
`gitlab.com/acme/platform/api`. `number` is the pull request or merge request
number. The repository does not need a local clone: any repository on a
configured host works.

| Tool | Arguments | What it returns |
| --- | --- | --- |
| `list_mrs` | `repo_path`, `state`, `limit` | Up to `limit` reviews in the repository, most recently updated first, filtered by `state`: `open` (the default), `closed` or `merged`. Each entry has the number, title, author, source and target branch, CI status and web URL. |
| `get_mr` | `repo_path`, `number` | One review in detail: description, state, draft flag, conflicts, additions and deletions, approvals, reviewers, labels, CI status, and the `head_sha` and `base_sha` of the revision you read. |
| `get_mr_diff` | `repo_path`, `number` | The changes as unified diff text, with `---` and `+++` headers for every changed file. |
| `list_pipelines` | `repo_path`, `number` | The review's 20 most recent pipelines or workflow runs: ID, status, ref, short SHA, source, duration in seconds and web URL. |
| `post_comment` | `repo_path`, `number`, `body` | Posts `body` as a general comment on the review. Markdown is supported, up to 64 KiB. |
| `approve_mr` | `repo_path`, `number`, `head_sha` | Approves the review at `head_sha`, the head commit that `get_mr` returned. |

Ask for what you want in plain language, and the assistant picks the tools.
For example: "Use tongs to list open PRs for `github.com/acme/app`", or
"Summarize the diff of merge request 42 in `gitlab.com/acme/api`".

### Listing reviews

`list_mrs` reads one page from the forge and never walks every page.
`limit` is 30 by default and accepts 1 to 100. Any `state` other than `open`,
`closed` or `merged` is rejected before a request is made. GitHub has no merged
filter, so `merged` reads one page of closed pull requests and keeps the
merged ones; it can return fewer than `limit`. On GitHub, `closed` includes
merged pull requests.

CI status is looked up for open reviews only, one lookup per returned review,
with the same per-host request limit as the terminal app. Closed and merged
reviews report CI as `unknown`. A lookup that fails, or a host that reported
a rate limit, leaves that review's CI as `unknown` instead of failing the list.

### Reading diffs

`get_mr_diff` lists every changed file, in the order the forge returns them.
A file the forge sends without a patch gets a marker line under its headers:

| Marker | Meaning |
| --- | --- |
| `[binary]` | Binary content, which a text diff cannot show. |
| `[renamed]` | Renamed or copied with no content change. |
| `[mode changed]` | Only the file mode changed. |
| `[empty]` | An added or deleted file with no content. |
| `[truncated]` | The forge sent only part of the patch, or none, because it is too large. |
| `[no patch]` | The forge sent no patch and no reason. |

The output is capped at 256 KiB. When the cap is reached, the file being
written is cut at a line boundary and marked `[truncated]`, and the output
ends with a note such as
`[diff stopped at the 256 KiB cap: 12 of 40 files left out]`. When the forge
itself capped the changed-file list (GitHub lists at most 3,000 files), the
output ends with an `[overflow]` note.

The diff is read the same way as in the terminal and desktop apps: the
review is read before and after the changes, and the tool fails with "The
review changed while its diff was loading. Fetch it again." if new commits
arrived in between.

### Approving

`approve_mr` requires `head_sha`. Call `get_mr` first, review the changes,
and pass the `head_sha` it returned. The approval goes through the same
review service as the terminal and desktop apps, bound to that revision, and
the forge receives the head SHA, so an approval never lands on commits pushed
after you read the review. The tool fails, and approves nothing, when:

- `head_sha` is missing: "head_sha is required. Call get_mr first and pass
  the head_sha it returns."
- `head_sha` is not a full commit SHA (40 or 64 hexadecimal characters).
- The review has new commits since `get_mr`: "The review has new commits
  since get_mr. Call get_mr again, check the changes, and approve with the
  new head_sha."

:::caution[Breaking change for approve_mr callers]
Earlier versions approved without a head SHA. Clients and scripts that call
`approve_mr` with only `repo_path` and `number` now get an error and must call
`get_mr` first and pass its `head_sha`. The other tools keep their names and
arguments; `limit` on `list_mrs` is a new optional argument.
:::

### Errors

A failed call returns an MCP tool error whose text the client shows. Input
problems name the argument, for example "limit must be an integer from 1 to
100." Forge and service failures read `<tool> failed: <message>`, where the
message is the same fixed text the terminal and desktop apps show, such as
"Forge authentication is unavailable.", "The forge rate limit was reached."
or "The requested forge resource was not found." The message never includes
tokens, URLs with credentials or response bodies.

When a write times out or the forge does not confirm it, `post_comment` and
`approve_mr` say so instead of reporting success. Check the review before you
try again, because the comment or approval may have been applied.

## Safety

- **The server acts as you.** It uses your own forge token. A comment or an
  approval from an agent appears under your name, exactly as if you had
  posted it.
- **Two tools write.** `post_comment` and `approve_mr` change state on the
  forge. The other four only read. Your MCP client decides whether to ask
  before each tool call; keep confirmation on for these two.
- **Approvals are pinned.** `approve_mr` approves only the head commit the
  caller read with `get_mr`, as described in [Approving](#approving).
- **No destructive actions.** There is no tool to merge, close, reopen or
  cancel anything. This is a deliberate boundary, not a missing feature.
- **General comments only.** The server cannot post inline comments,
  suggested changes or review drafts. Use the terminal app or the desktop app (beta) for
  those.
- **Checked repository paths.** A `repo_path` must have a hostname and at
  least an owner and a repository, may contain only ASCII letters, digits,
  `_`, `.` and `-` in each segment, and may not contain `..`. A GitHub path
  must be exactly `hostname/owner/repo`; a GitLab path may include nested
  groups. Anything else is rejected before a request is made.
- **Configured hosts only.** github.com and gitlab.com work out of the box.
  For a self-hosted forge, add it under `[hosts.*]` in the
  [configuration](/reference/configuration/) file, which the server reads
  when it handles its first tool call. Any other host is rejected before a
  token is looked up or a request is made, even if its name contains
  "github" or "gitlab". This is the same host set the terminal and desktop
  apps use.
- **Bounded requests.** Lists read one page, diffs are capped, and every
  request shares the per-host request limit (`max_parallel`) with the CI
  lookups, so a large repository cannot use up your hourly rate limit in
  one call.
- **Shared services and cache.** The server runs the same application
  services as the terminal and desktop apps for its whole life. Review lists
  and CI statuses can come from the tongs response cache for the configured
  lifetime (`mr_list_ttl`); review detail, diffs and every write go to the
  forge directly. The first call to a repository makes one small list read
  to check that it exists and that you can read it. On its first tool call
  the server also opens the local review drafts store, as the other apps do,
  so if that store cannot be opened every tool call fails with a startup
  error.

## The palette command

When the `mcp` extra is installed, the built-in `mcp` terminal plugin adds an
**MCP Server Setup** command to the command palette (++ctrl+p++). It shows a
notice with the command that registers the server with Claude Code, for
example `claude mcp add tongs -- tongs-mcp`. When `tongs-mcp` on your `PATH`
is not the one from the running install, the notice names the executable by
its absolute path. The command does not start a server: MCP clients start
`tongs-mcp` themselves, as described in [Connect a client](#connect-a-client).

Without the extra, the plugin adds no command. To remove it even when the
extra is installed, disable the plugin in `config.toml`:

```toml title="~/.config/tongs/config.toml"
[plugins.mcp]
enabled = false
```

This only hides the palette command. The `tongs-mcp` executable keeps working.
