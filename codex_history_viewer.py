#!/usr/bin/env python3
"""Interactive local Codex session viewer."""

from __future__ import annotations

import argparse
import curses
import datetime as dt
import json
import os
import re
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


DEFAULT_CODEX_DIR = Path.home() / ".codex"
SESSION_ID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")


@dataclass
class SessionSummary:
    session_id: str
    cwd: str
    transcript_path: Path | None
    started_ts_ms: int | None
    last_ts_ms: int | None
    first_prompt: str
    last_prompt: str
    prompt_count: int
    user_count: int
    assistant_count: int

    @property
    def sort_ts_ms(self) -> int:
        return self.last_ts_ms or self.started_ts_ms or 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Browse local Codex session history and print a resume command."
    )
    parser.add_argument(
        "--codex-dir",
        default=str(DEFAULT_CODEX_DIR),
        help="Path to the Codex state directory (default: ~/.codex)",
    )
    parser.add_argument("--limit", type=int, default=200, help="Maximum number of sessions to load.")
    parser.add_argument(
        "--query",
        default="",
        help="Initial case-insensitive filter against cwd, prompts, and session ID.",
    )
    parser.add_argument("--json", action="store_true", help="Print summaries as JSON.")
    parser.add_argument(
        "--output-file",
        default="",
        help="When set, write the selected resume command to this file instead of stdout.",
    )
    parser.add_argument("--session-id", default="", help="Session ID to export in non-interactive mode.")
    parser.add_argument("--export", choices=["json", "md", "html"], help="Export a session conversation.")
    parser.add_argument(
        "--output",
        default="",
        help="Output path for --export. Defaults to ./codex-session-<id>.<ext>.",
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


def compact_text(value: object, max_len: int = 240) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                if item.get("type") in {"output_text", "input_text"} and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            elif isinstance(item, str):
                parts.append(item)
        text = " ".join(parts)
    elif isinstance(value, dict):
        if isinstance(value.get("text"), str):
            text = value["text"]
        elif isinstance(value.get("content"), str):
            text = value["content"]
        elif isinstance(value.get("content"), list):
            text = compact_text(value["content"], max_len=max_len)
        else:
            text = json.dumps(value, ensure_ascii=True)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text[: max_len - 1] + "…" if len(text) > max_len else text


def parse_timestamp_ms(value: object) -> int | None:
    if isinstance(value, (int, float)):
        return int(value) * 1000 if value < 10_000_000_000 else int(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return None


def discover_transcript_map(sessions_dir: Path) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    if not sessions_dir.exists():
        return mapping
    for path in sessions_dir.rglob("*.jsonl"):
        match = SESSION_ID_RE.search(path.stem)
        if match:
            mapping.setdefault(match.group(1), path)
    return mapping


def load_history_prompts(history_path: Path) -> dict[str, dict[str, object]]:
    by_session: dict[str, dict[str, object]] = {}
    for entry in read_jsonl(history_path):
        session_id = entry.get("session_id")
        text = entry.get("text")
        ts = entry.get("ts")
        if not isinstance(session_id, str):
            continue
        bucket = by_session.setdefault(session_id, {"prompts": [], "last_ts_ms": None})
        if isinstance(text, str) and text.strip():
            bucket["prompts"].append(text.strip())
        parsed_ts = parse_timestamp_ms(ts)
        if parsed_ts is not None:
            bucket["last_ts_ms"] = max(parsed_ts, int(bucket["last_ts_ms"] or 0))
    return by_session


def parse_transcript(transcript_path: Path | None) -> dict[str, object]:
    parsed: dict[str, object] = {
        "cwd": "",
        "started_ts_ms": None,
        "last_ts_ms": None,
        "user_count": 0,
        "assistant_count": 0,
    }
    if not transcript_path or not transcript_path.exists():
        return parsed

    for entry in read_jsonl(transcript_path):
        entry_ts = parse_timestamp_ms(entry.get("timestamp"))
        if entry_ts is not None:
            parsed["last_ts_ms"] = max(entry_ts, int(parsed["last_ts_ms"] or 0))

        entry_type = entry.get("type")
        payload = entry.get("payload")
        if entry_type == "session_meta" and isinstance(payload, dict):
            parsed["cwd"] = payload.get("cwd") or parsed["cwd"]
            parsed["started_ts_ms"] = parse_timestamp_ms(payload.get("timestamp")) or parsed["started_ts_ms"]
        elif entry_type == "response_item" and isinstance(payload, dict):
            role = payload.get("role")
            if role == "user":
                parsed["user_count"] = int(parsed["user_count"]) + 1
            elif role == "assistant":
                parsed["assistant_count"] = int(parsed["assistant_count"]) + 1
    return parsed


def load_sessions(codex_dir: Path, limit: int) -> list[SessionSummary]:
    history_path = codex_dir / "history.jsonl"
    if not history_path.exists():
        raise FileNotFoundError(f"Missing history file: {history_path}")

    by_session = load_history_prompts(history_path)
    transcript_map = discover_transcript_map(codex_dir / "sessions")
    sessions: list[SessionSummary] = []

    for session_id, info in by_session.items():
        prompts = list(info.get("prompts", []))
        transcript_path = transcript_map.get(session_id)
        transcript = parse_transcript(transcript_path)
        first_prompt = prompts[0] if prompts else ""
        last_prompt = prompts[-1] if prompts else ""
        last_ts_ms = transcript.get("last_ts_ms") or info.get("last_ts_ms")

        sessions.append(
            SessionSummary(
                session_id=session_id,
                cwd=str(transcript.get("cwd") or ""),
                transcript_path=transcript_path,
                started_ts_ms=transcript.get("started_ts_ms"),
                last_ts_ms=last_ts_ms if isinstance(last_ts_ms, int) else None,
                first_prompt=first_prompt,
                last_prompt=last_prompt,
                prompt_count=len(prompts),
                user_count=int(transcript.get("user_count") or 0),
                assistant_count=int(transcript.get("assistant_count") or 0),
            )
        )

    sessions.sort(key=lambda item: item.sort_ts_ms, reverse=True)
    return sessions[:limit]


def filter_sessions(sessions: list[SessionSummary], query: str) -> list[SessionSummary]:
    if not query:
        return sessions
    needle = query.lower()
    filtered: list[SessionSummary] = []
    for session in sessions:
        haystack = "\n".join(
            [session.session_id, session.cwd, session.first_prompt, session.last_prompt]
        ).lower()
        if needle in haystack:
            filtered.append(session)
    return filtered


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


def clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    return value[: width - 1] + "…" if len(value) > width else value


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


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
        if key == 27:
            value = ""
            break
        if key in (curses.KEY_BACKSPACE, 127):
            value = value[:-1]
            continue
        if 32 <= key <= 126:
            value += chr(key)
    curses.curs_set(0)
    return value.strip()


def prompt_choice(stdscr: curses.window, label: str, allowed: set[str]) -> str:
    value = prompt_input(stdscr, label)
    if not value:
        return ""
    value = value.lower()
    return value if value in allowed else ""


def draw_lines(win: curses.window, start_y: int, start_x: int, width: int, lines: list[str]) -> None:
    y = start_y
    for line in lines:
        if y >= curses.LINES - 1:
            break
        win.addnstr(y, start_x, line, max(width, 0))
        y += 1


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
        public_marker = "public" if share.get("public_url") else "local"
        line = (
            f"{marker} {format_share_age(str(share.get('started_at') or '')):>8}  "
            f"{clip(str(share.get('mode') or 'serve'), 6):6}  "
            f"{clip(public_marker, 6):6}  "
            f"{clip(str(share.get('session_id') or ''), max(8, list_width - 28))}"
        )
        attr = curses.A_REVERSE if idx == selected else 0
        stdscr.addnstr(row, 0, clip(line, list_width - 1), list_width - 1, attr)

    share = shares[selected]
    detail_width = max(10, width - detail_x - 1)
    tunnel_state = "up" if share.get("tunnel_alive") else "down"
    details = [
        f"Session ID: {share.get('session_id', 'unknown')}",
        f"Mode: {share.get('mode', 'unknown')}",
        f"Started: {share.get('started_at', 'unknown')} ({format_share_age(str(share.get('started_at') or ''))})",
        f"Bundle: {share.get('bundle_dir', 'unknown')}",
        f"Local URL: {share.get('local_url', 'unknown')}",
        f"Public URL: {share.get('public_url') or 'not exposed'}",
        f"Server PID: {share.get('server_pid', 'unknown')}",
        f"Tunnel PID: {share.get('tunnel_pid', 'none')} ({tunnel_state})",
        "",
        "Notes:",
        "These rows are discovered from live share state files in /tmp.",
        "Keys: x stop selected share, r refresh.",
        "Closed or crashed sessions are removed automatically on refresh.",
    ]
    draw_lines(stdscr, top_y, detail_x, detail_width, [clip(line, detail_width) for line in details])


def build_codex_export(session: SessionSummary) -> dict:
    raw_entries: list[dict] = []
    messages: list[dict] = []
    metadata: dict[str, object] = {}
    if session.transcript_path and session.transcript_path.exists():
        for entry in read_jsonl(session.transcript_path):
            raw_entries.append(entry)
            entry_type = entry.get("type")
            payload = entry.get("payload")
            if entry_type == "session_meta" and isinstance(payload, dict):
                metadata["originator"] = payload.get("originator")
                metadata["cli_version"] = payload.get("cli_version")
                metadata["model_provider"] = payload.get("model_provider")
                messages.append(
                    {
                        "timestamp": format_export_ts(parse_timestamp_ms(entry.get("timestamp"))),
                        "role": "system",
                        "kind": "session_meta",
                        "raw_type": entry_type,
                        "text": (
                            f"Session started in {payload.get('cwd', 'unknown cwd')} "
                            f"via {payload.get('originator', 'unknown originator')} "
                            f"(Codex {payload.get('cli_version', 'unknown version')})."
                        ),
                        "extra": None,
                    }
                )
            elif entry_type == "response_item" and isinstance(payload, dict):
                role = payload.get("role") or "meta"
                content = payload.get("content")
                text = compact_text(content, max_len=8000)
                messages.append(
                    {
                        "timestamp": format_export_ts(parse_timestamp_ms(entry.get("timestamp"))),
                        "role": role,
                        "kind": payload.get("type") or "message",
                        "raw_type": entry_type,
                        "text": text,
                        "extra": None,
                    }
                )
            elif entry_type == "event_msg":
                event_type = payload.get("type") if isinstance(payload, dict) else None
                messages.append(
                    {
                        "timestamp": format_export_ts(parse_timestamp_ms(entry.get("timestamp"))),
                        "role": "system",
                        "kind": "event",
                        "raw_type": entry_type,
                        "text": compact_text(payload, max_len=1200),
                        "extra": {"event_type": event_type} if event_type else None,
                    }
                )

    bundle = {
        "schema_version": 1,
        "tool": "codex",
        "exported_at": now_iso(),
        "session": {
            "session_id": session.session_id,
            "cwd": session.cwd,
            "started_at": format_export_ts(session.started_ts_ms),
            "last_activity": format_export_ts(session.last_ts_ms),
            "transcript_path": str(session.transcript_path) if session.transcript_path else None,
            "first_prompt": session.first_prompt,
            "last_prompt": session.last_prompt,
            "prompt_count": session.prompt_count,
            "user_count": session.user_count,
            "assistant_count": session.assistant_count,
        },
        "metadata": metadata,
        "messages": messages,
        "raw_entries": raw_entries,
    }
    bundle["analytics"] = build_codex_analytics(bundle)
    return bundle


def build_codex_analytics(bundle: dict) -> dict:
    role_counts: dict[str, int] = {}
    tool_counts: dict[str, int] = {}
    commands: list[dict[str, object]] = []
    file_events: list[dict[str, object]] = []

    for message in bundle.get("messages", []):
        role = str(message.get("role") or "meta")
        role_counts[role] = role_counts.get(role, 0) + 1

    for entry in bundle.get("raw_entries", []):
        entry_type = entry.get("type")
        payload = entry.get("payload")
        timestamp = format_export_ts(parse_timestamp_ms(entry.get("timestamp")))
        if entry_type == "response_item" and isinstance(payload, dict):
            item_type = payload.get("type")
            if item_type in {"function_call", "custom_tool_call"}:
                name = str(payload.get("name") or "unknown")
                tool_counts[name] = tool_counts.get(name, 0) + 1
                if item_type == "function_call":
                    args = payload.get("arguments")
                else:
                    args = payload.get("input")
                try:
                    parsed = json.loads(args) if isinstance(args, str) else args
                except json.JSONDecodeError:
                    parsed = {}
                if isinstance(parsed, dict):
                    file_path = parsed.get("file_path")
                    if isinstance(file_path, str):
                        file_events.append({"timestamp": timestamp, "kind": name.lower(), "path": file_path})
                    cmd = parsed.get("cmd")
                    if isinstance(cmd, str):
                        commands.append({"timestamp": timestamp, "command": cmd, "exit_code": "", "tool": name})
            elif item_type in {"function_call_output", "custom_tool_call_output"}:
                output = payload.get("output")
                if isinstance(output, str):
                    if "Updated the following files:" in output or "\"changes\"" in output:
                        for path in re.findall(r"/[^\s:\"']+", output):
                            file_events.append({"timestamp": timestamp, "kind": "tool_output", "path": path})
        elif entry_type == "event_msg" and isinstance(payload, dict):
            event_type = payload.get("type")
            if event_type == "exec_command_end":
                command = payload.get("aggregated_output")
                executed = payload.get("command")
                if isinstance(executed, list):
                    command_text = " ".join(str(part) for part in executed)
                else:
                    command_text = ""
                commands.append(
                    {
                        "timestamp": timestamp,
                        "command": command_text,
                        "exit_code": payload.get("exit_code"),
                        "tool": "exec_command",
                    }
                )
            elif event_type == "patch_apply_end":
                changes = payload.get("changes")
                if isinstance(changes, dict):
                    for path, meta in changes.items():
                        kind = meta.get("type") if isinstance(meta, dict) else "patch"
                        file_events.append({"timestamp": timestamp, "kind": str(kind), "path": str(path)})

    return {
        "role_counts": role_counts,
        "tool_counts": tool_counts,
        "commands": commands,
        "file_events": file_events,
    }


def export_session(session: SessionSummary, export_format: str, output: str = "") -> Path:
    return write_export(build_codex_export(session), export_format, output)


def write_web_bundle(session: SessionSummary, output_dir: str = "", temp: bool = False) -> Path:
    return write_bundle(build_codex_export(session), output_dir=output_dir, temp=temp)


def interactive_export(stdscr: curses.window, session: SessionSummary) -> str:
    format_value = prompt_input(stdscr, "Export format [json|md|html]")
    if not format_value:
        return "Export cancelled."
    format_value = format_value.lower()
    if format_value not in {"json", "md", "html"}:
        return f"Unsupported export format: {format_value}"
    default_name = f"codex-session-{session.session_id}.{format_value}"
    output_value = prompt_input(stdscr, f"Output path [{default_name}]")
    output_path = output_value or default_name
    try:
        path = export_session(session, format_value, output_path)
    except OSError as exc:
        return f"Export failed: {exc}"
    return f"Exported {format_value} to {path}"


def interactive_web_bundle(stdscr: curses.window, session: SessionSummary) -> str:
    mode = prompt_choice(
        stdscr,
        "Web mode [bundle|serve|tunnel] (default: tunnel)",
        {"bundle", "serve", "tunnel"},
    ) or "tunnel"

    if mode == "bundle":
        default_dir = f"codex-session-{session.session_id}-bundle"
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
            tool="codex",
            session_id=session.session_id,
        )
    except Exception as exc:
        return f"Bundle serve failed: {exc}"
    target_url = served.public_url or served.local_url
    return f"Started {mode} share for {session.session_id}: {target_url}"


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
        shares = list_active_shares("codex")
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
        stdscr.addnstr(0, 0, clip("Codex History Viewer", width - 1), width - 1, curses.A_BOLD)
        help_text = "Tab switch tab  Up/Down move  Enter resumes  / filter  c command  e export  w share start/stop  J/M/H quick export  q quit"
        stdscr.addnstr(1, 0, clip(help_text, width - 1), width - 1)
        render_tabs(stdscr, width, active_tab)
        info_text = f"Filter: {query or '(none)'}" if active_tab == "sessions" else f"Active shares: {len(shares)}"
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
                line = (
                    f"{marker} {relative_age(session.sort_ts_ms):>8}  "
                    f"{clip(session.cwd or '(unknown cwd)', 28):28}  "
                    f"{clip(session.last_prompt or session.first_prompt, max(8, list_width - 44))}"
                )
                attr = curses.A_REVERSE if idx == selected else 0
                stdscr.addnstr(row, 0, clip(line, list_width - 1), list_width - 1, attr)

            session = filtered[selected]
            detail_width = max(10, width - detail_x - 1)
            details = [
                f"Session ID: {session.session_id}",
                f"CWD: {session.cwd or 'unknown'}",
                f"Last activity: {format_ts(session.sort_ts_ms)} ({relative_age(session.sort_ts_ms)})",
                f"Transcript: {session.transcript_path or 'not found'}",
                f"Counts: prompts={session.prompt_count} user_items={session.user_count} assistant_items={session.assistant_count}",
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
                    target = share.get("public_url") or share.get("local_url") or "unknown"
                    details.append(f"- {share.get('mode', 'serve')}: {target}")
                if len(active_session_shares) > 2:
                    details.append(f"- +{len(active_session_shares) - 2} more")
            else:
                details.append("Web share: none")
            details.append("")
            details.append("Resume command:")
            details.append(f"cd {shell_quote(session.cwd or os.getcwd())} && codex resume {session.session_id}")
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
                return f"cd {shell_quote(session.cwd or os.getcwd())} && codex resume {session.session_id}"
            elif key == ord("c"):
                return f"codex resume {session.session_id}"
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


def main() -> int:
    args = parse_args()
    codex_dir = Path(os.path.expanduser(args.codex_dir))
    try:
        sessions = load_sessions(codex_dir, args.limit)
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
                "cwd": item.cwd,
                "first_prompt": item.first_prompt,
                "last_prompt": item.last_prompt,
                "last_activity": format_ts(item.sort_ts_ms),
                "transcript_path": str(item.transcript_path) if item.transcript_path else None,
                "prompt_count": item.prompt_count,
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
