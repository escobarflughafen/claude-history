#!/usr/bin/env python3
"""Shared export, bundle, and temporary serving helpers for session viewers."""

from __future__ import annotations

import atexit
import datetime as dt
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path


SCHEMA_VERSION = 1
TRYCLOUDFLARE_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
SHARE_STATE_DIR = Path(tempfile.gettempdir()) / "history-viewer-shares"


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


def make_bundle_dir(tool: str, session_id: str, output_dir: str = "", temp: bool = False) -> Path:
    if output_dir:
        bundle_dir = Path(output_dir).expanduser()
    elif temp:
        bundle_dir = Path(tempfile.mkdtemp(prefix=f"{tool}-session-{session_id}-", dir="/tmp"))
    else:
        bundle_dir = Path.cwd() / f"{tool}-session-{session_id}-bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    return bundle_dir


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


def write_bundle(bundle: dict, output_dir: str = "", temp: bool = False) -> Path:
    bundle_dir = make_bundle_dir(bundle["tool"], bundle["session"]["session_id"], output_dir, temp=temp)
    session_json = bundle_dir / "session.json"
    index_html = bundle_dir / "index.html"
    readme_md = bundle_dir / "README.md"
    transcript_jsonl = bundle_dir / "transcript.jsonl"
    zip_path = bundle_dir / "package.zip"
    session_json.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    index_html.write_text(render_bundle_index(bundle), encoding="utf-8")
    readme_md.write_text(render_bundle_readme(bundle), encoding="utf-8")
    transcript_jsonl.write_text(render_bundle_transcript(bundle), encoding="utf-8")
    if bundle.get("tool") == "claude":
        importer_path = bundle_dir / "import_claude_bundle.py"
        importer_path.write_text(render_claude_importer_script(), encoding="utf-8")
    tmp_zip = shutil.make_archive(str(bundle_dir / "package"), "zip", root_dir=bundle_dir)
    if Path(tmp_zip) != zip_path:
        shutil.move(tmp_zip, zip_path)
    return bundle_dir


def render_bundle_transcript(bundle: dict) -> str:
    raw_entries = bundle.get("raw_entries", [])
    if not raw_entries:
        return ""
    return "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in raw_entries if isinstance(entry, dict))


def render_bundle_readme(bundle: dict) -> str:
    tool = str(bundle.get("tool") or "session")
    session = bundle.get("session", {})
    session_id = session.get("session_id", "unknown")
    cwd = session.get("cwd", "unknown")
    if tool == "claude":
        import_block = textwrap.dedent(
            f"""\
            ## Import Into Claude Code

            This bundle includes a standalone importer:

            ```bash
            python3 import_claude_bundle.py
            ```

            Typical usage:

            ```bash
            python3 import_claude_bundle.py --bundle ./session.json
            python3 import_claude_bundle.py --bundle ./session.json --import-cwd /path/to/restored/project
            python3 import_claude_bundle.py --bundle ./session.json --import-session-id "$(python3 - <<'PY'
            import uuid
            print(uuid.uuid4())
            PY
            )"
            ```

            After import, resume from the matching working directory:

            ```bash
            cd /path/to/restored/project
            claude --resume {session_id}
            ```

            Notes:

            - Treat downloaded bundles as sensitive. They may contain prompts, tool outputs, and file content from the original session.
            - Only run `import_claude_bundle.py` on bundles from trusted sources.
            - Claude must already be installed and authenticated on the destination host.
            - The working directory used for resume must match the imported project path.
            - `transcript.jsonl` is the raw session transcript shipped for inspection and migration.
            """
        )
    else:
        import_block = textwrap.dedent(
            """\
            ## Contents

            - `session.json`: exported session metadata and analytics
            - `transcript.jsonl`: raw session transcript entries when available
            - `index.html`: portable viewer
            - `package.zip`: zipped copy of the bundle
            """
        )

    return textwrap.dedent(
        f"""\
        # {tool.capitalize()} Session Bundle

        ## Session

        - Session ID: `{session_id}`
        - Working directory: `{cwd}`
        - Exported at: `{bundle.get('exported_at', 'unknown')}`
        - Message count: `{len(bundle.get('messages', []))}`

        ## Files

        - `session.json`: structured export with metadata, analytics, messages, and raw entries
        - `transcript.jsonl`: raw transcript entries suitable for migration and inspection
        - `index.html`: self-contained web viewer
        - `package.zip`: zipped copy of this directory
        {"- `import_claude_bundle.py`: standalone Claude session importer" if tool == "claude" else ""}

        ## Command-Line Download

        If this bundle is served over HTTP, you can download the full package:

        ```bash
        curl -O http://HOST:PORT/package.zip
        ```

        Or fetch individual files:

        ```bash
        curl -O http://HOST:PORT/session.json
        curl -O http://HOST:PORT/transcript.jsonl
        curl -O http://HOST:PORT/README.md
        {"curl -O http://HOST:PORT/import_claude_bundle.py" if tool == "claude" else ""}
        ```

        ## Security Notes

        - This bundle may contain sensitive transcript content, prompts, tool results, and file excerpts.
        - Only import bundles from trusted sources.
        - Import affects Claude session state under `~/.claude`; it does not restore arbitrary workspace files by itself.

        {import_block.rstrip()}
        """
    ).rstrip() + "\n"


