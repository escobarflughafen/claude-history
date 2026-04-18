# Claude Code Resume And History Observations

Date checked: 2026-04-18
Environment checked:
- Claude Code `2.1.114`
- Binary path: `/home/aoi/.local/bin/claude`

## What was verified

### `--resume` exists and is documented

Running `claude --help` shows:
- `-r, --resume [value]`: resume a conversation by session ID, or open an interactive picker with an optional search term
- `-c, --continue`: continue the most recent conversation in the current directory

This matches the observed exit hint pattern such as:

```sh
claude --resume af484d98-4068-4e0d-9b57-0c8909f8f3dc
```

### There is no obvious built-in history viewer command

The top-level CLI help does not list a `history` command or any transcript browser.

Running `claude history --help` did not show a dedicated history subcommand. It fell back to the generic top-level help output, which is consistent with there being no documented built-in history viewer.

### Claude does persist session history locally

Local files observed:
- Global history index: `/home/aoi/.claude/history.jsonl`
- Per-project session transcripts: `/home/aoi/.claude/projects/.../*.jsonl`

Example transcript for the sample resume ID:
- `/home/aoi/.claude/projects/-home-aoi-Workspaces-claude-history/af484d98-4068-4e0d-9b57-0c8909f8f3dc.jsonl`

### The working directory is stored, but not surfaced clearly in the resume hint

The global history index contains entries like:

```json
{"display":"this is a test session for implementing claude session history viewer","project":"/home/aoi/Workspaces/claude-history","sessionId":"af484d98-4068-4e0d-9b57-0c8909f8f3dc"}
```

The session transcript also stores `cwd` on message entries. For the sample session, entries included:

```json
"cwd":"/home/aoi/Workspaces/claude-history"
```

So the session working directory is persisted locally in at least two places:
- `project` in `~/.claude/history.jsonl`
- `cwd` in the per-session transcript JSONL

However, the normal resume hint shown to the user appears to expose only the session ID and not the working directory.

## Conclusion

The original claim is substantially correct:
- Claude Code does provide `--resume`
- Claude Code does not appear to provide a built-in history viewer in the CLI
- Claude Code does store enough local data to reconstruct session history and session working directories
- The user is not clearly told where the resumable session lived, even though that information exists on disk

## Related observation

An optional installed plugin exists at:

`/home/aoi/.claude/plugins/marketplaces/claude-plugins-official/plugins/session-report/skills/session-report/SKILL.md`

That plugin is for generating HTML usage reports from saved transcripts. It is not a built-in interactive session history browser.
