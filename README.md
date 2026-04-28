# Claude / Codex History Tools

Local history viewers, export tools, web-share bundles, and migration helpers for:

- `claude-history`
- `codex-history`

This repo adds interactive TUI history browsers for Claude Code and Codex, export and web-share workflows, and Claude session bundle migration tooling for moving resumable sessions across machines.

## Quick Start

Run directly from the repo:

```bash
./claude-history
./codex-history
```

Install for all users:

```bash
sudo ./deploy-claude-history.sh
```

After installation:

```bash
claude-history
codex-history
```

If you want public temporary sharing from the TUI, install `cloudflared` first.

## What This Repo Provides

- Arrow-key TUI history viewer for Claude Code sessions
- Arrow-key TUI history viewer for Codex sessions
- Quick export of a session as:
  - `json`
  - `md`
  - `html`
- Web bundle export with a portable `index.html`
- Local web serving and optional temporary Cloudflare tunnel sharing
- Active web share inspection and stop controls from the TUI
- CLI view for active web shares
- Claude session bundle importer for restoring resumable sessions on another host
- Self-contained Claude bundle packaging with:
  - `session.json`
  - `transcript.jsonl`
  - `README.md`
  - `import_claude_bundle.py`
  - `index.html`
  - `package.zip`

## Supported Workflows

- inspect local Claude and Codex session history in a terminal UI
- export a selected conversation as JSON, Markdown, or HTML
- package a session as a portable web bundle
- serve a bundle locally for browser-based review
- open a temporary public URL for a selected bundle through Cloudflare Tunnel
- inspect and stop active shares from the TUI
- migrate Claude session bundles onto another host for `claude --resume`

## Platform Support

Current target platforms:

- Linux
- macOS 10.14+

Important runtime requirement:

- Python `3.10+`

This is required because the viewer code uses modern Python syntax and typing features that older Python versions cannot parse.

## Requirements

Required:

- `python3` version `3.10+`
- `bash`
- an installed local CLI:
  - `claude` for `claude-history`
  - `codex` for `codex-history`

Optional:

- `cloudflared` for temporary public tunnel mode

Deployment-time checks now also report whether these CLIs are present in `PATH`:

- `claude`
- `codex`
- `cloudflared`

The deploy script does not require `claude` or `codex` to be installed, because some machines may only need one viewer, but it will warn clearly when either runtime command is missing.

## Capability Matrix

| Capability | Claude | Codex |
| --- | --- | --- |
| TUI history browsing | Yes | Yes |
| Resume command selection | Yes | Yes |
| Start-mode selection | No | Yes |
| JSON / Markdown / HTML export | Yes | Yes |
| Web bundle export | Yes | Yes |
| Local serve / Cloudflare tunnel | Yes | Yes |
| Active share inspection in TUI | Yes | Yes |
| Session migration bundle import | Yes | No |

## Install

### System Install For All Users

Use the deploy script:

```bash
sudo ./deploy-claude-history.sh
```

What the deploy script validates:

- `bash`
- `python3` `3.10+`
- required repo files
- launcher syntax and Python module compilation
- whether `claude`, `codex`, and `cloudflared` are available in `PATH`

If `cloudflared` is missing, the installer now explains that only `tunnel` mode is affected and, in an interactive terminal, asks whether installation should continue without tunnel support.

The deploy script installs:

- shared Python modules
- the two wrapper scripts
- launcher commands into a system `bin` directory

### macOS Install Notes

Recommended prerequisites on macOS:

```bash
brew install python
brew install cloudflared
```

Then install:

```bash
sudo ./deploy-claude-history.sh
```

Default macOS install locations:

- application files:
  - `/usr/local/share/claude-history`
- launcher commands:
  - `/usr/local/bin`

If your shell cannot find the commands afterward, ensure `/usr/local/bin` is in `PATH`.

### Linux Install Notes

Install Python 3.10+ first if your distribution does not already provide it.

Then:

```bash
sudo ./deploy-claude-history.sh
```

Default Linux install locations:

- application files:
  - `/opt/claude-history`
- launcher commands:
  - `/usr/local/bin`

## Common Commands

```bash
claude-history
codex-history
claude-history --active-shares
codex-history --active-shares
claude-history --export md --session-id <id>
codex-history --export html --session-id <id>
claude-history --import-bundle /path/to/package.zip --import-cwd /absolute/path/to/project
```

## Transfer Service

For an internal browser-first transfer flow, this repo now includes a unified service:

```bash
./transfer-service --tunnel
```

