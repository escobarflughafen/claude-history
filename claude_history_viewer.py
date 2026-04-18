#!/usr/bin/env python3
"""Interactive local Claude Code session viewer.

Reads session metadata from ~/.claude/history.jsonl and per-session transcripts
from ~/.claude/projects. Provides arrow-key navigation and a conversation
overview, then prints a ready-to-run `claude --resume <id>` command when the
user selects a session.
"""

from __future__ import annotations

import argparse
import curses
import datetime as dt
import json
import os
import tempfile
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from export_utils import (
    format_export_ts,
    list_active_shares,
    now_iso,
    serve_bundle,
    stop_active_share,
    write_bundle,
    write_export,
)


DEFAULT_CLAUDE_DIR = Path.home() / ".claude"


@dataclass
class SessionSummary:
    session_id: str
    project: str
    transcript_path: Path | None
    first_ts_ms: int | None
    last_ts_ms: int | None
    first_prompt: str
    last_prompt: str
    cwd: str
    message_count: int
    user_count: int
    assistant_count: int

    @property
    def sort_ts_ms(self) -> int:
        return self.last_ts_ms or self.first_ts_ms or 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Browse local Claude Code session history and print a resume command."
    )
    parser.add_argument(
        "--claude-dir",
        default=str(DEFAULT_CLAUDE_DIR),
        help="Path to the Claude state directory (default: ~/.claude)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum number of sessions to load into the viewer.",
    )
    parser.add_argument(
        "--query",
        default="",
        help="Initial case-insensitive filter applied to project, cwd, prompt, or session ID.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print session summaries as JSON instead of opening the interactive viewer.",
    )
    parser.add_argument(
        "--output-file",
        default="",
        help="When set, write the selected resume command to this file instead of stdout.",
    )
    parser.add_argument(
        "--session-id",
        default="",
        help="Session ID to export in non-interactive mode.",
    )
    parser.add_argument(
        "--export",
        choices=["json", "md", "html"],
        help="Export a session conversation in the selected format.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output path for --export. Defaults to ./claude-session-<id>.<ext>.",
    )
    parser.add_argument(
        "--active-shares",
        action="store_true",
        help="Print all active web shares and wait for Enter before exiting.",
    )
    return parser.parse_args()


def read_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def compact_text(value: object, max_len: int = 200) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            elif isinstance(item, str):
                parts.append(item)
        text = " ".join(parts)
    elif isinstance(value, dict):
        if isinstance(value.get("content"), str):
            text = value["content"]
        elif isinstance(value.get("content"), list):
            text = compact_text(value["content"], max_len=max_len)
        else:
            text = json.dumps(value, ensure_ascii=True)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text[: max_len - 1] + "…" if len(text) > max_len else text


def discover_transcript_map(projects_dir: Path) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    if not projects_dir.exists():
        return mapping
    for path in projects_dir.rglob("*.jsonl"):
        if path.name == "history.jsonl":
            continue
        session_id = path.stem
        if session_id not in mapping:
            mapping[session_id] = path
    return mapping


