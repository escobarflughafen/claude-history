# Release Notes

## 2026-04-18

### TUI Share Management

- Added an `Active Shares` tab to both `claude-history` and `codex-history`.
- Web shares can now be started and stopped directly from the TUI instead of forcing a blocking terminal handoff.
- `w` now acts as per-session share control:
  - starts a share when none exists
  - offers `stop` or `restart` when a share already exists
- In the `Active Shares` tab, `x` stops the selected share and `r` refreshes the live share list.

### Share Visibility Improvements

- The selected session now shows current web share state in the TUI header/status area.
- The `Active Shares` tab now surfaces the selected share URL directly in the header.
- The share list now shows the actual target URL instead of only a `local` or `public` marker.
- The active share detail pane now wraps long values instead of truncating them.
- Full values are shown for:
  - local URL
  - public URL
  - bundle directory
  - state file path
  - process identifiers and other share metadata

### Codex Session Start Modes

- Added start-mode selection to `codex-history`.
- `Enter` now prompts for a launch mode before starting a Codex session.
- `c` now prompts for the same mode and returns a copyable command.
- Supported modes:
  - `resume`
  - `resume-auto`
  - `resume-danger`
  - `fork`
  - `fork-auto`
  - `fork-danger`

### New CLI Active Share View

- Added a non-TUI active share viewer for both tools:
  - `claude-history --active-shares`
  - `codex-history --active-shares`
- This prints all active web shares in a read-only text view.
- When run in an interactive terminal, the view ends with `Press Enter to exit...`

### Notes

- Active share tracking continues to use transient state files under `/tmp`.
- Closed or stale shares are cleaned up automatically when refreshed or stopped.