def render_claude_importer_script() -> str:
    return textwrap.dedent(
        r'''#!/usr/bin/env python3
"""Standalone importer for a Claude session bundle."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path


def read_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def compact_text(value, max_len: int = 200) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts = []
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


def parse_timestamp_ms(value):
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


def encode_project_path(project: str) -> str:
    return "".join("-" if ch == "/" else ch for ch in project.replace("\\", "/").replace(":", "")) or "-"


def safe_extract_zip(source: Path, destination: Path):
    with zipfile.ZipFile(source) as archive:
        base = destination.resolve()
        for member in archive.infolist():
            member_path = Path(member.filename)
            if member_path.is_absolute():
                raise ValueError(f"Refusing to extract absolute zip path: {member.filename}")
            target = (destination / member_path).resolve()
            if os.path.commonpath([str(base), str(target)]) != str(base):
                raise ValueError(f"Refusing to extract path outside bundle temp dir: {member.filename}")
        archive.extractall(destination)


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding=encoding, dir=str(path.parent), delete=False) as handle:
        handle.write(content)
        temp_name = handle.name
    Path(temp_name).replace(path)


def resolve_bundle_payload(source: Path):
    if source.is_dir():
        session_json = source / "session.json"
        if not session_json.exists():
            raise FileNotFoundError(f"Bundle directory missing session.json: {source}")
        return json.loads(session_json.read_text(encoding="utf-8")), None
    if source.suffix.lower() == ".zip":
        temp_dir = Path(tempfile.mkdtemp(prefix="claude-bundle-import-", dir="/tmp"))
        safe_extract_zip(source, temp_dir)
        session_json = temp_dir / "session.json"
        if not session_json.exists():
            raise FileNotFoundError(f"Bundle zip missing session.json: {source}")
        return json.loads(session_json.read_text(encoding="utf-8")), temp_dir
    return json.loads(source.read_text(encoding="utf-8")), None


def bundle_to_history_entries(bundle: dict, project: str, session_id: str):
    entries = []
    for entry in bundle.get("raw_entries", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "user" or entry.get("isMeta"):
            continue
        text = extract_user_prompt(entry)
        if not text:
            continue
        entries.append(
            {
                "display": text,
                "pastedContents": {},
                "timestamp": parse_timestamp_ms(entry.get("timestamp")) or 0,
                "project": project,
                "sessionId": session_id,
            }
        )
    return entries


def rewritten_raw_entries(bundle: dict, session_id: str, cwd: str):
    out = []
    for raw in bundle.get("raw_entries", []):
        if not isinstance(raw, dict):
            continue
        entry = json.loads(json.dumps(raw))
        entry["sessionId"] = session_id
        if "cwd" in entry or entry.get("type") in {"user", "assistant", "attachment"}:
            entry["cwd"] = cwd
        out.append(entry)
    return out


def import_bundle(source_path: str, claude_dir: Path, *, force: bool = False, session_id_override: str = "", cwd_override: str = "", dry_run: bool = False):
    source = Path(source_path).expanduser()
    bundle, temp_dir = resolve_bundle_payload(source)
    try:
        if bundle.get("tool") != "claude":
            raise ValueError("Bundle is not a Claude export.")
        session = bundle.get("session", {})
        session_id = session_id_override or str(session.get("session_id") or "")
        if not session_id:
            raise ValueError("Bundle is missing session.session_id.")
        uuid.UUID(session_id)
        project = cwd_override or str(session.get("cwd") or session.get("project") or "")
        if not project:
            raise ValueError("Bundle is missing session working directory/project.")
        project = str(Path(project).expanduser())
        if not os.path.isabs(project):
            raise ValueError("Imported project path must be absolute. Use --import-cwd with an absolute path.")
        transcript_entries = rewritten_raw_entries(bundle, session_id, project)
        if not transcript_entries:
            raise ValueError("Bundle does not contain raw Claude transcript entries.")

        projects_dir = claude_dir / "projects" / encode_project_path(project)
        transcript_path = projects_dir / f"{session_id}.jsonl"
        history_path = claude_dir / "history.jsonl"
        history_entries = bundle_to_history_entries(bundle, project, session_id)
        writes = [str(transcript_path), str(history_path)]

        if transcript_path.exists() and not force:
            raise FileExistsError(f"Transcript already exists: {transcript_path}")

        if dry_run:
            print(json.dumps({
                "session_id": session_id,
                "project": project,
                "transcript_path": str(transcript_path),
                "history_entries": len(history_entries),
                "would_write": writes,
                "warning": "Import dry run only. No files were modified."
            }, indent=2))
            return

        claude_dir.mkdir(parents=True, exist_ok=True)
        projects_dir.mkdir(parents=True, exist_ok=True)
        (projects_dir / "memory").mkdir(parents=True, exist_ok=True)

        atomic_write_text(
            transcript_path,
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in transcript_entries),
            encoding="utf-8"
        )

        existing = []
        if history_path.exists():
            existing = [entry for entry in read_jsonl(history_path) if entry.get("sessionId") != session_id]
        existing.extend(history_entries)
        existing.sort(key=lambda item: int(item.get("timestamp") or 0))
        atomic_write_text(
            history_path,
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in existing),
            encoding="utf-8"
        )
        print(json.dumps({
            "session_id": session_id,
            "project": project,
            "transcript_path": str(transcript_path),
            "history_entries": len(history_entries),
            "wrote": writes,
        }, indent=2))
    finally:
        if temp_dir and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a Claude session bundle.")
    parser.add_argument("--bundle", default="./session.json", help="Path to session.json, bundle directory, or package.zip")
    parser.add_argument("--claude-dir", default=str(Path.home() / ".claude"), help="Destination Claude state directory")
    parser.add_argument("--import-force", action="store_true", help="Overwrite existing imported session data")
    parser.add_argument("--import-session-id", default="", help="Override the imported session ID")
    parser.add_argument("--import-cwd", default="", help="Override the imported working directory")
    parser.add_argument("--import-dry-run", action="store_true", help="Validate the bundle and print the files that would be written")
    args = parser.parse_args()
    import_bundle(
        args.bundle,
        Path(args.claude_dir).expanduser(),
        force=args.import_force,
        session_id_override=args.import_session_id,
        cwd_override=args.import_cwd,
        dry_run=args.import_dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
    )


def render_markdown(bundle: dict) -> str:
    session = bundle["session"]
    metadata = bundle.get("metadata", {})
    analytics = bundle.get("analytics", {})
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
        lines.extend(["", "## Metadata", ""])
        for key, value in metadata.items():
            lines.append(f"- {key}: `{value}`")
    if analytics:
        lines.extend(["", "## Analytics", ""])
        if analytics.get("role_counts"):
            lines.append("- Role counts:")
            for key, value in sorted(analytics["role_counts"].items()):
                lines.append(f"  - {key}: `{value}`")
        if analytics.get("tool_counts"):
            lines.append("- Tool usage:")
            for key, value in sorted(analytics["tool_counts"].items()):
                lines.append(f"  - {key}: `{value}`")
        if analytics.get("commands"):
            lines.append(f"- Shell commands: `{len(analytics['commands'])}`")
        if analytics.get("file_events"):
            lines.append(f"- File events: `{len(analytics['file_events'])}`")
    lines.extend(["", "## Conversation", ""])
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
    analytics = bundle.get("analytics", {})
    metadata_items.extend(
        [
            ("Roles", ", ".join(f"{k}:{v}" for k, v in sorted(analytics.get("role_counts", {}).items())) or "none"),
            ("Tools", ", ".join(f"{k}:{v}" for k, v in sorted(analytics.get("tool_counts", {}).items())) or "none"),
            ("Commands", str(len(analytics.get("commands", [])))),
            ("File events", str(len(analytics.get("file_events", [])))),
        ]
    )

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


def render_bundle_index(bundle: dict) -> str:
    title = html.escape(f"{bundle['tool'].capitalize()} Session {bundle['session']['session_id']}")
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      --page: #eef2f7;
      --surface: #ffffff;
      --surface-alt: #f7f9fc;
      --ink: #18212f;
      --muted: #627489;
      --line: #d7dee8;
      --line-strong: #c2ccda;
      --azure: #0078d4;
      --azure-deep: #005a9e;
      --shadow: 0 8px 28px rgba(15, 23, 42, 0.08);
      --overlay: rgba(17, 24, 39, 0.44);
      --user: #f8fbff;
      --assistant: #fffaf3;
      --system: #f7f8fb;
      --mono: "SF Mono", "JetBrains Mono", "IBM Plex Mono", monospace;
      --sans: "Helvetica Neue", "Neue Haas Grotesk Text Pro", "Avenir Next", "Segoe UI Variable", "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ height: 100%; }}
    body {{
      margin: 0;
      background: linear-gradient(180deg, #f8fbfe 0%, var(--page) 120px, var(--page) 100%);
      color: var(--ink);
      font-family: var(--sans);
    }}
    .portal {{
      min-height: 100vh;
      display: grid;
      grid-template-rows: 56px 1fr;
    }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 0 20px;
      background: linear-gradient(90deg, #0e3a6d, #0f5da8);
      color: #fff;
      box-shadow: 0 2px 10px rgba(15, 23, 42, 0.18);
    }}
    .topbar-title {{
      font-size: 18px;
      font-weight: 600;
      letter-spacing: 0.01em;
    }}
    .topbar-meta {{
      font-size: 12px;
      opacity: 0.82;
    }}
    .layout {{
      display: grid;
      grid-template-columns: 304px minmax(0, 1fr);
      min-height: 0;
    }}
    .sidebar {{
      border-right: 1px solid var(--line);
      background: #f6f8fb;
      padding: 18px 16px;
      overflow: auto;
    }}
    .content {{
      padding: 18px;
      overflow: auto;
      display: grid;
      gap: 18px;
      align-content: start;
    }}
    .section-title {{
      margin: 0 0 10px;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      font-weight: 700;
    }}
    .resource-card, .blade {{
      background: var(--surface);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      border-radius: 6px;
    }}
    .resource-card {{
      padding: 14px;
      display: grid;
      gap: 12px;
    }}
    .resource-name {{
      font-size: 22px;
      font-weight: 600;
      line-height: 1.25;
    }}
    .resource-sub {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }}
    .meta-list {{
      display: grid;
      gap: 8px;
    }}
    .meta-item {{
      padding: 9px 10px;
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 4px;
    }}
    .meta-item strong {{
      display: block;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 3px;
    }}
    .toolbar {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: flex-start;
      flex-wrap: wrap;
    }}
    .toolbar-copy {{
      display: grid;
      gap: 5px;
    }}
    .toolbar-copy h1 {{
      margin: 0;
      font-size: 26px;
      font-weight: 600;
      line-height: 1.2;
    }}
    .toolbar-copy p {{
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
      max-width: 900px;
    }}
    .toolbar-actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .btn {{
      appearance: none;
      border: 1px solid var(--line-strong);
      background: var(--surface);
      color: var(--ink);
      border-radius: 4px;
      padding: 9px 14px;
      font: inherit;
      text-decoration: none;
      cursor: pointer;
      transition: background 120ms ease, border-color 120ms ease, transform 120ms ease;
    }}
    .btn:hover {{
      background: #f8fbff;
      border-color: #b4c2d6;
    }}
    .btn.primary {{
      background: var(--azure);
      color: white;
      border-color: var(--azure);
    }}
    .btn.primary:hover {{
      background: var(--azure-deep);
      border-color: var(--azure-deep);
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 18px;
    }}
    .span-12 {{ grid-column: span 12; }}
    .span-8 {{ grid-column: span 8; }}
    .span-6 {{ grid-column: span 6; }}
    .span-4 {{ grid-column: span 4; }}
    .blade {{
      padding: 16px;
      display: grid;
      gap: 14px;
    }}
    .blade-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
    }}
    .blade-head h2 {{
      margin: 0;
      font-size: 16px;
      font-weight: 600;
    }}
    .blade-head .hint {{
      color: var(--muted);
      font-size: 12px;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
      gap: 10px;
    }}
    .stat {{
      padding: 12px;
      border: 1px solid var(--line);
      background: var(--surface-alt);
      border-radius: 4px;
      min-height: 78px;
    }}
    .stat .label {{
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    .stat .value {{
      margin-top: 8px;
      font-size: 24px;
      font-weight: 600;
    }}
    .bars {{
      display: grid;
      gap: 10px;
    }}
    .bar {{
      display: grid;
      grid-template-columns: 140px 1fr 42px;
      gap: 10px;
      align-items: center;
      font-size: 13px;
    }}
    .track {{
      height: 8px;
      background: #ebf1f7;
      border-radius: 999px;
      overflow: hidden;
    }}
    .fill {{
      height: 100%;
      background: linear-gradient(90deg, #2490ea, #0f6cbd);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }}
    th, td {{
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
    }}
    code {{
      font-family: var(--mono);
      font-size: 12px;
    }}
    .messages {{
      display: grid;
      gap: 12px;
    }}
    .message-card {{
      display: grid;
      gap: 10px;
      padding: 14px 16px;
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 4px;
      transition: border-color 120ms ease, box-shadow 120ms ease;
    }}
    .message-card:hover {{
      border-color: #a6bdd8;
      box-shadow: 0 10px 28px rgba(15, 23, 42, 0.08);
    }}
    .message-card.user {{ background: var(--user); }}
    .message-card.assistant {{ background: var(--assistant); }}
    .message-card.system, .message-card.meta {{ background: var(--system); }}
    .message-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }}
    .message-role {{
      font-weight: 600;
      font-size: 14px;
      text-transform: capitalize;
    }}
    .message-meta {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }}
    .message-preview {{
      color: #253041;
      font-size: 14px;
      line-height: 1.55;
      display: -webkit-box;
      -webkit-line-clamp: 4;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}
    .message-actions {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .muted-note {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }}
    .modal-backdrop {{
      position: fixed;
      inset: 0;
      background: var(--overlay);
      display: none;
      z-index: 100;
      padding: 28px;
      overflow: auto;
    }}
    .modal-backdrop.open {{
      display: block;
    }}
    .modal {{
      width: min(1100px, calc(100vw - 56px));
      min-height: min(640px, calc(100vh - 56px));
      margin: 0 auto;
      display: grid;
      grid-template-rows: auto auto 1fr;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 32px 80px rgba(15, 23, 42, 0.26);
      overflow: hidden;
    }}
    .modal-header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      padding: 18px 20px 14px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfe;
    }}
    .modal-header h3 {{
      margin: 0;
      font-size: 20px;
      font-weight: 600;
    }}
    .modal-sub {{
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }}
    .modal-tabs {{
      display: flex;
      gap: 0;
      border-bottom: 1px solid var(--line);
      background: #f7f9fc;
      padding: 0 16px;
    }}
    .tab {{
      border: 0;
      border-bottom: 2px solid transparent;
      background: transparent;
      color: var(--muted);
      padding: 12px 14px 11px;
      font: inherit;
      cursor: pointer;
    }}
    .tab.active {{
      color: var(--azure-deep);
      border-bottom-color: var(--azure);
      font-weight: 600;
    }}
    .modal-body {{
      min-height: 0;
      overflow: auto;
      padding: 18px 20px 22px;
      background: #fff;
    }}
    .tab-panel {{
      display: none;
    }}
    .tab-panel.active {{
      display: block;
    }}
    .markdown {{
      color: #243140;
      font-size: 15px;
      line-height: 1.72;
    }}
    .markdown h1, .markdown h2, .markdown h3 {{
      margin: 1.2em 0 0.45em;
      font-weight: 600;
      line-height: 1.3;
    }}
    .markdown p, .markdown ul, .markdown ol, .markdown pre, .markdown blockquote, .markdown table {{
      margin: 0 0 1em;
    }}
    .markdown pre {{
      padding: 14px;
      border: 1px solid #dbe4ef;
      background: #f7f9fc;
      border-radius: 4px;
      overflow: auto;
    }}
    .markdown code {{
      font-family: var(--mono);
      background: #f3f6fa;
      padding: 2px 4px;
      border-radius: 3px;
    }}
    .markdown pre code {{
      background: transparent;
      padding: 0;
    }}
    .markdown blockquote {{
      padding: 8px 0 8px 14px;
      border-left: 3px solid #8fc1ef;
      color: #50657d;
      background: #f7fbff;
    }}
    .markdown table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    .markdown th, .markdown td {{
      border: 1px solid var(--line);
      padding: 8px 10px;
    }}
    .properties-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 10px;
    }}
    .property {{
      padding: 11px 12px;
      border: 1px solid var(--line);
      background: var(--surface-alt);
      border-radius: 4px;
    }}
    .property strong {{
      display: block;
      margin-bottom: 4px;
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    @media (max-width: 1080px) {{
      .layout {{ grid-template-columns: 1fr; }}
      .sidebar {{ border-right: 0; border-bottom: 1px solid var(--line); }}
      .span-8, .span-6, .span-4 {{ grid-column: span 12; }}
    }}
    @media (max-width: 720px) {{
      .content, .sidebar {{ padding: 14px; }}
      .modal-backdrop {{ padding: 10px; }}
      .modal {{ width: 100%; min-height: calc(100vh - 20px); }}
      .bar {{ grid-template-columns: 96px 1fr 32px; }}
    }}
  </style>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
</head>
<body>
  <div class="portal">
    <header class="topbar">
      <div>
        <div class="topbar-title">{title}</div>
        <div class="topbar-meta">Session resource bundle</div>
      </div>
      <div class="topbar-meta">Static package · local or tunneled viewing</div>
    </header>
    <div class="layout">
      <aside class="sidebar">
        <div class="section-title">Resource</div>
        <section class="resource-card">
          <div class="resource-name">{title}</div>
          <div class="resource-sub">Portal-style operational view for a single exported session. Interactive messages are isolated in a modal. Structural metadata stays in muted property surfaces rather than mixing into the main transcript.</div>
        </section>
        <div class="section-title" style="margin-top:16px;">Session Properties</div>
        <div id="session-meta" class="meta-list"></div>
        <div class="section-title" style="margin-top:16px;">Bundle Metadata</div>
        <div id="bundle-meta" class="meta-list"></div>
      </aside>
      <main class="content">
        <section class="blade">
          <div class="toolbar">
            <div class="toolbar-copy">
              <h1>Overview</h1>
              <p>Operations-oriented summary of the exported session. Use the conversation blade below to open individual messages in a modal with rendered Markdown and muted technical properties.</p>
            </div>
            <div class="toolbar-actions">
              <a class="btn primary" href="./package.zip" download>Download package zip</a>
              <a class="btn" href="./session.json" download>Download JSON</a>
              <a class="btn" href="./transcript.jsonl" download>Download transcript</a>
              <a class="btn" href="./README.md" download>Download README</a>
              {importer_link}
            </div>
          </div>
          <div id="overview" class="stats"></div>
        </section>

        <section class="grid">
          <div class="blade span-6">
            <div class="blade-head">
              <h2>Role Distribution</h2>
              <div class="hint">Top-level transcript composition</div>
            </div>
            <div id="role-bars" class="bars"></div>
          </div>
          <div class="blade span-6">
            <div class="blade-head">
              <h2>Tool Usage</h2>
              <div class="hint">Most frequent invoked tools</div>
            </div>
            <div id="tool-bars" class="bars"></div>
          </div>
          <div class="blade span-6">
            <div class="blade-head">
              <h2>Command Activity</h2>
              <div class="hint">Recent shell and tool command executions</div>
            </div>
            <table>
              <thead><tr><th>Time</th><th>Command</th><th>Exit</th></tr></thead>
              <tbody id="commands-body"></tbody>
            </table>
          </div>
          <div class="blade span-6">
            <div class="blade-head">
              <h2>File Activity</h2>
              <div class="hint">Recent file read, patch, edit, and write events</div>
            </div>
            <table>
              <thead><tr><th>Time</th><th>Kind</th><th>Path</th></tr></thead>
              <tbody id="files-body"></tbody>
            </table>
          </div>
        </section>

        <section class="blade">
          <div class="blade-head">
            <div>
              <h2>Conversation</h2>
              <div class="hint">Open a message to inspect rich content. Metadata does not render inline as if it were a conversational turn.</div>
            </div>
          </div>
          <div id="messages" class="messages"></div>
        </section>
      </main>
    </div>
  </div>

  <div id="message-modal-shell" class="modal-backdrop" aria-hidden="true">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-header">
        <div>
          <h3 id="modal-title">Conversation message</h3>
          <div id="modal-subtitle" class="modal-sub"></div>
        </div>
        <button id="modal-close" class="btn" type="button">Close</button>
      </div>
      <div class="modal-tabs">
        <button class="tab active" data-tab="content" type="button">Content</button>
        <button class="tab" data-tab="properties" type="button">Properties</button>
      </div>
      <div class="modal-body">
        <section id="panel-content" class="tab-panel active">
          <div id="modal-markdown" class="markdown"></div>
        </section>
        <section id="panel-properties" class="tab-panel">
          <div id="modal-properties" class="properties-grid"></div>
        </section>
      </div>
    </div>
  </div>

  <script>
    let currentMessages = [];
    let previousActive = null;

    async function main() {{
      const response = await fetch("./session.json");
      const data = await response.json();
      const session = data.session || {{}};
      const analytics = data.analytics || {{}};
      currentMessages = data.messages || [];
      fillMeta("session-meta", {{
        "Session ID": session.session_id,
        "Started at": session.started_at,
        "Last activity": session.last_activity,
        "Working directory": session.cwd,
        "Transcript path": session.transcript_path
      }});
      fillMeta("bundle-meta", data.metadata || {{}});
      fillOverview(analytics, currentMessages.length);
      fillBars("role-bars", analytics.role_counts || {{}});
      fillBars("tool-bars", analytics.tool_counts || {{}});
      fillCommands(analytics.commands || []);
      fillFiles(analytics.file_events || []);
      fillMessages(currentMessages);
      wireModal();
      configureMarkdown();
    }}

    function configureMarkdown() {{
      if (window.marked) {{
        marked.setOptions({{
          breaks: true,
          gfm: true,
          headerIds: false,
          mangle: false
        }});
      }}
    }}

    function fillMeta(id, obj) {{
      const root = document.getElementById(id);
      root.innerHTML = "";
      for (const [key, value] of Object.entries(obj)) {{
        const item = document.createElement("div");
        item.className = "meta-item";
        item.innerHTML = `<strong>${{escapeHtml(key)}}</strong><div>${{escapeHtml(String(value ?? "unknown"))}}</div>`;
        root.appendChild(item);
      }}
    }}

    function fillOverview(analytics, messageCount) {{
      const stats = {{
        Messages: messageCount,
        Tools: Object.keys(analytics.tool_counts || {{}}).length,
        Commands: (analytics.commands || []).length,
        "File events": (analytics.file_events || []).length
      }};
      const root = document.getElementById("overview");
      root.innerHTML = "";
      for (const [label, value] of Object.entries(stats)) {{
        const node = document.createElement("div");
        node.className = "stat";
        node.innerHTML = `<div class="label">${{escapeHtml(label)}}</div><div class="value">${{escapeHtml(String(value))}}</div>`;
        root.appendChild(node);
      }}
    }}

    function fillBars(id, obj) {{
      const root = document.getElementById(id);
      const entries = Object.entries(obj).sort((a, b) => b[1] - a[1]).slice(0, 12);
      root.innerHTML = "";
      if (!entries.length) {{
        root.innerHTML = `<div class="muted-note">No data.</div>`;
        return;
      }}
      const max = Math.max(...entries.map(([, value]) => value), 1);
      for (const [label, value] of entries) {{
        const row = document.createElement("div");
        row.className = "bar";
        row.innerHTML = `
          <div>${{escapeHtml(label)}}</div>
          <div class="track"><div class="fill" style="width:${{(value / max) * 100}}%"></div></div>
          <div>${{escapeHtml(String(value))}}</div>`;
        root.appendChild(row);
      }}
    }}

    function fillCommands(commands) {{
      const body = document.getElementById("commands-body");
      body.innerHTML = "";
      for (const item of commands.slice(0, 40)) {{
        const row = document.createElement("tr");
        row.innerHTML = `<td>${{escapeHtml(item.timestamp || "unknown")}}</td><td><code>${{escapeHtml(item.command || "")}}</code></td><td>${{escapeHtml(String(item.exit_code ?? ""))}}</td>`;
        body.appendChild(row);
      }}
    }}

    function fillFiles(files) {{
      const body = document.getElementById("files-body");
      body.innerHTML = "";
      for (const item of files.slice(0, 60)) {{
        const row = document.createElement("tr");
        row.innerHTML = `<td>${{escapeHtml(item.timestamp || "unknown")}}</td><td>${{escapeHtml(item.kind || "")}}</td><td><code>${{escapeHtml(item.path || "")}}</code></td>`;
        body.appendChild(row);
      }}
    }}

    function fillMessages(messages) {{
      const root = document.getElementById("messages");
      root.innerHTML = "";
      messages.forEach((message, index) => {{
        const node = document.createElement("article");
        node.className = `message-card ${{message.role || "meta"}}`;
        const preview = escapeHtml((message.text || "(no text extracted)").trim().slice(0, 340));
        node.innerHTML = `
          <div class="message-head">
            <div>
              <div class="message-role">${{escapeHtml(message.role || "meta")}}</div>
              <div class="message-meta">${{escapeHtml(message.timestamp || "unknown")}} · ${{escapeHtml(message.kind || "message")}}</div>
            </div>
            <div class="message-actions">
              <button class="btn copy-message" type="button" data-index="${{index}}">Copy</button>
              <button class="btn primary open-message" type="button" data-index="${{index}}">Open</button>
            </div>
          </div>
          <div class="message-preview">${{preview}}</div>`;
        root.appendChild(node);
      }});

      root.querySelectorAll(".copy-message").forEach((button) => {{
        button.addEventListener("click", async () => {{
          const message = currentMessages[Number(button.dataset.index)];
          await navigator.clipboard.writeText(message?.text || "");
          const old = button.textContent;
          button.textContent = "Copied";
          setTimeout(() => button.textContent = old, 1200);
        }});
      }});

      root.querySelectorAll(".open-message").forEach((button) => {{
        button.addEventListener("click", () => openModal(Number(button.dataset.index), button));
      }});
    }}

    function wireModal() {{
      const shell = document.getElementById("message-modal-shell");
      document.getElementById("modal-close").addEventListener("click", closeModal);
      shell.addEventListener("click", (event) => {{
        if (event.target === shell) closeModal();
      }});
      document.addEventListener("keydown", (event) => {{
        if (event.key === "Escape" && shell.classList.contains("open")) closeModal();
      }});
      document.querySelectorAll(".tab").forEach((tab) => {{
        tab.addEventListener("click", () => setActiveTab(tab.dataset.tab));
      }});
    }}

    function openModal(index, sourceButton) {{
      const message = currentMessages[index];
      if (!message) return;
      previousActive = sourceButton || document.activeElement;
      document.getElementById("modal-title").textContent = `${{capitalize(message.role || "meta")}} message`;
      document.getElementById("modal-subtitle").textContent = `${{message.timestamp || "unknown"}} · ${{message.kind || "message"}}`;
      document.getElementById("modal-markdown").innerHTML = renderMarkdown(message.text || "");
      fillProperties(message);
      setActiveTab("content");
      const shell = document.getElementById("message-modal-shell");
      shell.classList.add("open");
      shell.setAttribute("aria-hidden", "false");
      document.body.style.overflow = "hidden";
      document.getElementById("modal-close").focus();
    }}

    function closeModal() {{
      const shell = document.getElementById("message-modal-shell");
      shell.classList.remove("open");
      shell.setAttribute("aria-hidden", "true");
      document.body.style.overflow = "";
      if (previousActive && typeof previousActive.focus === "function") previousActive.focus();
    }}

    function setActiveTab(name) {{
      document.querySelectorAll(".tab").forEach((tab) => {{
        tab.classList.toggle("active", tab.dataset.tab === name);
      }});
      document.getElementById("panel-content").classList.toggle("active", name === "content");
      document.getElementById("panel-properties").classList.toggle("active", name === "properties");
    }}

    function fillProperties(message) {{
      const root = document.getElementById("modal-properties");
      root.innerHTML = "";
      const properties = {{
        Role: message.role || "meta",
        Timestamp: message.timestamp || "unknown",
        Kind: message.kind || "message",
        "Raw type": message.raw_type || ""
      }};
      Object.entries(message.extra || {{}}).forEach(([key, value]) => {{
        properties[key] = value;
      }});
      for (const [label, value] of Object.entries(properties)) {{
        if (value === "" || value == null) continue;
        const node = document.createElement("div");
        node.className = "property";
        node.innerHTML = `<strong>${{escapeHtml(label)}}</strong><div>${{escapeHtml(String(value))}}</div>`;
        root.appendChild(node);
      }}
    }}

    function renderMarkdown(input) {{
      const source = String(input || "");
      if (window.marked && window.DOMPurify) {{
        const raw = marked.parse(source);
        return DOMPurify.sanitize(raw, {{
          USE_PROFILES: {{ html: true }},
          ALLOWED_ATTR: ["href", "title", "target", "rel"]
        }});
      }}
      return `<pre>${{escapeHtml(source)}}</pre>`;
    }}

    function capitalize(value) {{
      return value ? value.charAt(0).toUpperCase() + value.slice(1) : "Meta";
    }}

    function escapeHtml(value) {{
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }}

    main();
  </script>
</body>
</html>
""".format(
        title=title,
        importer_link='<a class="btn" href="./import_claude_bundle.py" download>Download importer</a>'
        if bundle.get("tool") == "claude"
        else "",
    )


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _pid_is_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def register_active_share(
    *,
    tool: str,
    session_id: str,
    mode: str,
    bundle_dir: Path,
    port: int,
    server_pid: int,
    keep_bundle: bool,
    public_url: str | None = None,
    tunnel_pid: int | None = None,
) -> Path:
    SHARE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = SHARE_STATE_DIR / f"{tool}-{session_id}-{server_pid}.json"
    payload = {
        "schema_version": 1,
        "tool": tool,
        "session_id": session_id,
        "mode": mode,
        "started_at": now_iso(),
        "bundle_dir": str(bundle_dir),
        "keep_bundle": keep_bundle,
        "port": port,
        "local_url": f"http://127.0.0.1:{port}/",
        "public_url": public_url,
        "server_pid": server_pid,
        "tunnel_pid": tunnel_pid,
    }
    state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return state_path


def update_active_share(state_path: Path | None, **updates: object) -> None:
    if not state_path or not state_path.exists():
        return
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    payload.update(updates)
    state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def remove_active_share(state_path: Path | None) -> None:
    if not state_path:
        return
    try:
        state_path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def list_active_shares(tool: str | None = None) -> list[dict]:
    if not SHARE_STATE_DIR.exists():
        return []
    shares: list[dict] = []
    for state_path in sorted(SHARE_STATE_DIR.glob("*.json")):
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            remove_active_share(state_path)
            continue
        if tool and payload.get("tool") != tool:
            continue
        server_pid = payload.get("server_pid")
        if not isinstance(server_pid, int) or not _pid_is_alive(server_pid):
            remove_active_share(state_path)
            continue
        tunnel_pid = payload.get("tunnel_pid")
        payload["tunnel_alive"] = isinstance(tunnel_pid, int) and _pid_is_alive(tunnel_pid)
        payload["state_path"] = str(state_path)
        shares.append(payload)
    shares.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
    return shares


def _terminate_pid(pid: int | None) -> None:
    if not pid or pid <= 0:
        return
    try:
        os.kill(pid, 15)
    except OSError:
        return
    deadline = time.time() + 5
    while time.time() < deadline:
        if not _pid_is_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, 9)
    except OSError:
        return


def stop_active_share(share: dict) -> None:
    tunnel_pid = share.get("tunnel_pid")
    server_pid = share.get("server_pid")
    bundle_dir = share.get("bundle_dir")
    keep_bundle = bool(share.get("keep_bundle", True))
    state_path = share.get("state_path")
    if isinstance(tunnel_pid, int):
        _terminate_pid(tunnel_pid)
    if isinstance(server_pid, int):
        _terminate_pid(server_pid)
    if isinstance(state_path, str):
        remove_active_share(Path(state_path))
    if not keep_bundle and isinstance(bundle_dir, str):
        shutil.rmtree(bundle_dir, ignore_errors=True)


class ServedBundle:
    def __init__(
        self,
        bundle_dir: Path,
        port: int,
        server_process: subprocess.Popen[str],
        tunnel_process: subprocess.Popen[str] | None = None,
        public_url: str | None = None,
        keep_bundle: bool = True,
        share_state_path: Path | None = None,
    ) -> None:
        self.bundle_dir = bundle_dir
        self.port = port
        self.server_process = server_process
        self.tunnel_process = tunnel_process
        self.public_url = public_url
        self.keep_bundle = keep_bundle
        self.share_state_path = share_state_path
        self._closed = False

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for proc in (self.tunnel_process, self.server_process):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        remove_active_share(self.share_state_path)
        if not self.keep_bundle and self.bundle_dir.exists():
            shutil.rmtree(self.bundle_dir, ignore_errors=True)


def start_static_server(bundle_dir: Path, port: int | None = None) -> tuple[int, subprocess.Popen[str]]:
    actual_port = port or find_free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(actual_port), "--bind", "127.0.0.1", "--directory", str(bundle_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    time.sleep(0.4)
    if proc.poll() is not None:
        raise RuntimeError("Failed to start local HTTP server.")
    return actual_port, proc


def start_cloudflare_tunnel(local_url: str) -> tuple[subprocess.Popen[str], str]:
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", local_url, "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    deadline = time.time() + 25
    captured: list[str] = []
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            time.sleep(0.1)
            continue
        captured.append(line)
        match = TRYCLOUDFLARE_RE.search(line)
        if match:
            return proc, match.group(0)
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    joined = "".join(captured).strip()
    raise RuntimeError(f"Failed to acquire Cloudflare tunnel URL. Output: {joined[:500]}")


def serve_bundle(
    bundle_dir: Path,
    with_tunnel: bool = False,
    keep_bundle: bool = True,
    *,
    tool: str = "",
    session_id: str = "",
) -> ServedBundle:
    port, server_process = start_static_server(bundle_dir)
    tunnel_process = None
    public_url = None
    share_state_path = None
    try:
        if tool and session_id:
            share_state_path = register_active_share(
                tool=tool,
                session_id=session_id,
                mode="tunnel" if with_tunnel else "serve",
                bundle_dir=bundle_dir,
                port=port,
                server_pid=server_process.pid,
                keep_bundle=keep_bundle,
            )
        if with_tunnel:
            tunnel_process, public_url = start_cloudflare_tunnel(f"http://127.0.0.1:{port}")
            update_active_share(
                share_state_path,
                public_url=public_url,
                tunnel_pid=tunnel_process.pid,
            )
        served = ServedBundle(
            bundle_dir=bundle_dir,
            port=port,
            server_process=server_process,
            tunnel_process=tunnel_process,
            public_url=public_url,
            keep_bundle=keep_bundle,
            share_state_path=share_state_path,
        )
        atexit.register(served.close)
        return served
    except Exception:
        remove_active_share(share_state_path)
        if tunnel_process and tunnel_process.poll() is None:
            tunnel_process.terminate()
        if server_process.poll() is None:
            server_process.terminate()
        raise
