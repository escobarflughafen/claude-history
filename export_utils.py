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
import time
from pathlib import Path


SCHEMA_VERSION = 1
TRYCLOUDFLARE_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


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
    session_json.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    index_html.write_text(render_bundle_index(bundle), encoding="utf-8")
    return bundle_dir


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
      --bg: #f4efe7;
      --panel: #fffaf3;
      --ink: #201a15;
      --muted: #6e655b;
      --line: #ddcfbf;
      --accent: #a24b1f;
      --user: #d8ebff;
      --assistant: #f8e2c2;
      --system: #ebe4da;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top right, #fff1cc 0, transparent 30%),
        linear-gradient(170deg, #f4efe7, #ece1d2);
    }}
    .shell {{
      display: grid;
      grid-template-columns: 320px minmax(0, 1fr);
      min-height: 100vh;
    }}
    aside {{
      padding: 22px;
      border-right: 1px solid var(--line);
      background: rgba(255, 250, 243, 0.82);
      backdrop-filter: blur(12px);
      overflow: auto;
    }}
    main {{
      padding: 22px;
      display: grid;
      gap: 20px;
      overflow: auto;
    }}
    h1, h2, h3 {{ margin: 0 0 12px; }}
    .stack {{ display: grid; gap: 12px; }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px 16px;
      box-shadow: 0 10px 26px rgba(32, 26, 21, 0.06);
    }}
    .stat-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
      gap: 10px;
    }}
    .stat {{
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 10px 12px;
      background: #fffdf8;
    }}
    .label {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    .value {{
      margin-top: 4px;
      font-size: 22px;
      font-weight: 700;
    }}
    ul.meta {{
      list-style: none;
      padding: 0;
      margin: 0;
      display: grid;
      gap: 8px;
    }}
    ul.meta li {{
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 9px 10px;
      background: #fffdf8;
    }}
    ul.meta strong {{
      display: block;
      margin-bottom: 4px;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .bars {{
      display: grid;
      gap: 8px;
    }}
    .bar {{
      display: grid;
      grid-template-columns: 180px 1fr 48px;
      gap: 10px;
      align-items: center;
    }}
    .track {{
      height: 10px;
      background: #eadfce;
      border-radius: 999px;
      overflow: hidden;
    }}
    .fill {{
      height: 100%;
      background: linear-gradient(90deg, #d06d2d, #9b3d17);
    }}
    .timeline {{
      display: grid;
      gap: 12px;
    }}
    .msg {{
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 13px 14px;
      background: #fffdf8;
    }}
    .msg.user {{ background: var(--user); }}
    .msg.assistant {{ background: var(--assistant); }}
    .msg.system, .msg.meta {{ background: var(--system); }}
    .msg header {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 13px;
    }}
    .msg .role {{
      font-weight: 700;
      text-transform: capitalize;
      color: var(--ink);
    }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: "Iosevka", "IBM Plex Mono", monospace;
      font-size: 13px;
      line-height: 1.45;
    }}
    .row {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 18px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      padding: 8px 6px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    code {{
      font-family: "Iosevka", "IBM Plex Mono", monospace;
      font-size: 12px;
    }}
    @media (max-width: 960px) {{
      .shell {{ grid-template-columns: 1fr; }}
      aside {{ border-right: 0; border-bottom: 1px solid var(--line); }}
      .bar {{ grid-template-columns: 120px 1fr 42px; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <aside class="stack">
      <div>
        <h1>{title}</h1>
        <div class="label">Bundle Viewer</div>
      </div>
      <div class="card">
        <h3>Session</h3>
        <ul id="session-meta" class="meta"></ul>
      </div>
      <div class="card">
        <h3>Metadata</h3>
        <ul id="bundle-meta" class="meta"></ul>
      </div>
    </aside>
    <main>
      <section class="card">
        <h2>Overview</h2>
        <div id="overview" class="stat-grid"></div>
      </section>
      <div class="row">
        <section class="card">
          <h2>Roles</h2>
          <div id="role-bars" class="bars"></div>
        </section>
        <section class="card">
          <h2>Tool Usage</h2>
          <div id="tool-bars" class="bars"></div>
        </section>
      </div>
      <div class="row">
        <section class="card">
          <h2>Commands</h2>
          <table>
            <thead><tr><th>Time</th><th>Command</th><th>Exit</th></tr></thead>
            <tbody id="commands-body"></tbody>
          </table>
        </section>
        <section class="card">
          <h2>File Activity</h2>
          <table>
            <thead><tr><th>Time</th><th>Kind</th><th>Path</th></tr></thead>
            <tbody id="files-body"></tbody>
          </table>
        </section>
      </div>
      <section class="card">
        <h2>Conversation</h2>
        <div id="timeline" class="timeline"></div>
      </section>
    </main>
  </div>
  <script>
    async function main() {{
      const response = await fetch("./session.json");
      const data = await response.json();
      const session = data.session || {{}};
      const analytics = data.analytics || {{}};
      const messages = data.messages || [];
      fillMeta("session-meta", {{
        "Session ID": session.session_id,
        "Started at": session.started_at,
        "Last activity": session.last_activity,
        "Working directory": session.cwd,
        "Transcript path": session.transcript_path,
      }});
      fillMeta("bundle-meta", data.metadata || {{}});
      fillOverview(analytics, messages.length);
      fillBars("role-bars", analytics.role_counts || {{}});
      fillBars("tool-bars", analytics.tool_counts || {{}});
      fillCommands(analytics.commands || []);
      fillFiles(analytics.file_events || []);
      fillTimeline(messages);
    }}

    function fillMeta(id, obj) {{
      const root = document.getElementById(id);
      root.innerHTML = "";
      for (const [key, value] of Object.entries(obj)) {{
        const li = document.createElement("li");
        li.innerHTML = `<strong>${{escapeHtml(key)}}</strong><span>${{escapeHtml(String(value ?? "unknown"))}}</span>`;
        root.appendChild(li);
      }}
    }}

    function fillOverview(analytics, messageCount) {{
      const stats = {{
        Messages: messageCount,
        Tools: Object.keys(analytics.tool_counts || {{}}).length,
        Commands: (analytics.commands || []).length,
        "File events": (analytics.file_events || []).length,
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
      root.innerHTML = "";
      const entries = Object.entries(obj).sort((a, b) => b[1] - a[1]).slice(0, 12);
      const max = Math.max(...entries.map(([, value]) => value), 1);
      if (!entries.length) {{
        root.textContent = "No data.";
        return;
      }}
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

    function fillTimeline(messages) {{
      const root = document.getElementById("timeline");
      root.innerHTML = "";
      for (const message of messages) {{
        const node = document.createElement("article");
        node.className = `msg ${{message.role || "meta"}}`;
        const extra = Object.entries(message.extra || {{}})
          .map(([k, v]) => `${{escapeHtml(k)}}: ${{escapeHtml(String(v))}}`)
          .join(" · ");
        node.innerHTML = `
          <header>
            <span class="role">${{escapeHtml(message.role || "meta")}}</span>
            <span>${{escapeHtml(message.timestamp || "unknown")}} · ${{escapeHtml(message.kind || "message")}}${{message.raw_type ? " · " + escapeHtml(message.raw_type) : ""}}${{extra ? " · " + extra : ""}}</span>
          </header>
          <pre>${{escapeHtml(message.text || "(no text extracted)")}}</pre>`;
        root.appendChild(node);
      }}
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
""".format(title=title)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


class ServedBundle:
    def __init__(
        self,
        bundle_dir: Path,
        port: int,
        server_process: subprocess.Popen[str],
        tunnel_process: subprocess.Popen[str] | None = None,
        public_url: str | None = None,
        keep_bundle: bool = True,
    ) -> None:
        self.bundle_dir = bundle_dir
        self.port = port
        self.server_process = server_process
        self.tunnel_process = tunnel_process
        self.public_url = public_url
        self.keep_bundle = keep_bundle
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


def serve_bundle(bundle_dir: Path, with_tunnel: bool = False, keep_bundle: bool = True) -> ServedBundle:
    port, server_process = start_static_server(bundle_dir)
    tunnel_process = None
    public_url = None
    try:
        if with_tunnel:
            tunnel_process, public_url = start_cloudflare_tunnel(f"http://127.0.0.1:{port}")
        served = ServedBundle(
            bundle_dir=bundle_dir,
            port=port,
            server_process=server_process,
            tunnel_process=tunnel_process,
            public_url=public_url,
            keep_bundle=keep_bundle,
        )
        atexit.register(served.close)
        return served
    except Exception:
        if tunnel_process and tunnel_process.poll() is None:
            tunnel_process.terminate()
        if server_process.poll() is None:
            server_process.terminate()
        raise