What it does:

- starts a localhost HTTP app
- optionally exposes it through a temporary Cloudflare tunnel
- generates a one-time tokenized URL
- shows a one-liner the source user can run in `bash`
- downloads a temporary helper to the source machine
- lets that source user pick a local Claude or Codex session in the terminal
- uploads the selected session bundle back to the service
- updates the web UI with a preview
- optionally accepts file or directory uploads from the browser
- checks whether the uploaded session ID already exists on the destination host
- lets the operator choose whether to overwrite that destination session or create a new session ID
- proposes a safe destination workspace path under the host user's home directory and lets the operator change it
- prepares a staged workspace on the server and bundle extraction area

High-level operator flow:

1. Start `./transfer-service --tunnel` on the receiving host.
2. Open the printed URL in a browser.
3. Copy the one-liner from the page and run it on the source machine.
4. Pick the session to upload in the helper terminal prompt.
5. Review the uploaded session in the browser.
6. Optionally upload files or a folder from the browser.
7. If the destination host already has the same session ID, choose `overwrite` or `create a new session ID`.
8. Review the proposed destination workspace path under the host user's home directory and change it if needed.
9. Click `Prepare In Background`.
8. Click `Close Session` when done.

Notes:

- the one-liner requires `python3` and `curl` on the source machine
- the helper reads the session-selection prompt from `/dev/tty`, so the `curl ... | bash` flow works in interactive Linux and macOS shells
- uploaded bundles and staged files are stored under a temporary server session directory
- destination workspace paths must stay inside the receiving host user's home directory
- `Prepare In Background` currently stages the extracted bundle and synced files on the receiving host; it does not yet perform a full Codex import because this repo still has no Codex session-import path

### Transfer Service File Upload Support

Current browser upload behavior:

- multiple files are supported
- whole-directory upload is supported when the browser exposes relative paths
- relative paths are preserved into the staged workspace
- per-file limit: `50 MB`
- total request limit: `250 MB`

Accepted file categories:

- source code and scripts:
  - `.py`, `.js`, `.ts`, `.tsx`, `.go`, `.rs`, `.java`, `.php`, `.rb`, `.swift`, `.kt`, `.c`, `.cc`, `.cpp`, `.h`, `.hpp`, `.m`, `.lua`, `.sh`, `.vue`
- web and styling:
  - `.html`, `.css`, `.scss`, `.svg`
- config and structured data:
  - `.json`, `.jsonl`, `.yaml`, `.yml`, `.toml`, `.ini`, `.xml`, `.sql`, `.csv`
- docs and text:
  - `.md`, `.txt`, `.log`, `.patch`, `.pdf`
- images:
  - `.png`, `.jpg`, `.jpeg`, `.svg`
- common project filenames:
  - `Dockerfile`, `Makefile`, `Gemfile`, `requirements.txt`, `.env`, `.env.example`, `.gitignore`, `.npmrc`, `.prettierrc`, `.tool-versions`

Files outside that allowlist are rejected by the service and shown in the UI as rejected uploads.

### Transfer Service macOS Notes

- the helper path works on macOS as long as `python3` and `curl` are available
- for folder upload from the browser, Chromium-based browsers are the most reliable option
- Safari may be less consistent for directory upload because browser support for relative-path directory selection is weaker

## Out-Of-The-Box Usage Manual

### Claude History Viewer

Start it:

```bash
claude-history
```

What you can do in the TUI:

- browse sessions with arrows or `j` / `k`
- press `Enter` to return a resume command
- press `c` to copy a plain resume command
- press `/` to filter
- press `e` to export the selected session
- press `w` to start, stop, or restart web sharing for the selected session
- press `Tab` to switch between:
  - `Sessions`
  - `Active Shares`

In the `Active Shares` tab:

- `x` stops the selected share
- `r` refreshes the live share list

### Codex History Viewer

Start it:

```bash
codex-history
```

Differences from Claude:

- `Enter` prompts for a start mode
- supported modes:
  - `resume`
  - `resume-auto`
  - `resume-danger`
  - `fork`
  - `fork-auto`
  - `fork-danger`

### Export A Session

Fast export inside the TUI:

- `J` for JSON
- `M` for Markdown
- `H` for HTML

Non-interactive export examples:

```bash
claude-history --export json --session-id <id>
claude-history --export md --session-id <id>
claude-history --export html --session-id <id>

codex-history --export json --session-id <id>
```

### View Active Web Shares In The CLI

Claude:

```bash
claude-history --active-shares
```

