#!/usr/bin/env python3
"""Shared export helpers for session history viewers."""

from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path


SCHEMA_VERSION = 1


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat()


def format_export_ts(ts_ms: int | None) -> str:
    if not ts_ms:
        return "unknown"
    return dt.datetime.fromtimestamp(ts_ms / 1000).astimezone().isoformat()


def default_export_path(tool: str, session_id: str, fmt: str, output: str = "") -> Path:
    if output:
        return Path(output).expanduser()
    return Path.cwd() / f"{tool}-session-{session_id}.{fmt}"


def write_export(bundle: dict, fmt: str, output: str = "") -> Path:
    target = default_export_path(bundle["tool"], bundle["session"]["session_id"], fmt, output)
    target.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        target.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    elif fmt == "md":
        target.write_text(render_markdown(bundle), encoding="utf-8")
    elif fmt == "html":
        target.write_text(render_html(bundle), encoding="utf-8")
    else:
        raise ValueError(f"Unsupported export format: {fmt}")
    return target


def render_markdown(bundle: dict) -> str:
    session = bundle["session"]
    metadata = bundle.get("metadata", {})
    lines = [
        f"# {bundle['tool'].capitalize()} Session Export",
        "",
        f"- Session ID: `{session['session_id']}`",
        f"- Exported at: `{bundle['exported_at']}`",
        f"- Started at: `{session.get('started_at', 'unknown')}`",
        f"- Last activity: `{session.get('last_activity', 'unknown')}`",
        f"- Working directory: `{session.get('cwd', 'unknown')}`",
        f"- Transcript path: `{session.get('transcript_path', 'unknown')}`",
        f"- Message count: `{len(bundle.get('messages', []))}`",
    ]
    if metadata:
        lines.append("")
        lines.append("## Metadata")
        lines.append("")
        for key, value in metadata.items():
            lines.append(f"- {key}: `{value}`")
    lines.append("")
    lines.append("## Conversation")
    lines.append("")
    for index, message in enumerate(bundle.get("messages", []), start=1):
        lines.append(f"### {index}. {message['role'].capitalize()}")
        lines.append("")
        lines.append(f"- Timestamp: `{message.get('timestamp', 'unknown')}`")
        lines.append(f"- Kind: `{message.get('kind', 'message')}`")
        if message.get("raw_type"):
            lines.append(f"- Raw type: `{message['raw_type']}`")
        if message.get("extra"):
            for key, value in message["extra"].items():
                lines.append(f"- {key}: `{value}`")
        lines.append("")
        text = message.get("text") or ""
        lines.append(text if text else "_(no text extracted)_")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_html(bundle: dict) -> str:
    session = bundle["session"]
    metadata_items = [
        ("Session ID", session["session_id"]),
        ("Exported at", bundle["exported_at"]),
        ("Started at", session.get("started_at", "unknown")),
        ("Last activity", session.get("last_activity", "unknown")),
        ("Working directory", session.get("cwd", "unknown")),
        ("Transcript path", session.get("transcript_path", "unknown")),
        ("Messages", str(len(bundle.get("messages", [])))),
    ]
    metadata_items.extend((key, str(value)) for key, value in bundle.get("metadata", {}).items())

    cards: list[str] = []
    for message in bundle.get("messages", []):
        role = html.escape(message.get("role", "meta"))
        text = html.escape(message.get("text", "") or "(no text extracted)")
        timestamp = html.escape(message.get("timestamp", "unknown"))
        kind = html.escape(message.get("kind", "message"))
        raw_type = html.escape(message.get("raw_type", ""))
        extra = ""
        if message.get("extra"):
            extra = "".join(
                f"<li><strong>{html.escape(str(k))}</strong>: {html.escape(str(v))}</li>"
                for k, v in message["extra"].items()
            )
            extra = f"<ul class='extra'>{extra}</ul>"
        cards.append(
            "<article class='msg role-{role}'>"
            "<header><span class='role'>{role}</span>"
            "<span class='meta'>{timestamp} · {kind}{raw}</span></header>"
            "<pre>{text}</pre>{extra}</article>".format(
                role=role,
                timestamp=timestamp,
                kind=kind,
                raw=f" · {raw_type}" if raw_type else "",
                text=text,
                extra=extra,
            )
        )

    meta_html = "".join(
        f"<li><strong>{html.escape(label)}</strong><span>{html.escape(value)}</span></li>"
        for label, value in metadata_items
    )

    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      --bg: #f3efe6;
      --paper: #fffdf8;
      --ink: #1f1b16;
      --muted: #6a6259;
      --line: #d9cfc1;
      --user: #d9ecff;
      --assistant: #fbe7c9;
      --meta: #ece7df;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Iosevka Aile", "IBM Plex Sans", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, #fff7d6 0, transparent 28%),
        linear-gradient(160deg, #f3efe6, #e9dfd2);
    }}
    .page {{
      display: grid;
      grid-template-columns: 320px minmax(0, 1fr);
      min-height: 100vh;
    }}
    aside {{
      padding: 24px;
      border-right: 1px solid var(--line);
      background: rgba(255, 253, 248, 0.72);
      backdrop-filter: blur(10px);
    }}
    main {{
      padding: 24px;
    }}
    h1 {{
      margin: 0 0 16px;
      font-size: 28px;
      line-height: 1.1;
    }}
    .meta-list {{
      list-style: none;
      padding: 0;
      margin: 0;
      display: grid;
      gap: 10px;
    }}
    .meta-list li {{
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--paper);
    }}
    .meta-list strong {{
      display: block;
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 4px;
    }}
    .conversation {{
      display: grid;
      gap: 16px;
    }}
    .msg {{
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 14px 16px;
      background: var(--paper);
      box-shadow: 0 10px 28px rgba(31, 27, 22, 0.06);
    }}
    .role-user {{ background: var(--user); }}
    .role-assistant {{ background: var(--assistant); }}
    .role-meta, .role-system {{ background: var(--meta); }}
    .msg header {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
      font-size: 13px;
      color: var(--muted);
    }}
    .msg .role {{
      font-weight: 700;
      color: var(--ink);
      text-transform: capitalize;
    }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: "Iosevka", "IBM Plex Mono", monospace;
      font-size: 14px;
      line-height: 1.5;
    }}
    .extra {{
      margin: 10px 0 0;
      padding-left: 18px;
      color: var(--muted);
      font-size: 13px;
    }}
    @media (max-width: 900px) {{
      .page {{ grid-template-columns: 1fr; }}
      aside {{ border-right: 0; border-bottom: 1px solid var(--line); }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <aside>
      <h1>{title}</h1>
      <ul class="meta-list">{meta_html}</ul>
    </aside>
    <main>
      <section class="conversation">{cards}</section>
    </main>
  </div>
</body>
</html>
""".format(
        title=html.escape(f"{bundle['tool'].capitalize()} Session {session['session_id']}"),
        meta_html=meta_html,
        cards="".join(cards),
    )
