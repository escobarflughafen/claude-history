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
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


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


def run_tui(stdscr: curses.window, sessions: list[SessionSummary], initial_query: str) -> str | None:
    curses.curs_set(0)
    stdscr.keypad(True)
    query = initial_query
    selected = 0
    scroll = 0

    while True:
        filtered = filter_sessions(sessions, query)
        if selected >= len(filtered):
            selected = max(0, len(filtered) - 1)

        height, width = stdscr.getmaxyx()
        list_width = max(40, min(72, width // 2))
        detail_x = list_width + 2
        visible_rows = max(5, height - 5)

        if selected < scroll:
            scroll = selected
        elif selected >= scroll + visible_rows:
            scroll = selected - visible_rows + 1

        stdscr.erase()
        stdscr.addnstr(0, 0, clip("Claude History Viewer", width - 1), width - 1, curses.A_BOLD)
        help_text = "Up/Down move  Enter prints resume command  / filter  c copy command view  q quit"
        stdscr.addnstr(1, 0, clip(help_text, width - 1), width - 1)
        stdscr.addnstr(2, 0, clip(f"Filter: {query or '(none)'}", width - 1), width - 1, curses.A_DIM)

        if not filtered:
            stdscr.addnstr(4, 0, clip("No sessions matched the current filter.", width - 1), width - 1)
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("q"), 27):
                return None
            if key == ord("/"):
                query = prompt_input(stdscr, "Filter")
            elif key in (curses.KEY_BACKSPACE, 127):
                query = query[:-1]
            continue

        for idx in range(scroll, min(len(filtered), scroll + visible_rows)):
            session = filtered[idx]
            row = 4 + idx - scroll
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
        details.append("Resume command:")
        details.append(f"cd {shell_quote(session.cwd)} && claude --resume {session.session_id}")
        draw_lines(stdscr, 4, detail_x, detail_width, [clip(line, detail_width) for line in details])

        stdscr.refresh()
        key = stdscr.getch()

        if key in (ord("q"), 27):
            return None
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
        elif key == ord("/"):
            query = prompt_input(stdscr, "Filter")
            selected = 0
            scroll = 0
        elif key in (curses.KEY_BACKSPACE, 127):
            query = query[:-1]
            selected = 0
            scroll = 0


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


def main() -> int:
    args = parse_args()
    claude_dir = Path(os.path.expanduser(args.claude_dir))

    try:
        sessions = load_sessions(claude_dir, limit=args.limit)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

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