def build_session_summary(
    session_id: str,
    project: str,
    transcript_path: Path | None,
    prompt_displays: list[str],
) -> SessionSummary:
    first_ts_ms = None
    last_ts_ms = None
    first_prompt = prompt_displays[0] if prompt_displays else ""
    last_prompt = prompt_displays[-1] if prompt_displays else ""
    cwd = project
    message_count = 0
    user_count = 0
    assistant_count = 0

    if transcript_path and transcript_path.exists():
        for entry in read_jsonl(transcript_path):
            timestamp = entry.get("timestamp")
            parsed_ms = parse_timestamp_ms(timestamp)
            if parsed_ms is not None:
                if first_ts_ms is None or parsed_ms < first_ts_ms:
                    first_ts_ms = parsed_ms
                if last_ts_ms is None or parsed_ms > last_ts_ms:
                    last_ts_ms = parsed_ms

            entry_cwd = entry.get("cwd")
            if isinstance(entry_cwd, str) and entry_cwd:
                cwd = entry_cwd

            entry_type = entry.get("type")
            if entry_type == "user":
                user_count += 1
                message_count += 1
                if not entry.get("isMeta"):
                    extracted_prompt = extract_user_prompt(entry)
                    if extracted_prompt:
                        if not first_prompt:
                            first_prompt = extracted_prompt
                        if not prompt_displays:
                            last_prompt = extracted_prompt
            elif entry_type == "assistant":
                assistant_count += 1
                message_count += 1
            elif entry_type in {"summary", "attachment"}:
                message_count += 1

    return SessionSummary(
        session_id=session_id,
        project=project,
        transcript_path=transcript_path,
        first_ts_ms=first_ts_ms,
        last_ts_ms=last_ts_ms,
        first_prompt=first_prompt,
        last_prompt=last_prompt,
        cwd=cwd,
        message_count=message_count,
        user_count=user_count,
        assistant_count=assistant_count,
    )


def parse_timestamp_ms(value: object) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            parsed = dt.datetime.fromisoformat(value)
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return None


def extract_user_prompt(entry: dict) -> str:
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    text = compact_text(content)
    if not text:
        return ""
    if text.startswith("<local-command-caveat>"):
        return ""
    if text.startswith("<local-command-stdout>"):
        return ""
    if "<command-name>" in text and "<command-message>" in text:
        return ""
    return text


def load_sessions(claude_dir: Path, limit: int) -> list[SessionSummary]:
    history_path = claude_dir / "history.jsonl"
    if not history_path.exists():
        raise FileNotFoundError(f"Missing history file: {history_path}")

    transcript_map = discover_transcript_map(claude_dir / "projects")
    by_session: dict[str, dict[str, object]] = {}

    for entry in read_jsonl(history_path):
        session_id = entry.get("sessionId")
        project = entry.get("project")
        if not isinstance(session_id, str) or not isinstance(project, str):
            continue
        bucket = by_session.setdefault(
            session_id,
            {
                "project": project,
                "prompts": [],
                "last_ts_ms": None,
            },
        )
        display = entry.get("display")
        if isinstance(display, str) and display.strip():
            bucket["prompts"].append(display.strip())
        ts = entry.get("timestamp")
        if isinstance(ts, (int, float)):
            bucket["last_ts_ms"] = max(int(ts), int(bucket["last_ts_ms"] or 0))

    summaries: list[SessionSummary] = []
    for session_id, data in by_session.items():
        prompt_displays = list(data.get("prompts", []))
        project = str(data.get("project", ""))
        summary = build_session_summary(
            session_id=session_id,
            project=project,
            transcript_path=transcript_map.get(session_id),
            prompt_displays=prompt_displays,
        )
        if summary.last_ts_ms is None and data.get("last_ts_ms") is not None:
            summary.last_ts_ms = int(data["last_ts_ms"])
        summaries.append(summary)

    summaries.sort(key=lambda item: item.sort_ts_ms, reverse=True)
    return summaries[:limit]


def format_ts(ts_ms: int | None) -> str:
    if not ts_ms:
        return "unknown"
    local = dt.datetime.fromtimestamp(ts_ms / 1000).astimezone()
    return local.strftime("%Y-%m-%d %H:%M")


def relative_age(ts_ms: int | None) -> str:
    if not ts_ms:
        return "unknown"
    now = dt.datetime.now().astimezone()
    then = dt.datetime.fromtimestamp(ts_ms / 1000).astimezone()
    delta = now - then
    if delta.days >= 1:
        return f"{delta.days}d ago"
    hours = delta.seconds // 3600
    if hours >= 1:
        return f"{hours}h ago"
    minutes = delta.seconds // 60
    return f"{minutes}m ago"


