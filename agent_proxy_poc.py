#!/usr/bin/env python3
"""Minimal local web proxy POC for Claude/Codex-style supervised sessions."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import http.server
import json
import os
import pty
import secrets
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
import fcntl
import termios
import struct
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from export_utils import start_cloudflare_tunnel


POC_ROOT = Path(tempfile.gettempdir()) / "agent-proxy-poc"
SESSIONS: dict[str, dict[str, Any]] = {}
LOCK = threading.Lock()
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat()


def debug_log(message: str) -> None:
    print(f"[agent-proxy-poc {now_iso()}] {message}", flush=True)


def safe_rel_path(value: str) -> list[str]:
    parts = [part for part in Path(value).parts if part not in {"", ".", ".."}]
    return parts


def workspace_tree(root: Path) -> list[dict[str, Any]]:
    def build(path: Path) -> dict[str, Any]:
        children = []
        if path.is_dir():
            for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
                children.append(build(child))
        return {
            "name": path.name,
            "type": "dir" if path.is_dir() else "file",
            "children": children,
        }

    if not root.exists():
        return []
    return [build(child) for child in sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))]


def session_snapshot(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": session["id"],
        "tool": session["tool"],
        "title": session["title"],
        "workspace_dir": str(session["workspace_dir"]),
        "created_at": session["created_at"],
        "messages": session["messages"],
        "commands": session["commands"],
        "files": workspace_tree(session["workspace_dir"]),
        "tty": {
            "active": bool(session.get("tty")),
            "title": session.get("tty_title", ""),
            "cursor": int(session.get("tty_seq", 0)),
        },
    }


def _append_tty_output(session: dict[str, Any], data: str) -> None:
    if not data:
        return
    session["tty_seq"] = int(session.get("tty_seq", 0)) + 1
    session.setdefault("tty_chunks", []).append({"seq": session["tty_seq"], "data": data})
    session["tty_chunks"] = session["tty_chunks"][-2000:]
    broadcast_tty_message(session, {"type": "output", "data": data, "cursor": session["tty_seq"]})


def broadcast_tty_message(session: dict[str, Any], payload: dict[str, Any]) -> None:
    stale: list[WebSocketPeer] = []
    for peer in list(session.get("tty_clients", [])):
        try:
            peer.send_json(payload)
        except Exception as exc:
            debug_log(f"tty websocket send failed for session={session['id']}: {exc}")
            stale.append(peer)
    for peer in stale:
        session.get("tty_clients", set()).discard(peer)


def broadcast_tty_status(session: dict[str, Any]) -> None:
    tty = session.get("tty") or {}
    broadcast_tty_message(
        session,
        {
            "type": "status",
            "alive": bool(tty.get("alive")),
            "title": session.get("tty_title", ""),
            "cursor": int(session.get("tty_seq", 0)),
        },
    )


def _tty_reader(session_id: str) -> None:
    debug_log(f"tty reader started for session={session_id}")
    while True:
        with LOCK:
            session = SESSIONS.get(session_id)
            if not session or not session.get("tty"):
                debug_log(f"tty reader stopping for session={session_id}: no session or tty state")
                return
            master_fd = session["tty"]["master_fd"]
            pid = session["tty"]["pid"]
        try:
            data = os.read(master_fd, 4096)
        except BlockingIOError:
            time.sleep(0.05)
            continue
        except OSError as exc:
            debug_log(f"tty read error for session={session_id}: {exc}")
            data = b""
        if not data:
            exit_status = None
            try:
                waited_pid, status = os.waitpid(pid, os.WNOHANG)
                if waited_pid == pid:
                    if os.WIFEXITED(status):
                        exit_status = f"exit {os.WEXITSTATUS(status)}"
                    elif os.WIFSIGNALED(status):
                        exit_status = f"signal {os.WTERMSIG(status)}"
            except ChildProcessError:
                exit_status = "already reaped"
            with LOCK:
                session = SESSIONS.get(session_id)
                if session and session.get("tty"):
                    session["tty"]["alive"] = False
                    _append_tty_output(session, "\r\n[session ended]\r\n")
                    broadcast_tty_status(session)
            debug_log(f"tty eof for session={session_id} pid={pid} status={exit_status or 'unknown'}")
            return
        text = data.decode("utf-8", errors="replace")
        with LOCK:
            session = SESSIONS.get(session_id)
            if not session:
                debug_log(f"tty reader stopping for session={session_id}: session removed")
                return
            _append_tty_output(session, text)


def start_tty_session(session: dict[str, Any], mode: str) -> None:
    if session.get("tty") and session["tty"].get("alive"):
        debug_log(f"tty start ignored for session={session['id']}: already alive")
        return
    shell_path = os.environ.get("SHELL", "/bin/bash")
    debug_log(
        f"tty start requested for session={session['id']} mode={mode} "
        f"workspace={session['workspace_dir']} shell={shell_path}"
    )
    pid, master_fd = pty.fork()
    if pid == 0:
        os.chdir(session["workspace_dir"])
        os.environ.setdefault("TERM", "xterm-256color")
        if mode == "shell":
            os.execvp(shell_path, [shell_path, "-i"])
        command = {
            "claude": "claude",
            "codex": "codex",
        }.get(mode, shell_path)
        fallback = f'exec {command} || {{ printf "\\r\\n[{command} exited or was unavailable]\\r\\n"; exec {shell_path} -i; }}'
        os.execvp(shell_path, [shell_path, "-lc", fallback])
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    session["tty"] = {"pid": pid, "master_fd": master_fd, "alive": True}
    session["tty_title"] = mode
    session["tty_seq"] = 0
    session["tty_chunks"] = []
    thread = threading.Thread(target=_tty_reader, args=(session["id"],), daemon=True)
    session["tty_thread"] = thread
    thread.start()
    broadcast_tty_status(session)
    debug_log(f"tty started for session={session['id']} pid={pid} mode={mode}")


def tty_resize(session: dict[str, Any], cols: int, rows: int) -> None:
    tty = session.get("tty")
    if not tty:
        return
    payload = struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0)
    try:
        fcntl.ioctl(tty["master_fd"], termios.TIOCSWINSZ, payload)
    except OSError:
        return
    broadcast_tty_message(session, {"type": "resize", "cols": max(cols, 1), "rows": max(rows, 1)})
    debug_log(f"tty resized for session={session['id']} cols={max(cols, 1)} rows={max(rows, 1)}")


def tty_write(session: dict[str, Any], data: str) -> None:
    tty = session.get("tty")
    if not tty or not tty.get("alive"):
        debug_log(f"tty input ignored for session={session['id']}: no live tty")
        return
    try:
        os.write(tty["master_fd"], data.encode("utf-8", errors="replace"))
    except OSError:
        tty["alive"] = False
        debug_log(f"tty input write failed for session={session['id']}")


def tty_poll(session: dict[str, Any], cursor: int) -> dict[str, Any]:
    chunks = [item for item in session.get("tty_chunks", []) if int(item.get("seq", 0)) > cursor]
    tty = session.get("tty") or {}
    return {
        "cursor": int(session.get("tty_seq", 0)),
        "chunks": chunks,
        "alive": bool(tty.get("alive")),
        "title": session.get("tty_title", ""),
    }


class WebSocketPeer:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.send_lock = threading.Lock()
        self.alive = True

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other

    def send_json(self, payload: dict[str, Any]) -> None:
        self.send_text(json.dumps(payload, ensure_ascii=False))

    def send_text(self, text: str) -> None:
        if not self.alive:
            raise ConnectionError("websocket closed")
        data = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(data)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(length.to_bytes(2, "big"))
        else:
            header.append(127)
            header.extend(length.to_bytes(8, "big"))
        with self.send_lock:
            self.connection.sendall(header + data)

    def close(self) -> None:
        if not self.alive:
            return
        self.alive = False
        try:
            self.connection.sendall(b"\x88\x00")
        except OSError:
            pass


def read_ws_frame(rfile: Any) -> tuple[int, bytes]:
    first = rfile.read(2)
    if len(first) < 2:
        raise EOFError("websocket closed")
    opcode = first[0] & 0x0F
    masked = bool(first[1] & 0x80)
    length = first[1] & 0x7F
    if length == 126:
        length = int.from_bytes(rfile.read(2), "big")
    elif length == 127:
        length = int.from_bytes(rfile.read(8), "big")
    mask = rfile.read(4) if masked else b""
    payload = rfile.read(length)
    if masked and mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def create_session(tool: str, title: str, workspace_dir: str = "") -> dict[str, Any]:
    session_id = secrets.token_hex(12)
    root = Path(workspace_dir).expanduser() if workspace_dir else POC_ROOT / session_id
    root.mkdir(parents=True, exist_ok=True)
    session = {
        "id": session_id,
        "tool": tool,
        "title": title or f"{tool.capitalize()} session",
        "workspace_dir": root,
        "created_at": now_iso(),
        "messages": [
            {
                "role": "system",
                "text": f"POC session created for {tool}. This UI controls files and commands through a local supervisor.",
                "ts": now_iso(),
            }
        ],
        "commands": [],
    }
    with LOCK:
        SESSIONS[session_id] = session
    return session


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Proxy POC</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css">
<style>
:root{
  --bg:#f8f8f6;--panel:#ffffff;--line:#e5e5df;--ink:#243238;--mut:#6e7c80;
  --acc:#2f6f98;--warn:#a56b3f;--soft:#f1f3f1;
}
*{box-sizing:border-box}
body{margin:0;font:16px/1.4 "Helvetica Neue",Helvetica,"Segoe UI",Arial,sans-serif;background:var(--bg);color:var(--ink)}
.app{display:grid;grid-template-columns:280px minmax(0,1fr) 400px;min-height:100vh}
.pane{border-right:1px solid var(--line);background:var(--panel)}
.pane:last-child{border-right:0}
.head{padding:14px 16px;border-bottom:1px solid var(--line);background:#fcfcfa}
.body{padding:14px 16px}
h1,h2,h3{margin:0;color:var(--ink)}
h1{font-size:20px}
h2{font-size:15px}
.mut{color:var(--mut)}
.card{border:1px solid var(--line);border-radius:12px;background:#fff;padding:12px}
.stack{display:grid;gap:12px}
.grow{flex:1 1 auto}
input,select,textarea,button{font:inherit}
input,select,textarea{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:10px;background:#fff}
button{padding:9px 12px;border:0;border-radius:999px;background:var(--acc);color:#fff;font-weight:700;cursor:pointer}
button.alt{background:#dde5e8;color:var(--ink)}
.row{display:flex;gap:8px;flex-wrap:wrap}
.row > *{flex:1 1 0}
.sessions{display:grid;gap:8px}
.session-btn{display:block;width:100%;text-align:left;padding:10px 11px;border:1px solid var(--line);border-radius:10px;background:#fff}
.session-btn.active{border-color:var(--acc);background:#f7fbfd}
.chat{display:grid;gap:8px;max-height:52vh;overflow:auto;padding-right:4px}
.msg{padding:10px 11px;border-radius:12px;border:1px solid var(--line);background:#fff}
.msg.user{background:#f7fbfd;border-color:#d9ebf5}
.msg.system{background:#fbfaf5}
.msg .meta{font-size:12px;color:var(--mut);margin-bottom:4px}
.tree{display:grid;gap:2px;max-height:260px;overflow:auto}
.node{font-size:13px;padding:2px 0}
.node.dir{font-weight:700}
.children{margin-left:14px}
pre{margin:0;white-space:pre-wrap;word-break:break-word;font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--soft);padding:10px;border-radius:10px;border:1px solid var(--line)}
.commands{display:grid;gap:8px;max-height:28vh;overflow:auto}
.cmd{border:1px solid var(--line);border-radius:10px;padding:10px;background:#fff}
.cmd .meta{font-size:12px;color:var(--mut);margin-bottom:6px}
.terminal-wrap{border:1px solid var(--line);border-radius:12px;background:#fbfbf8;overflow:hidden}
.terminal-meta{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 10px;border-bottom:1px solid var(--line);font-size:13px;color:var(--mut)}
.terminal-meta strong{color:var(--ink)}
#terminal{height:320px;padding:8px}
.pill{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:999px;padding:3px 8px;background:#fff;font-size:12px;color:var(--mut)}
.pill.live{color:#225d42;border-color:#b6d8c5;background:#f4fbf7}
.hint{font-size:13px;color:var(--mut)}
@media (max-width:1100px){.app{grid-template-columns:1fr}.pane{border-right:0;border-bottom:1px solid var(--line)}}
</style>
</head>
<body>
<div class="app">
  <section class="pane">
    <div class="head"><h1>Agent Proxy POC</h1><div class="mut">Local supervisor for chat, files, and commands.</div></div>
    <div class="body stack">
      <div class="card stack">
        <h2>New Session</h2>
        <select id="tool">
          <option value="claude">Claude</option>
          <option value="codex">Codex</option>
        </select>
        <input id="title" placeholder="Session title">
        <input id="workspace" placeholder="Optional workspace path">
        <button onclick="createSession()">Create Session</button>
      </div>
      <div class="stack">
        <h2>Sessions</h2>
        <div id="sessions" class="sessions"></div>
      </div>
    </div>
  </section>
  <section class="pane">
    <div class="head"><h2 id="session-title">No session selected</h2><div class="mut" id="session-path"></div></div>
    <div class="body stack">
      <div class="card stack">
        <h2>Live Terminal</h2>
        <div class="row">
          <button onclick="startTty('shell')">Open Shell</button>
          <button class="alt" onclick="startTty('claude')">Open Claude</button>
          <button class="alt" onclick="startTty('codex')">Open Codex</button>
        </div>
        <div class="terminal-wrap">
          <div class="terminal-meta">
            <div><strong id="tty-title">No terminal attached</strong></div>
            <div id="tty-status" class="pill">pending</div>
          </div>
          <div id="terminal"></div>
        </div>
        <div class="hint">This POC streams a real PTY into the browser terminal over a direct WebSocket. Input and resize events are sent back to the running shell, Claude, or Codex session.</div>
      </div>
      <div class="card stack">
        <h2>Chat</h2>
        <div id="chat" class="chat"></div>
        <textarea id="prompt" rows="4" placeholder="Send a prompt to the supervisor"></textarea>
        <div class="row">
          <button onclick="sendPrompt()">Send Prompt</button>
          <button class="alt" onclick="refresh()">Refresh</button>
        </div>
      </div>
      <div class="card stack">
        <h2>Command Runner</h2>
        <input id="command" placeholder="pwd">
        <button onclick="runCommand()">Run Command</button>
      </div>
    </div>
  </section>
  <section class="pane">
    <div class="head"><h2>Files And Activity</h2><div class="mut">Browse workspace files and recent commands.</div></div>
    <div class="body stack">
      <div class="card stack">
        <h2>File Tree</h2>
        <div id="tree" class="tree"></div>
        <input id="path" placeholder="Relative file path">
        <button onclick="readFile()">Open File</button>
        <pre id="file-content">Select a session and file path.</pre>
      </div>
      <div class="card stack">
        <h2>Commands</h2>
        <div id="commands" class="commands"></div>
      </div>
    </div>
  </section>
</div>
<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js"></script>
<script>
let currentId = "";
let term = null;
let termLoadFailed = false;
let fitAddon = null;
let resizeObserver = null;
let ttySocket = null;
let ttySocketSession = "";
let ttyResizeTimer = null;
function esc(s){return String(s ?? "");}
async function api(path, options={}){
  const res = await fetch(path, options);
  const data = await res.json();
  if(!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
function ensureTerminal(){
  if(term) return;
  if(typeof window.Terminal === "undefined" || !window.FitAddon || !window.FitAddon.FitAddon){
    if(!termLoadFailed){
      termLoadFailed = true;
      document.getElementById("tty-title").textContent = "Terminal library failed to load";
      setTtyStatus("error", false);
      document.getElementById("terminal").textContent = "xterm.js or its fit addon did not load. Check network access to the CDN or host the assets locally.";
    }
    return;
  }
  term = new Terminal({
    cursorBlink: true,
    fontSize: 13,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
    theme: { background: "#fbfbf8", foreground: "#243238" },
    scrollback: 3000,
    convertEol: true,
  });
  fitAddon = new window.FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(document.getElementById("terminal"));
  fitTerminal(true);
  term.writeln("Select a session, then open Shell, Claude, or Codex.");
  term.onData((data) => {
    if(!ttySocket || ttySocket.readyState !== WebSocket.OPEN) return;
    ttySocket.send(JSON.stringify({type:"input", data}));
  });
  term.onResize(({cols, rows}) => {
    if(cols < 2 || rows < 2) return;
    if(!ttySocket || ttySocket.readyState !== WebSocket.OPEN) return;
    if(ttyResizeTimer) clearTimeout(ttyResizeTimer);
    ttyResizeTimer = setTimeout(() => {
      if(ttySocket && ttySocket.readyState === WebSocket.OPEN){
        ttySocket.send(JSON.stringify({type:"resize", cols, rows}));
      }
    }, 30);
  });
  if(!resizeObserver){
    resizeObserver = new ResizeObserver(() => fitTerminal());
    resizeObserver.observe(document.getElementById("terminal"));
    resizeObserver.observe(document.querySelector(".app"));
  }
}
function fitTerminal(force=false){
  if(!term || !fitAddon) return;
  fitAddon.fit();
  if(force && ttySocket && ttySocket.readyState === WebSocket.OPEN && term.cols > 1 && term.rows > 1){
    ttySocket.send(JSON.stringify({type:"resize", cols: term.cols, rows: term.rows}));
  }
}
function setTtyStatus(label, live=false){
  const el = document.getElementById("tty-status");
  el.textContent = label;
  el.className = live ? "pill live" : "pill";
}
function setTtyTitle(label){
  document.getElementById("tty-title").textContent = label || "No terminal attached";
}
function disconnectTtySocket(){
  if(ttySocket){
    ttySocket.onopen = null;
    ttySocket.onmessage = null;
    ttySocket.onclose = null;
    ttySocket.onerror = null;
    try{ ttySocket.close(); }catch(err){}
  }
  ttySocket = null;
  ttySocketSession = "";
}
function handleTtyMessage(message){
  ensureTerminal();
  if(!term) return;
  if(message.type === "output"){
    term.write(message.data || "");
    return;
  }
  if(message.type === "status"){
    setTtyTitle(message.title ? `Terminal: ${message.title}` : "No terminal attached");
    setTtyStatus(message.alive ? "live" : "idle", !!message.alive);
    return;
  }
  if(message.type === "error"){
    setTtyStatus("error", false);
    if(message.message){
      term.writeln(`\r\n[${message.message}]`);
    }
  }
}
function connectTtySocket(){
  if(!currentId) return;
  if(ttySocket && ttySocketSession === currentId &&
    (ttySocket.readyState === WebSocket.OPEN || ttySocket.readyState === WebSocket.CONNECTING)){
    return;
  }
  disconnectTtySocket();
  ensureTerminal();
  if(!term) return;
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  ttySocketSession = currentId;
  ttySocket = new WebSocket(`${protocol}://${window.location.host}/api/sessions/${currentId}/tty/ws`);
  ttySocket.onopen = () => {
    setTtyStatus("connected", false);
    fitTerminal(true);
  };
  ttySocket.onmessage = (event) => {
    try{
      handleTtyMessage(JSON.parse(event.data));
    }catch(err){
      console.error("Invalid tty message", err);
    }
  };
  ttySocket.onclose = () => {
    if(currentId === ttySocketSession){
      setTtyStatus("disconnected", false);
    }
  };
  ttySocket.onerror = () => {
    setTtyStatus("error", false);
  };
}
function renderTree(nodes){
  const wrap = document.getElementById("tree");
  wrap.innerHTML = "";
  const build = (node) => {
    const outer = document.createElement("div");
    const line = document.createElement("div");
    line.className = `node ${node.type}`;
    line.textContent = node.name;
    outer.appendChild(line);
    if(node.children && node.children.length){
      const child = document.createElement("div");
      child.className = "children";
      node.children.forEach(item => child.appendChild(build(item)));
      outer.appendChild(child);
    }
    return outer;
  };
  if(!nodes.length){wrap.textContent = "Workspace is empty."; return;}
  nodes.forEach(node => wrap.appendChild(build(node)));
}
function renderSession(data){
  currentId = data.id;
  document.getElementById("session-title").textContent = data.title;
  document.getElementById("session-path").textContent = data.workspace_dir;
  ensureTerminal();
  if(term) term.clear();
  if(data.tty){
    setTtyTitle(data.tty.title ? `Terminal: ${data.tty.title}` : "No terminal attached");
    setTtyStatus(data.tty.active ? "live" : "pending", !!data.tty.active);
  }else{
    setTtyTitle("No terminal attached");
    setTtyStatus("pending", false);
  }
  const chat = document.getElementById("chat");
  chat.innerHTML = "";
  data.messages.forEach(msg => {
    const el = document.createElement("div");
    el.className = `msg ${msg.role}`;
    el.innerHTML = `<div class="meta">${esc(msg.role)} • ${esc(msg.ts)}</div><div>${esc(msg.text)}</div>`;
    chat.appendChild(el);
  });
  renderTree(data.files || []);
  const commands = document.getElementById("commands");
  commands.innerHTML = "";
  (data.commands || []).slice().reverse().forEach(cmd => {
    const el = document.createElement("div");
    el.className = "cmd";
    el.innerHTML = `<div class="meta">${esc(cmd.ts)} • exit ${esc(cmd.exit_code)}</div><pre>${esc(cmd.command)}\n\n${esc(cmd.output || "")}</pre>`;
    commands.appendChild(el);
  });
}
async function startTty(mode){
  if(!currentId) return;
  ensureTerminal();
  if(!term) return;
  term.clear();
  connectTtySocket();
  fitTerminal(true);
  setTtyTitle(`Terminal: ${mode}`);
  setTtyStatus("starting", false);
  await api(`/api/sessions/${currentId}/tty/start`, {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({mode})
  });
}
async function loadSessions(){
  const data = await api("/api/sessions");
  const wrap = document.getElementById("sessions");
  wrap.innerHTML = "";
  data.sessions.forEach(item => {
    const btn = document.createElement("button");
    btn.className = `session-btn ${item.id === currentId ? "active" : ""}`;
    btn.innerHTML = `<strong>${esc(item.title)}</strong><div class="mut">${esc(item.tool)} • ${esc(item.workspace_dir)}</div>`;
    btn.onclick = () => loadSession(item.id);
    wrap.appendChild(btn);
  });
}
async function createSession(){
  const data = await api("/api/sessions", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({
      tool: document.getElementById("tool").value,
      title: document.getElementById("title").value.trim(),
      workspace_dir: document.getElementById("workspace").value.trim(),
    })
  });
  await loadSessions();
  await loadSession(data.id);
}
async function loadSession(id){
  if(currentId && currentId !== id){
    disconnectTtySocket();
  }
  const data = await api(`/api/sessions/${id}`);
  renderSession(data);
  await loadSessions();
  connectTtySocket();
  fitTerminal(true);
}
async function sendPrompt(){
  if(!currentId) return;
  const prompt = document.getElementById("prompt").value.trim();
  if(!prompt) return;
  document.getElementById("prompt").value = "";
  const data = await api(`/api/sessions/${currentId}/prompt`, {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({prompt})
  });
  renderSession(data);
  await loadSessions();
}
async function runCommand(){
  if(!currentId) return;
  const command = document.getElementById("command").value.trim();
  if(!command) return;
  document.getElementById("command").value = "";
  const data = await api(`/api/sessions/${currentId}/commands`, {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({command})
  });
  renderSession(data);
}
async function readFile(){
  if(!currentId) return;
  const path = document.getElementById("path").value.trim();
  if(!path) return;
  const data = await api(`/api/sessions/${currentId}/files?path=${encodeURIComponent(path)}`);
  document.getElementById("file-content").textContent = data.content;
}
async function refresh(){
  if(currentId) await loadSession(currentId);
  else await loadSessions();
}
loadSessions();
</script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, code: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/tty/ws"):
            parts = parsed.path.split("/")
            if len(parts) == 6:
                self._handle_tty_websocket(parts[3])
                return
        if parsed.path == "/":
            self._html(HTML)
            return
        if parsed.path == "/api/sessions":
            with LOCK:
                items = [
                    {
                        "id": sess["id"],
                        "title": sess["title"],
                        "tool": sess["tool"],
                        "workspace_dir": str(sess["workspace_dir"]),
                    }
                    for sess in SESSIONS.values()
                ]
            self._json(200, {"sessions": items})
            return
        if parsed.path.startswith("/api/sessions/"):
            parts = parsed.path.split("/")
            if len(parts) == 4:
                session = self._get_session(parts[3])
                if not session:
                    return
                self._json(200, session_snapshot(session))
                return
            if len(parts) == 5 and parts[4] == "files":
                session = self._get_session(parts[3])
                if not session:
                    return
                qs = parse_qs(parsed.query)
                raw = (qs.get("path") or [""])[0]
                parts_rel = safe_rel_path(unquote(raw))
                target = session["workspace_dir"].joinpath(*parts_rel)
                if not target.exists() or not target.is_file():
                    self._json(404, {"error": "File not found"})
                    return
                self._json(200, {"path": str(target), "content": target.read_text(encoding="utf-8", errors="replace")})
                return
            if len(parts) == 5 and parts[4] == "tty":
                session = self._get_session(parts[3])
                if not session:
                    return
                self._json(200, tty_poll(session, 0))
                return
            if len(parts) == 6 and parts[4] == "tty" and parts[5] == "poll":
                session = self._get_session(parts[3])
                if not session:
                    return
                qs = parse_qs(parsed.query)
                try:
                    cursor = int((qs.get("cursor") or ["0"])[0])
                except ValueError:
                    cursor = 0
                self._json(200, tty_poll(session, cursor))
                return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/sessions":
            body = self._read_json()
            if body is None:
                return
            session = create_session(
                tool=str(body.get("tool") or "claude"),
                title=str(body.get("title") or ""),
                workspace_dir=str(body.get("workspace_dir") or ""),
            )
            self._json(200, {"id": session["id"]})
            return
        if parsed.path.startswith("/api/sessions/"):
            parts = parsed.path.split("/")
            if len(parts) < 5:
                self._json(404, {"error": "not found"})
                return
            session = self._get_session(parts[3])
            if not session:
                return
            action = parts[4]
            if action == "prompt":
                body = self._read_json()
                if body is None:
                    return
                prompt = str(body.get("prompt") or "").strip()
                if prompt:
                    session["messages"].append({"role": "user", "text": prompt, "ts": now_iso()})
                    session["messages"].append(
                        {
                            "role": "assistant",
                            "text": (
                                f"POC supervisor accepted a {session['tool']} prompt. "
                                "Real CLI integration would stream tool events here."
                            ),
                            "ts": now_iso(),
                        }
                    )
                self._json(200, session_snapshot(session))
                return
            if action == "commands":
                body = self._read_json()
                if body is None:
                    return
                command = str(body.get("command") or "").strip()
                if not command:
                    self._json(400, {"error": "Missing command"})
                    return
                result = subprocess.run(
                    command,
                    cwd=str(session["workspace_dir"]),
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                session["commands"].append(
                    {
                        "command": command,
                        "output": (result.stdout or "") + (result.stderr or ""),
                        "exit_code": result.returncode,
                        "ts": now_iso(),
                    }
                )
                session["messages"].append(
                    {
                        "role": "system",
                        "text": f"Executed command: {command} (exit {result.returncode})",
                        "ts": now_iso(),
                    }
                )
                self._json(200, session_snapshot(session))
                return
            if action == "tty":
                if len(parts) < 6:
                    self._json(404, {"error": "not found"})
                    return
                body = self._read_json()
                if body is None:
                    return
                tty_action = parts[5]
                if tty_action == "start":
                    mode = str(body.get("mode") or "shell")
                    with LOCK:
                        start_tty_session(session, mode)
                        if session.get("tty"):
                            tty_resize(session, 100, 30)
                    self._json(200, tty_poll(session, 0))
                    return
                if tty_action == "input":
                    data = str(body.get("data") or "")
                    with LOCK:
                        tty_write(session, data)
                    self._json(200, {"ok": True})
                    return
                if tty_action == "resize":
                    try:
                        cols = int(body.get("cols") or 100)
                        rows = int(body.get("rows") or 30)
                    except (TypeError, ValueError):
                        self._json(400, {"error": "Invalid resize payload"})
                        return
                    with LOCK:
                        tty_resize(session, cols, rows)
                    self._json(200, {"ok": True})
                    return
        self._json(404, {"error": "not found"})

    def _handle_tty_websocket(self, session_id: str) -> None:
        session = self._get_session(session_id)
        if not session:
            return
        ws_key = self.headers.get("Sec-WebSocket-Key")
        if not ws_key:
            self._json(400, {"error": "Missing WebSocket key"})
            return
        accept = base64.b64encode(hashlib.sha1(f"{ws_key}{WS_GUID}".encode("utf-8")).digest()).decode("ascii")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True

        peer = WebSocketPeer(self.connection)
        with LOCK:
            session.setdefault("tty_clients", set()).add(peer)
            title = session.get("tty_title", "")
            alive = bool((session.get("tty") or {}).get("alive"))
            backlog = "".join(chunk.get("data", "") for chunk in session.get("tty_chunks", []))
            cursor = int(session.get("tty_seq", 0))
        debug_log(f"tty websocket connected for session={session_id}")
        try:
            peer.send_json({"type": "status", "alive": alive, "title": title, "cursor": cursor})
            if backlog:
                peer.send_json({"type": "output", "data": backlog, "cursor": cursor})
            while peer.alive:
                opcode, payload = read_ws_frame(self.rfile)
                if opcode == 0x8:
                    debug_log(f"tty websocket close frame for session={session_id}")
                    break
                if opcode == 0x9:
                    with peer.send_lock:
                        self.connection.sendall(b"\x8a\x00")
                    continue
                if opcode != 0x1:
                    continue
                try:
                    message = json.loads(payload.decode("utf-8"))
                except Exception:
                    peer.send_json({"type": "error", "message": "invalid tty message"})
                    continue
                with LOCK:
                    live_session = SESSIONS.get(session_id)
                    if not live_session:
                        peer.send_json({"type": "error", "message": "session disappeared"})
                        break
                    if message.get("type") == "input":
                        tty_write(live_session, str(message.get("data") or ""))
                    elif message.get("type") == "resize":
                        try:
                            cols = int(message.get("cols") or 0)
                            rows = int(message.get("rows") or 0)
                        except (TypeError, ValueError):
                            cols = 0
                            rows = 0
                        if cols > 0 and rows > 0:
                            tty_resize(live_session, cols, rows)
        except EOFError:
            debug_log(f"tty websocket eof for session={session_id}")
        except Exception as exc:
            debug_log(f"tty websocket error for session={session_id}: {exc}")
        finally:
            with LOCK:
                live_session = SESSIONS.get(session_id)
                if live_session:
                    live_session.setdefault("tty_clients", set()).discard(peer)
            peer.close()
            debug_log(f"tty websocket disconnected for session={session_id}")

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length))
        except Exception:
            self._json(400, {"error": "Invalid JSON"})
            return None

    def _get_session(self, session_id: str) -> dict[str, Any] | None:
        with LOCK:
            session = SESSIONS.get(session_id)
        if not session:
            self._json(404, {"error": "session not found"})
            return None
        return session


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal local proxy POC for Claude/Codex-style sessions")
    parser.add_argument("--port", type=int, default=0, help="Port to listen on (0 = auto-select)")
    parser.add_argument("--tunnel", action="store_true", help="Expose the POC through cloudflared")
    args = parser.parse_args()
    POC_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadedServer(("127.0.0.1", args.port), Handler)
    port = server.server_address[1]
    local_url = f"http://127.0.0.1:{port}"
    print(f"Agent Proxy POC: {local_url}")
    tunnel_proc = None
    if args.tunnel:
        try:
            tunnel_proc, public_url = start_cloudflare_tunnel(local_url)
            print(f"Public URL: {public_url}")
        except Exception as exc:
            print(f"WARNING: tunnel start failed: {exc}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if tunnel_proc and tunnel_proc.poll() is None:
            tunnel_proc.terminate()
            try:
                tunnel_proc.wait(timeout=5)
            except Exception:
                tunnel_proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