Codex:

```bash
codex-history --active-shares
```

This prints all active shares in a read-only text view and waits for `Enter` when run in a terminal.

### Web Share A Session

From the TUI:

- select a session
- press `w`
- choose:
  - `bundle`
  - `serve`
  - `tunnel`

Mode summary:

- `bundle`
  - writes a portable package only
- `serve`
  - starts a local HTTP server for the selected bundle
- `tunnel`
  - starts a local HTTP server and a temporary public Cloudflare tunnel

When using the web viewer:

- download the whole package:
  - `package.zip`
- or download individual files:
  - `session.json`
  - `transcript.jsonl`
  - `README.md`
  - `import_claude_bundle.py` for Claude bundles

### Claude Session Bundle Migration

The Claude bundle importer can restore a resumable session into local Claude state.

Bundle import:

```bash
claude-history --import-bundle /path/to/session.json
claude-history --import-bundle /path/to/package.zip
claude-history --import-bundle /path/to/bundle-directory
```

Recommended import on a new machine:

```bash
claude-history \
  --import-bundle /path/to/package.zip \
  --import-cwd /absolute/path/to/local/project
```

Dry-run first:

```bash
claude-history \
  --import-bundle /path/to/package.zip \
  --import-cwd /absolute/path/to/local/project \
  --import-dry-run
```

After import:

```bash
cd /absolute/path/to/local/project
claude --resume <session-id>
```

Important:

- Claude resume is project-directory scoped in practice
- if the imported original path does not exist on the destination host, use `--import-cwd`
- Claude must already be installed and authenticated on the destination host

## Current Limitations

- Claude resume portability is not automatic across hosts; the session path and project cwd still matter
- Codex sessions can be viewed and exported here, but there is no Codex session-import equivalent in this repo
- tunnel mode depends on a working external `cloudflared` binary and network access
- exported bundles are intentionally portable, which means they can contain sensitive transcript data

## macOS-Specific Behavior

### What Needed To Be Adjusted

macOS support required these implementation choices:

- explicit Python `3.10+` requirement
- platform-aware deployment defaults
- removal of hard-coded Linux temp path assumptions in migration and bundle code
- clearer import guidance for Linux-to-macOS session migration

### What Works On macOS

- local TUI history viewing
- JSON / Markdown / HTML export
- web bundle generation
- local `serve` mode
- `tunnel` mode with `cloudflared`
- active share inspection
- Claude bundle migration with `--import-cwd`

## Security

These tools handle real session transcripts. Treat exports and bundles as sensitive artifacts.

### Important Security Facts

- bundles may contain:
  - prompts
  - assistant responses
  - tool inputs and outputs
  - file excerpts
  - local command output
- migration bundles modify Claude state under:
  - `~/.claude/projects/...`
  - `~/.claude/history.jsonl`
- web-share mode is for convenience, not for publishing sensitive transcripts broadly

### Current Safety Measures

- zip extraction is hardened against path traversal
- import supports `--import-dry-run`
- transcript and history writes are atomic
- imported destination cwd must be absolute
- existing transcript overwrite requires explicit `--import-force`

### Safe Operating Guidance

- only import bundles from trusted sources
- prefer `--import-dry-run` before a real import
- use `--import-cwd` when moving sessions across machines or platforms
- do not share bundle URLs publicly unless you are comfortable disclosing transcript contents
- do not treat `package.zip` as a safe public artifact by default

## Repo Layout

- [claude_history_viewer.py](/home/aoi/Workspaces/claude-history/claude_history_viewer.py)
- [codex_history_viewer.py](/home/aoi/Workspaces/claude-history/codex_history_viewer.py)
- [export_utils.py](/home/aoi/Workspaces/claude-history/export_utils.py)
- [claude-history](/home/aoi/Workspaces/claude-history/claude-history)
- [codex-history](/home/aoi/Workspaces/claude-history/codex-history)
- [deploy-claude-history.sh](/home/aoi/Workspaces/claude-history/deploy-claude-history.sh)

## Related Notes

- [CLAUDE_RESUME_OBSERVATIONS.md](/home/aoi/Workspaces/claude-history/CLAUDE_RESUME_OBSERVATIONS.md)
- [TUNNEL_FAILSAFE_NOTES.md](/home/aoi/Workspaces/claude-history/TUNNEL_FAILSAFE_NOTES.md)
- [RELEASE_NOTES_2026-04-18.md](/home/aoi/Workspaces/claude-history/RELEASE_NOTES_2026-04-18.md)