def filter_sessions(sessions: list[SessionSummary], query: str) -> list[SessionSummary]:
    if not query:
        return sessions
    needle = query.lower()
    filtered: list[SessionSummary] = []
    for session in sessions:
        haystack = "\n".join(
            [
                session.session_id,
                session.project,
                session.cwd,
                session.first_prompt,
                session.last_prompt,
            ]
        ).lower()
        if needle in haystack:
            filtered.append(session)
    return filtered


def clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    return value[: width - 1] + "…" if len(value) > width else value


def draw_lines(win: curses.window, start_y: int, start_x: int, width: int, lines: list[str], attr: int = 0) -> None:
    y = start_y
    for line in lines:
        if y >= curses.LINES - 1:
            break
        win.addnstr(y, start_x, line, max(width, 0), attr)
        y += 1


def wrap_detail_lines(items: list[tuple[str, str]], width: int) -> list[str]:
    lines: list[str] = []
    key_width = min(14, max(8, width // 4))
    value_width = max(12, width - key_width - 2)
    for key, value in items:
        wrapped = textwrap.wrap(value or "", width=value_width) or [""]
        lines.append(f"{key:<{key_width}} {wrapped[0]}")
        for extra in wrapped[1:]:
            lines.append(f"{'':<{key_width}} {extra}")
    return lines


def format_active_shares_report(tool: str) -> str:
    shares = list_active_shares(tool)
    lines = [f"{tool.capitalize()} Active Web Shares", ""]
    if not shares:
        lines.append("No active web shares.")
        return "\n".join(lines)
    for index, share in enumerate(shares, start=1):
        lines.extend(
            [
                f"{index}. Session: {share.get('session_id', 'unknown')}",
                f"   Mode: {share.get('mode', 'unknown')}",
                f"   Started: {share.get('started_at', 'unknown')}",
                f"   Bundle: {share.get('bundle_dir', 'unknown')}",
                f"   Local URL: {share.get('local_url', 'unknown')}",
                f"   Public URL: {share.get('public_url') or 'not exposed'}",
                f"   Server PID: {share.get('server_pid', 'unknown')}",
                f"   Tunnel PID: {share.get('tunnel_pid', 'none')}",
                f"   Tunnel alive: {'yes' if share.get('tunnel_alive') else 'no'}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def show_active_shares_report(tool: str) -> int:
    report = format_active_shares_report(tool)
    print(report)
    if sys.stdin.isatty() and sys.stdout.isatty():
        print("")
        input("Press Enter to exit...")
    return 0


def format_share_age(started_at: str) -> str:
    if not started_at:
        return "unknown"
    try:
        started = dt.datetime.fromisoformat(started_at)
    except ValueError:
        return started_at
    delta = dt.datetime.now(started.tzinfo or dt.timezone.utc) - started
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def render_tabs(stdscr: curses.window, width: int, active_tab: str) -> None:
    tabs = [
        ("sessions", "Sessions"),
        ("shares", "Active Shares"),
    ]
    x = 0
    for key, label in tabs:
        text = f"[{label}]" if key == active_tab else f" {label} "
        attr = curses.A_BOLD if key == active_tab else curses.A_DIM
        if x < width - 1:
            stdscr.addnstr(2, x, clip(text, width - x - 1), width - x - 1, attr)
        x += len(text) + 1


def find_session_shares(shares: list[dict], session_id: str) -> list[dict]:
    return [share for share in shares if str(share.get("session_id") or "") == session_id]


def summarize_share(share: dict) -> str:
    target = str(share.get("public_url") or share.get("local_url") or "unknown")
    return f"{share.get('mode', 'serve')} {target}"


def draw_share_view(
    stdscr: curses.window,
    shares: list[dict],
    selected: int,
    scroll: int,
    top_y: int,
    list_width: int,
    detail_x: int,
    width: int,
    visible_rows: int,
) -> None:
    if not shares:
        stdscr.addnstr(top_y, 0, clip("No active serve/tunnel sessions detected.", width - 1), width - 1)
        stdscr.addnstr(top_y + 1, 0, clip("Open a web share with `w` from the Sessions tab, then return here.", width - 1), width - 1, curses.A_DIM)
        return

    for idx in range(scroll, min(len(shares), scroll + visible_rows)):
        share = shares[idx]
        row = top_y + idx - scroll
        marker = ">" if idx == selected else " "
        target = str(share.get("public_url") or share.get("local_url") or "unknown")
        line = (
            f"{marker} {format_share_age(str(share.get('started_at') or '')):>8}  "
            f"{clip(str(share.get('mode') or 'serve'), 6):6}  "
            f"{clip(str(share.get('session_id') or ''), 12):12}  "
            f"{clip(target, max(8, list_width - 32))}"
        )
        attr = curses.A_REVERSE if idx == selected else 0
        stdscr.addnstr(row, 0, clip(line, list_width - 1), list_width - 1, attr)

    share = shares[selected]
    detail_width = max(10, width - detail_x - 1)
    tunnel_state = "up" if share.get("tunnel_alive") else "down"
    details = wrap_detail_lines(
        [
            ("Session ID", str(share.get("session_id", "unknown"))),
            ("Mode", str(share.get("mode", "unknown"))),
            (
                "Started",
                f"{share.get('started_at', 'unknown')} ({format_share_age(str(share.get('started_at') or ''))})",
            ),
            ("Bundle", str(share.get("bundle_dir", "unknown"))),
            ("Local URL", str(share.get("local_url", "unknown"))),
            ("Public URL", str(share.get("public_url") or "not exposed")),
            ("Server PID", str(share.get("server_pid", "unknown"))),
            ("Tunnel PID", f"{share.get('tunnel_pid', 'none')} ({tunnel_state})"),
            ("State Path", str(share.get("state_path", "unknown"))),
        ],
        detail_width,
    )
    details.extend(
        [
            "",
            "Notes:",
            "These rows are discovered from live share state files in /tmp.",
            "Keys: x stop selected share, r refresh.",
            "Closed or crashed sessions are removed automatically on refresh.",
        ]
    )
    draw_lines(stdscr, top_y, detail_x, detail_width, details)


def run_tui(stdscr: curses.window, sessions: list[SessionSummary], initial_query: str) -> str | None:
    curses.curs_set(0)
    stdscr.keypad(True)
    query = initial_query
    selected = 0
    scroll = 0
    share_selected = 0
    share_scroll = 0
    active_tab = "sessions"
    status = ""

    while True:
        filtered = filter_sessions(sessions, query)
        if selected >= len(filtered):
            selected = max(0, len(filtered) - 1)
        shares = list_active_shares("claude")
        if share_selected >= len(shares):
            share_selected = max(0, len(shares) - 1)

        height, width = stdscr.getmaxyx()
        list_width = max(40, min(72, width // 2))
        detail_x = list_width + 2
        top_y = 5
        visible_rows = max(5, height - top_y)

        if active_tab == "sessions":
            if selected < scroll:
                scroll = selected
            elif selected >= scroll + visible_rows:
                scroll = selected - visible_rows + 1
        else:
            if share_selected < share_scroll:
                share_scroll = share_selected
            elif share_selected >= share_scroll + visible_rows:
                share_scroll = share_selected - visible_rows + 1

        stdscr.erase()
        stdscr.addnstr(0, 0, clip("Claude History Viewer", width - 1), width - 1, curses.A_BOLD)
        help_text = "Tab switch tab  Up/Down move  Enter resumes  / filter  c command  e export  w share start/stop  J/M/H quick export  q quit"
        stdscr.addnstr(1, 0, clip(help_text, width - 1), width - 1)
        render_tabs(stdscr, width, active_tab)
        if active_tab == "sessions":
            if filtered:
                selected_shares = find_session_shares(shares, filtered[selected].session_id)
                share_text = summarize_share(selected_shares[0]) if selected_shares else "none"
            else:
                share_text = "none"
            info_text = f"Filter: {query or '(none)'}  Share: {share_text}"
        else:
            selected_share = shares[share_selected] if shares else None
            share_text = summarize_share(selected_share) if selected_share else "none"
            info_text = f"Active shares: {len(shares)}  Selected: {share_text}"
        stdscr.addnstr(3, 0, clip(info_text, width - 1), width - 1, curses.A_DIM)
        if status:
            stdscr.addnstr(4, 0, clip(status, width - 1), width - 1, curses.A_DIM)

        if active_tab == "sessions":
            if not filtered:
                stdscr.addnstr(top_y, 0, clip("No sessions matched the current filter.", width - 1), width - 1)
                stdscr.refresh()
                key = stdscr.getch()
                if key in (ord("q"), 27):
                    return None
                if key in (9, curses.KEY_BTAB):
                    active_tab = "shares"
                elif key == ord("/"):
                    query = prompt_input(stdscr, "Filter")
                elif key in (curses.KEY_BACKSPACE, 127):
                    query = query[:-1]
                continue

            for idx in range(scroll, min(len(filtered), scroll + visible_rows)):
                session = filtered[idx]
                row = top_y + idx - scroll
                marker = ">" if idx == selected else " "
                line = f"{marker} {relative_age(session.sort_ts_ms):>8}  {clip(session.project, 28):28}  {clip(session.last_prompt or session.first_prompt, max(8, list_width - 44))}"
                attr = curses.A_REVERSE if idx == selected else 0
                stdscr.addnstr(row, 0, clip(line, list_width - 1), list_width - 1, attr)

            session = filtered[selected]
            detail_width = max(10, width - detail_x - 1)
            details = [
                f"Session ID: {session.session_id}",
                f"Project: {session.project}",
                f"CWD: {session.cwd}",
                f"Last activity: {format_ts(session.sort_ts_ms)} ({relative_age(session.sort_ts_ms)})",
                f"Transcript: {session.transcript_path or 'not found'}",
                f"Counts: messages={session.message_count} user={session.user_count} assistant={session.assistant_count}",
                "",
                "First prompt:",
            ]
            details.extend(textwrap.wrap(session.first_prompt or "(none found)", width=detail_width) or ["(none found)"])
            details.append("")
            details.append("Last prompt:")
            details.extend(textwrap.wrap(session.last_prompt or "(none found)", width=detail_width) or ["(none found)"])
            details.append("")
            active_session_shares = find_session_shares(shares, session.session_id)
            if active_session_shares:
                details.append(f"Web share: {len(active_session_shares)} active")
                for share in active_session_shares[:2]:
                    details.append(f"- {summarize_share(share)}")
                if len(active_session_shares) > 2:
                    details.append(f"- +{len(active_session_shares) - 2} more")
            else:
                details.append("Web share: none")
            details.append("")
            details.append("Resume command:")
            details.append(f"cd {shell_quote(session.cwd)} && claude --resume {session.session_id}")
            draw_lines(stdscr, top_y, detail_x, detail_width, [clip(line, detail_width) for line in details])
        else:
            draw_share_view(
                stdscr,
                shares,
                share_selected,
                share_scroll,
                top_y,
                list_width,
                detail_x,
                width,
                visible_rows,
            )

        stdscr.refresh()
        key = stdscr.getch()

        if key in (ord("q"), 27):
            return None
        if key in (9, curses.KEY_BTAB):
            active_tab = "shares" if active_tab == "sessions" else "sessions"
            status = ""
            continue
        if active_tab == "sessions":
            if key in (curses.KEY_UP, ord("k")):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = min(len(filtered) - 1, selected + 1)
            elif key in (curses.KEY_PPAGE,):
                selected = max(0, selected - visible_rows)
            elif key in (curses.KEY_NPAGE,):
                selected = min(len(filtered) - 1, selected + visible_rows)
            elif key in (10, 13, curses.KEY_ENTER):
                return f"cd {shell_quote(session.cwd)} && claude --resume {session.session_id}"
            elif key == ord("c"):
                return f"claude --resume {session.session_id}"
            elif key == ord("e"):
                status = interactive_export(stdscr, session)
            elif key == ord("w"):
                existing = find_session_shares(shares, session.session_id)
                if existing:
                    action = prompt_choice(stdscr, "Share action [stop|restart|cancel]", {"stop", "restart", "cancel"})
                    if action == "stop":
                        for share in existing:
                            stop_active_share(share)
                        status = f"Stopped {len(existing)} web share(s) for {session.session_id}"
                    elif action == "restart":
                        for share in existing:
                            stop_active_share(share)
                        status = interactive_web_bundle(stdscr, session)
                    else:
                        status = "Share action cancelled."
                else:
                    status = interactive_web_bundle(stdscr, session)
            elif key in (ord("J"), ord("M"), ord("H")):
                export_format = {ord("J"): "json", ord("M"): "md", ord("H"): "html"}[key]
                try:
                    path = export_session(session, export_format)
                    status = f"Exported {export_format} to {path}"
                except OSError as exc:
                    status = f"Export failed: {exc}"
            elif key == ord("/"):
                query = prompt_input(stdscr, "Filter")
                selected = 0
                scroll = 0
            elif key in (curses.KEY_BACKSPACE, 127):
                query = query[:-1]
                selected = 0
                scroll = 0
        else:
            if key in (curses.KEY_UP, ord("k")):
                share_selected = max(0, share_selected - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                share_selected = min(max(0, len(shares) - 1), share_selected + 1)
            elif key in (curses.KEY_PPAGE,):
                share_selected = max(0, share_selected - visible_rows)
            elif key in (curses.KEY_NPAGE,):
                share_selected = min(max(0, len(shares) - 1), share_selected + visible_rows)
            elif key == ord("r"):
                status = f"Refreshed active share list ({len(shares)} found)"
            elif key == ord("x") and shares:
                share = shares[share_selected]
                stop_active_share(share)
                status = f"Stopped share for {share.get('session_id', 'unknown session')}"
            elif key == ord("/"):
                status = "Filtering is only available on the Sessions tab."
            elif key in (10, 13, curses.KEY_ENTER):
                status = "Enter resumes sessions only. Switch to the Sessions tab for resume commands."


def prompt_input(stdscr: curses.window, label: str) -> str:
    curses.curs_set(1)
    height, width = stdscr.getmaxyx()
    prompt = f"{label}: "
    value = ""
    while True:
        stdscr.move(height - 1, 0)
        stdscr.clrtoeol()
        stdscr.addnstr(height - 1, 0, clip(prompt + value, width - 1), width - 1)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (10, 13, curses.KEY_ENTER):
            break
        if key in (27,):
            value = ""
            break
        if key in (curses.KEY_BACKSPACE, 127):
            value = value[:-1]
            continue
        if 32 <= key <= 126:
            value += chr(key)
    curses.curs_set(0)
    return value.strip()


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def interactive_export(stdscr: curses.window, session: SessionSummary) -> str:
    format_value = prompt_input(stdscr, "Export format [json|md|html]")
    if not format_value:
        return "Export cancelled."
    format_value = format_value.lower()
    if format_value not in {"json", "md", "html"}:
        return f"Unsupported export format: {format_value}"
    default_name = f"claude-session-{session.session_id}.{format_value}"
    output_value = prompt_input(stdscr, f"Output path [{default_name}]")
    output_path = output_value or default_name
    try:
        path = export_session(session, format_value, output_path)
    except OSError as exc:
        return f"Export failed: {exc}"
    return f"Exported {format_value} to {path}"


def prompt_choice(stdscr: curses.window, label: str, allowed: set[str]) -> str:
    value = prompt_input(stdscr, label)
    if not value:
        return ""
    value = value.lower()
    return value if value in allowed else ""


def build_claude_export(session: SessionSummary) -> dict:
    raw_entries: list[dict] = []
    messages: list[dict] = []
    metadata = {"project": session.project}
    if session.transcript_path and session.transcript_path.exists():
        for entry in read_jsonl(session.transcript_path):
            raw_entries.append(entry)
            role = "meta"
            kind = "message"
            text = ""
            extra: dict[str, object] = {}
            entry_type = entry.get("type")
            if entry_type == "user":
                role = "meta" if entry.get("isMeta") else "user"
                text = compact_text(entry.get("message", {}).get("content"), max_len=20000)
            elif entry_type == "assistant":
                role = "assistant"
                text = compact_text(entry.get("message", {}).get("content"), max_len=20000)
            elif entry_type == "summary":
                role = "system"
                kind = "summary"
                text = compact_text(entry.get("summary"), max_len=20000)
            elif entry_type == "attachment":
                role = "system"
                kind = "attachment"
                attachment = entry.get("attachment", {})
                if isinstance(attachment, dict) and attachment.get("type"):
                    extra["attachment_type"] = attachment.get("type")
                    attachment_type = str(attachment.get("type"))
                    if attachment_type == "skill_listing":
                        text = "Skill listing attached for session startup."
                    elif attachment_type == "deferred_tools_delta":
                        added = len(attachment.get("addedNames") or [])
                        removed = len(attachment.get("removedNames") or [])
                        text = f"Deferred tools changed: {added} added, {removed} removed."
                    elif attachment_type == "auto_mode":
                        text = "Auto mode metadata attached."
                    else:
                        text = compact_text(attachment.get("content") or attachment, max_len=1200)
                else:
                    text = compact_text(attachment.get("content") or attachment, max_len=1200)
            elif entry_type == "permission-mode":
                role = "system"
                kind = "permission-mode"
                text = f"Permission mode set to {entry.get('permissionMode', 'unknown')}."
            elif entry_type == "last-prompt":
                role = "system"
                kind = "last-prompt"
                text = compact_text(entry.get("lastPrompt"), max_len=20000)
            else:
                role = "meta"
                kind = str(entry_type or "event")
                text = compact_text(entry, max_len=1200)

            messages.append(
                {
                    "timestamp": format_export_ts(parse_timestamp_ms(entry.get("timestamp"))),
                    "role": role,
                    "kind": kind,
                    "raw_type": entry_type,
                    "text": text,
                    "extra": extra or None,
                }
            )

    bundle = {
        "schema_version": 1,
        "tool": "claude",
        "exported_at": now_iso(),
        "session": {
            "session_id": session.session_id,
            "project": session.project,
            "cwd": session.cwd,
            "started_at": format_export_ts(session.first_ts_ms),
            "last_activity": format_export_ts(session.last_ts_ms),
            "transcript_path": str(session.transcript_path) if session.transcript_path else None,
            "first_prompt": session.first_prompt,
            "last_prompt": session.last_prompt,
            "message_count": session.message_count,
            "user_count": session.user_count,
            "assistant_count": session.assistant_count,
        },
        "metadata": metadata,
        "messages": messages,
        "raw_entries": raw_entries,
    }
    bundle["analytics"] = build_claude_analytics(bundle)
    return bundle


def build_claude_analytics(bundle: dict) -> dict:
    role_counts: dict[str, int] = {}
    tool_counts: dict[str, int] = {}
    commands: list[dict[str, object]] = []
    file_events: list[dict[str, object]] = []

    for message in bundle.get("messages", []):
        role = str(message.get("role") or "meta")
        role_counts[role] = role_counts.get(role, 0) + 1

    for entry in bundle.get("raw_entries", []):
        entry_type = entry.get("type")
        if entry_type == "assistant":
            for item in entry.get("message", {}).get("content", []):
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    name = str(item.get("name") or "unknown")
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                    payload = item.get("input") or {}
                    timestamp = format_export_ts(parse_timestamp_ms(entry.get("timestamp")))
                    if isinstance(payload, dict):
                        file_path = payload.get("file_path")
                        if isinstance(file_path, str):
                            file_events.append(
                                {"timestamp": timestamp, "kind": name.lower(), "path": file_path}
                            )
                        command = payload.get("command")
                        if isinstance(command, str):
                            commands.append(
                                {
                                    "timestamp": timestamp,
                                    "command": command,
                                    "exit_code": "",
                                    "tool": name,
                                }
                            )
        elif entry_type == "user":
            result = entry.get("toolUseResult")
            timestamp = format_export_ts(parse_timestamp_ms(entry.get("timestamp")))
            if isinstance(result, dict):
                file_path = result.get("filePath")
                if isinstance(file_path, str):
                    file_events.append({"timestamp": timestamp, "kind": "tool_result", "path": file_path})
                stdout = result.get("stdout")
                stderr = result.get("stderr")
                if isinstance(stdout, str) or isinstance(stderr, str):
                    if commands:
                        last = commands[-1]
                        if last.get("exit_code") == "":
                            last["exit_code"] = 0 if not stderr else 1

    return {
        "role_counts": role_counts,
        "tool_counts": tool_counts,
        "commands": commands,
        "file_events": file_events,
    }


def export_session(session: SessionSummary, export_format: str, output: str = "") -> Path:
    return write_export(build_claude_export(session), export_format, output)


def write_web_bundle(session: SessionSummary, output_dir: str = "", temp: bool = False) -> Path:
    return write_bundle(build_claude_export(session), output_dir=output_dir, temp=temp)


def interactive_web_bundle(stdscr: curses.window, session: SessionSummary) -> str:
    mode = prompt_choice(
        stdscr,
        "Web mode [bundle|serve|tunnel] (default: tunnel)",
        {"bundle", "serve", "tunnel"},
    ) or "tunnel"

    if mode == "bundle":
        default_dir = f"claude-session-{session.session_id}-bundle"
        output_dir = prompt_input(stdscr, f"Bundle path [{default_dir}]") or default_dir
        try:
            path = write_web_bundle(session, output_dir=output_dir)
        except OSError as exc:
            return f"Bundle export failed: {exc}"
        return f"Bundle written to {path}"

    output_hint = prompt_input(stdscr, "Bundle path [temp]")
    keep_raw = prompt_input(stdscr, "Keep bundle after session ends? [y/N]")
    keep_bundle = keep_raw.lower() in {"y", "yes"}
    try:
        path = write_web_bundle(session, output_dir=output_hint, temp=not bool(output_hint))
        served = serve_bundle(
            path,
            with_tunnel=(mode == "tunnel"),
            keep_bundle=keep_bundle,
            tool="claude",
            session_id=session.session_id,
        )
    except Exception as exc:
        return f"Bundle serve failed: {exc}"
    target_url = served.public_url or served.local_url
    return f"Started {mode} share for {session.session_id}: {target_url}"


def main() -> int:
    args = parse_args()
    claude_dir = Path(os.path.expanduser(args.claude_dir))

    if args.active_shares:
        return show_active_shares_report("claude")

    try:
        sessions = load_sessions(claude_dir, limit=args.limit)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.export:
        if not args.session_id:
            print("--export requires --session-id", file=sys.stderr)
            return 1
        session = next((item for item in sessions if item.session_id == args.session_id), None)
        if session is None:
            print(f"Unknown session id: {args.session_id}", file=sys.stderr)
            return 1
        path = export_session(session, args.export, args.output)
        print(str(path))
        return 0

    if args.json:
        payload = [
            {
                "session_id": item.session_id,
                "project": item.project,
                "cwd": item.cwd,
                "first_prompt": item.first_prompt,
                "last_prompt": item.last_prompt,
                "last_activity": format_ts(item.sort_ts_ms),
                "transcript_path": str(item.transcript_path) if item.transcript_path else None,
                "message_count": item.message_count,
                "user_count": item.user_count,
                "assistant_count": item.assistant_count,
            }
            for item in filter_sessions(sessions, args.query)
        ]
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    command = curses.wrapper(run_tui, sessions, args.query)
    if command:
        if args.output_file:
            Path(args.output_file).write_text(command + "\n", encoding="utf-8")
        else:
            print(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
