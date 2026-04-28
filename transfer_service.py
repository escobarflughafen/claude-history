#!/usr/bin/env python3
"""Unified transfer service for session upload, preview, file sync, and staging."""

from __future__ import annotations

import argparse
import datetime as dt
import hmac
import http.server
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from export_utils import now_iso, start_cloudflare_tunnel


MAX_UPLOAD_BYTES = 250 * 1024 * 1024
MAX_FILE_UPLOAD_BYTES = 50 * 1024 * 1024
PENDING_TTL = 30 * 60
SERVER_TEMP = Path(tempfile.gettempdir()) / "claude-history-transfer-service"
DEFAULT_WORKSPACE_ROOT = Path.home() / "claude-history-transfers"

ALLOWED_FILE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".css", ".csv", ".go", ".graphql", ".h", ".hpp",
    ".html", ".ini", ".java", ".jpeg", ".jpg", ".js", ".json", ".jsonl",
    ".kt", ".log", ".lua", ".m", ".md", ".patch", ".pdf", ".php", ".png",
    ".py", ".rb", ".rs", ".scss", ".sh", ".sql", ".svg", ".swift", ".toml",
    ".ts", ".tsx", ".txt", ".vue", ".xml", ".yaml", ".yml",
}
ALLOWED_BASENAMES = {
    ".env", ".env.example", ".gitignore", ".npmrc", ".prettierrc", ".tool-versions",
    "Dockerfile", "Gemfile", "Makefile", "README", "README.md", "requirements.txt",
}
ALLOWED_MIME_PREFIXES = ("text/", "image/")
ALLOWED_MIME_TYPES = {
    "application/json",
    "application/pdf",
    "application/sql",
    "application/xml",
    "application/x-sh",
    "application/x-yaml",
}

SESSIONS: dict[str, dict[str, Any]] = {}
SESSIONS_LOCK = threading.Lock()


def _parse_multipart(content_type: str, body: bytes) -> dict[str, Any]:
    match = re.search(r'boundary=([^\s;]+)', content_type)
    if not match:
        raise ValueError("No boundary parameter in Content-Type")
    boundary = match.group(1).strip('"').encode()
    result: dict[str, Any] = {}
    for part in body.split(b"--" + boundary)[1:]:
        if part.lstrip(b"\r\n").startswith(b"--"):
            break
        sep = part.find(b"\r\n\r\n")
        if sep < 0:
            continue
        raw_headers = part[2:sep] if part[:2] == b"\r\n" else part[:sep]
        part_body = part[sep + 4:]
        if part_body.endswith(b"\r\n"):
            part_body = part_body[:-2]
        headers: dict[str, str] = {}
        for line in raw_headers.split(b"\r\n"):
            if b":" in line:
                key, value = line.split(b":", 1)
                headers[key.strip().lower().decode()] = value.strip().decode()
        disp = headers.get("content-disposition", "")
        nm = re.search(r'name="([^"]*)"', disp)
        if not nm:
            continue
        name = nm.group(1)
        fn = re.search(r'filename="([^"]*)"', disp)
        if fn:
            result.setdefault(name, []).append((fn.group(1), headers.get("content-type", "application/octet-stream"), part_body))
        else:
            result[name] = part_body.decode("utf-8", errors="replace")
    return result


def _safe_json_load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_extract_zip(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(source) as archive:
        base = destination.resolve()
        for member in archive.infolist():
            member_path = Path(member.filename)
            if member_path.is_absolute():
                raise ValueError(f"Refusing to extract absolute zip path: {member.filename}")
            target = (destination / member_path).resolve()
            if os.path.commonpath([str(base), str(target)]) != str(base):
                raise ValueError(f"Refusing to extract path outside extract dir: {member.filename}")
        archive.extractall(destination)


def _clean_name(value: str) -> str:
    trimmed = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return trimmed or "workspace"


def _is_allowed_upload(name: str, content_type: str, payload: bytes) -> tuple[bool, str]:
    basename = Path(name).name
    suffix = Path(name).suffix.lower()
    normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
    if len(payload) > MAX_FILE_UPLOAD_BYTES:
        return False, f"{name}: file exceeds {MAX_FILE_UPLOAD_BYTES // 1024 // 1024} MB limit"
    if basename in ALLOWED_BASENAMES or suffix in ALLOWED_FILE_EXTENSIONS:
        return True, ""
    if normalized_type in ALLOWED_MIME_TYPES:
        return True, ""
    if any(normalized_type.startswith(prefix) for prefix in ALLOWED_MIME_PREFIXES):
        return True, ""
    return False, f"{name}: unsupported file type"


def _session_snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    uploaded_bundle = entry.get("uploaded_bundle")
    files = entry.get("uploaded_files", [])
    prepared = entry.get("prepared")
    return {
        "status": entry.get("status", "waiting"),
        "expires_at": entry.get("expires_at"),
        "uploaded_session": uploaded_bundle,
        "uploaded_files": files,
        "prepared": prepared,
        "suggested_workspace_dir": entry.get("suggested_workspace_dir"),
        "session_conflict": entry.get("session_conflict"),
    }


def _ensure_session(token: str) -> dict[str, Any]:
    with SESSIONS_LOCK:
        session = SESSIONS.get(token)
        if not session:
            session_dir = SERVER_TEMP / token
            session_dir.mkdir(parents=True, exist_ok=True)
            session = {
                "token": token,
                "created_at": time.monotonic(),
                "expires_at": (dt.datetime.now().astimezone() + dt.timedelta(seconds=PENDING_TTL)).isoformat(),
                "session_dir": session_dir,
                "status": "waiting",
                "uploaded_bundle": None,
                "uploaded_files": [],
                "prepared": None,
                "suggested_workspace_dir": "",
                "session_conflict": None,
            }
            SESSIONS[token] = session
        return session


def _default_workspace_dir(bundle: dict[str, Any]) -> Path:
    session = bundle.get("session") or {}
    cwd = str(session.get("cwd") or session.get("project") or "").strip()
    leaf = Path(cwd).name if cwd else ""
    if not leaf:
        leaf = f"{bundle.get('tool', 'session')}-{session.get('session_id', 'workspace')}"
    return DEFAULT_WORKSPACE_ROOT / _clean_name(leaf)


def _destination_session_conflict(bundle: dict[str, Any]) -> dict[str, Any]:
    tool = str(bundle.get("tool") or "")
    session = bundle.get("session") or {}
    session_id = str(session.get("session_id") or "")
    if not tool or not session_id:
        return {"exists": False, "tool": tool, "session_id": session_id}

    if tool == "claude":
        claude_dir = Path.home() / ".claude"
        history_path = claude_dir / "history.jsonl"
        transcript_match = False
        history_match = False
        project = str(session.get("project") or session.get("cwd") or "")
        if project:
            encoded = "".join("-" if ch == "/" else ch for ch in project.replace("\\", "/").replace(":", "")) or "-"
            transcript_path = claude_dir / "projects" / encoded / f"{session_id}.jsonl"
            transcript_match = transcript_path.exists()
        if history_path.exists():
            try:
                for line in history_path.read_text(encoding="utf-8").splitlines():
                    if f'"sessionId":"{session_id}"' in line or f'"sessionId": "{session_id}"' in line:
                        history_match = True
                        break
            except OSError:
                history_match = False
        return {
            "exists": transcript_match or history_match,
            "tool": tool,
            "session_id": session_id,
            "history_match": history_match,
            "transcript_match": transcript_match,
        }

    if tool == "codex":
        codex_dir = Path.home() / ".codex"
        history_path = codex_dir / "history.jsonl"
        sessions_dir = codex_dir / "sessions"
        history_match = False
        transcript_match = False
        if history_path.exists():
            try:
                for line in history_path.read_text(encoding="utf-8").splitlines():
                    if f'"session_id":"{session_id}"' in line or f'"session_id": "{session_id}"' in line:
                        history_match = True
                        break
            except OSError:
                history_match = False
        if sessions_dir.exists():
            transcript_match = any(sessions_dir.rglob(f"*{session_id}.jsonl"))
        return {
            "exists": history_match or transcript_match,
            "tool": tool,
            "session_id": session_id,
            "history_match": history_match,
            "transcript_match": transcript_match,
        }

    return {"exists": False, "tool": tool, "session_id": session_id}


def _evict_expired() -> None:
    now = time.monotonic()
    stale: list[str] = []
    with SESSIONS_LOCK:
        for token, entry in SESSIONS.items():
            if now - float(entry.get("created_at") or 0) > PENDING_TTL:
                stale.append(token)
        for token in stale:
            entry = SESSIONS.pop(token, None)
            if entry:
                shutil.rmtree(entry.get("session_dir", ""), ignore_errors=True)


def _persist_state(entry: dict[str, Any]) -> None:
    state_path = Path(entry["session_dir"]) / "state.json"
    payload = _session_snapshot(entry)
    payload["updated_at"] = now_iso()
    state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _resolve_workspace_dir(raw_value: str, fallback: str) -> Path:
    chosen = raw_value.strip() if raw_value.strip() else fallback
    path = Path(chosen).expanduser()
    if not path.is_absolute():
        raise ValueError("Destination workspace path must be absolute")
    try:
        path.relative_to(Path.home())
    except ValueError as exc:
        raise ValueError(f"Destination workspace must stay under the host user's home directory: {Path.home()}") from exc
    return path


def _prepare_workspace(
    entry: dict[str, Any],
    workspace_name: str,
    workspace_dir: Path,
    session_action: str,
) -> dict[str, Any]:
    bundle_meta = entry.get("uploaded_bundle") or {}
    bundle_path = Path(bundle_meta["bundle_path"])
    prepared_root = Path(entry["session_dir"]) / "prepared" / _clean_name(workspace_name)
    bundle_extract = prepared_root / "bundle"
    workspace_root = workspace_dir.expanduser()
    if prepared_root.exists():
        shutil.rmtree(prepared_root)
    bundle_extract.mkdir(parents=True, exist_ok=True)
    workspace_root.parent.mkdir(parents=True, exist_ok=True)

    _safe_extract_zip(bundle_path, bundle_extract)
    session_json_path = bundle_extract / "session.json"
    bundle_payload = _safe_json_load(session_json_path)
    original_session = bundle_payload.get("session") or {}
    target_session_id = str(original_session.get("session_id") or "")
    if session_action == "new":
        target_session_id = str(uuid.uuid4())
    elif session_action != "overwrite":
        raise ValueError("session_action must be 'overwrite' or 'new'")

    target_cwd = str(workspace_root)
    if bundle_payload.get("tool") == "claude":
        bundle_payload.setdefault("session", {})
        bundle_payload["session"]["session_id"] = target_session_id
        bundle_payload["session"]["cwd"] = target_cwd
        bundle_payload["session"]["project"] = target_cwd
    else:
        bundle_payload.setdefault("session", {})
        bundle_payload["session"]["session_id"] = target_session_id
        bundle_payload["session"]["cwd"] = target_cwd
    session_json_path.write_text(json.dumps(bundle_payload, indent=2) + "\n", encoding="utf-8")

    if workspace_root.exists():
        if session_action == "overwrite":
            shutil.rmtree(workspace_root)
        else:
            raise ValueError(f"Workspace already exists: {workspace_root}")
    workspace_root.mkdir(parents=True, exist_ok=True)

    synced_files: list[dict[str, str]] = []
    for item in entry.get("uploaded_files", []):
        source = Path(item["stored_path"])
        rel_path = Path(item.get("relative_path") or item.get("name") or source.name)
        safe_parts = [part for part in rel_path.parts if part not in {"", ".", ".."}]
        if not safe_parts:
            safe_parts = [source.name]
        target = workspace_root.joinpath(*safe_parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        synced_files.append({"name": item["name"], "relative_path": "/".join(safe_parts), "target": str(target)})

    manifest = {
        "prepared_at": now_iso(),
        "tool": bundle_meta.get("tool"),
        "source_session_id": original_session.get("session_id"),
        "target_session_id": target_session_id,
        "session_action": session_action,
        "workspace_name": workspace_name,
        "bundle_dir": str(bundle_extract),
        "workspace_dir": str(workspace_root),
        "files_synced": synced_files,
    }
    (prepared_root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    entry["prepared"] = manifest
    entry["status"] = "prepared"
    _persist_state(entry)
    return manifest


_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transfer Service</title>
<style>
:root{--bg:#f5f0e6;--card:#fffaf1;--ink:#1b1b18;--mut:#6f6a62;--line:#d8cfc0;--acc:#0b6e4f;--acc2:#c96d28;--err:#a12626}
*{box-sizing:border-box} body{margin:0;font-family:Georgia,ui-serif,serif;background:linear-gradient(135deg,#efe7d7,#f8f4ec 45%,#e4efe5);color:var(--ink)}
.wrap{max-width:980px;margin:0 auto;padding:24px} h1{margin:0 0 8px;font-size:38px} p{line-height:1.45}
.hero{padding:18px 20px;border:1px solid var(--line);background:rgba(255,250,241,.88);backdrop-filter:blur(8px);border-radius:18px;box-shadow:0 18px 42px rgba(67,53,24,.08)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin-top:16px}
.card{border:1px solid var(--line);background:var(--card);border-radius:18px;padding:18px;box-shadow:0 10px 28px rgba(67,53,24,.06)}
.eyebrow{font:700 12px/1.2 ui-monospace,SFMono-Regular,monospace;letter-spacing:.12em;text-transform:uppercase;color:var(--acc2);margin-bottom:8px}
.mono,code,pre,input{font-family:ui-monospace,SFMono-Regular,monospace}
pre{white-space:pre-wrap;word-break:break-word;background:#faf5ea;border:1px solid var(--line);padding:12px;border-radius:12px;min-height:54px}
button{border:0;border-radius:999px;padding:10px 16px;background:var(--acc);color:#fff;font-weight:700;cursor:pointer}
button.alt{background:#d9d1c2;color:var(--ink)} button.warn{background:var(--acc2)} button.danger{background:var(--err)}
button:disabled{opacity:.45;cursor:default}
input[type=text]{width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:#fffdfa;color:var(--ink)}
.mut{color:var(--mut)} .hidden{display:none}
.list{display:grid;gap:10px;margin-top:10px}
.pill{display:inline-block;padding:3px 8px;border-radius:999px;background:#ece2d0;color:#5a4530;font-size:12px;font-weight:700}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.files{margin-top:10px}
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <div class="eyebrow">Internal Transfer Session</div>
    <h1>Session Upload And Workspace Prep</h1>
    <p class="mut">Run the one-liner on the source machine, pick a Claude or Codex session in the terminal, then return here to review the uploaded preview and optionally send workspace files.</p>
  </div>

  <div class="grid">
    <section class="card">
      <div class="eyebrow">Step 1</div>
      <h2>Connect Source Machine</h2>
      <p class="mut">Copy this one-liner into a shell on the machine that has the session history.</p>
      <pre id="cmd"></pre>
      <div class="row">
        <button onclick="copyCmd()">Copy Command</button>
        <button class="alt" onclick="refreshState()">Refresh</button>
      </div>
    </section>

    <section class="card">
      <div class="eyebrow">Step 2</div>
      <h2>Uploaded Session</h2>
      <div id="session-empty" class="mut">Waiting for the helper to upload a selected session.</div>
      <div id="session-view" class="hidden">
        <div class="row">
          <span class="pill" id="tool-pill"></span>
          <span class="pill" id="sid-pill"></span>
        </div>
        <div class="list">
          <div><strong>Working dir:</strong> <span id="cwd"></span></div>
          <div><strong>Messages:</strong> <span id="messages"></span></div>
          <div><strong>Last prompt:</strong> <span id="prompt"></span></div>
          <div><strong>Destination session check:</strong> <span id="conflict"></span></div>
        </div>
      </div>
    </section>
  </div>

  <div class="grid">
    <section class="card">
      <div class="eyebrow">Step 3</div>
      <h2>Optional File Sync</h2>
      <p class="mut">Pick multiple files or a whole directory. Relative paths are preserved when the browser provides them. Accepted types: common source, config, text, image, JSON, YAML, Markdown, PDF, and logs. Per-file limit: 50 MB.</p>
      <input id="files" class="files" type="file" multiple webkitdirectory accept=".c,.cc,.cpp,.css,.csv,.go,.graphql,.h,.hpp,.html,.ini,.java,.jpeg,.jpg,.js,.json,.jsonl,.kt,.log,.lua,.m,.md,.patch,.pdf,.php,.png,.py,.rb,.rs,.scss,.sh,.sql,.svg,.swift,.toml,.ts,.tsx,.txt,.vue,.xml,.yaml,.yml,.env,.env.example,.gitignore,.npmrc,.prettierrc,.tool-versions">
      <div class="row" style="margin-top:12px">
        <button class="warn" onclick="uploadFiles()">Upload Files</button>
      </div>
      <div id="files-list" class="list"></div>
    </section>

    <section class="card">
      <div class="eyebrow">Step 4</div>
      <h2>Prepare Workspace</h2>
      <p class="mut">This stages the uploaded bundle and synced files on the server in a prepared workspace directory owned by the host user. You can change the destination cwd before preparing.</p>
      <label for="workspace">Workspace name</label>
      <input id="workspace" type="text" placeholder="restored-project">
      <label for="workspace-dir" style="margin-top:12px;display:block">Destination workspace path</label>
      <input id="workspace-dir" type="text" placeholder="/home/host-user/claude-history-transfers/restored-project">
      <label for="session-action" style="margin-top:12px;display:block">If the session already exists on this host</label>
      <select id="session-action" style="width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:#fffdfa;color:var(--ink)">
        <option value="new">Create a new session ID</option>
        <option value="overwrite">Overwrite the existing session ID</option>
      </select>
      <div class="row" style="margin-top:12px">
        <button onclick="finalizeTransfer()">Prepare In Background</button>
        <button class="danger" onclick="closeSession()">Close Session</button>
      </div>
      <pre id="result"></pre>
    </section>
  </div>
</div>
<script>
const qs=new URLSearchParams(location.search);
const token=qs.get("token")||"";
const origin=location.origin;
const cmd=`curl -fsSL ${origin}/bootstrap.sh?token=${encodeURIComponent(token)} | bash`;
document.getElementById("cmd").textContent=cmd;

function byId(id){return document.getElementById(id);}
async function api(path, options={}){
  const response=await fetch(path, options);
  const data=await response.json();
  if(!response.ok){throw new Error(data.error||`HTTP ${response.status}`);}
  return data;
}
function renderState(data){
  const session=data.uploaded_session;
  const list=byId("files-list");
  list.innerHTML="";
  for(const item of data.uploaded_files||[]){
    const el=document.createElement("div");
    el.textContent=`${item.relative_path||item.name} (${item.size} bytes)`;
    list.appendChild(el);
  }
  if((data.rejected_files||[]).length){
    const rejected=document.createElement("div");
    rejected.textContent=`Rejected: ${data.rejected_files.join("; ")}`;
    list.appendChild(rejected);
  }
  if(!session){
    byId("session-empty").classList.remove("hidden");
    byId("session-view").classList.add("hidden");
  }else{
    byId("session-empty").classList.add("hidden");
    byId("session-view").classList.remove("hidden");
    byId("tool-pill").textContent=session.tool;
    byId("sid-pill").textContent=session.session.session_id;
    byId("cwd").textContent=session.session.cwd||session.session.project||"unknown";
    byId("messages").textContent=String(session.session.message_count||session.session.prompt_count||0);
    byId("prompt").textContent=session.session.last_prompt||session.session.first_prompt||"";
    const conflict=data.session_conflict||{};
    byId("conflict").textContent=conflict.exists
      ? `existing ${conflict.tool} session ${conflict.session_id} found on destination host`
      : "no existing session with this ID found on destination host";
    byId("session-action").value=conflict.exists ? "new" : "overwrite";
    if(!byId("workspace").value){
      const base=(session.session.cwd||session.session.project||"workspace").split("/").filter(Boolean).pop()||"workspace";
      byId("workspace").value=base;
    }
    if(!byId("workspace-dir").value && data.suggested_workspace_dir){
      byId("workspace-dir").value=data.suggested_workspace_dir;
    }
  }
  if(data.prepared){
    byId("result").textContent=JSON.stringify(data.prepared,null,2);
  }
}
async function refreshState(){
  try{
    const data=await api(`/api/state?token=${encodeURIComponent(token)}`);
    renderState(data);
  }catch(err){
    byId("result").textContent=String(err);
  }
}
async function copyCmd(){
  await navigator.clipboard.writeText(cmd);
}
async function uploadFiles(){
  const files=byId("files").files;
  if(!files.length){
    byId("result").textContent="Choose at least one file or directory first.";
    return;
  }
  const fd=new FormData();
  fd.append("token", token);
  for(const file of files){
    fd.append("files", file, file.webkitRelativePath || file.name);
  }
  try{
    const data=await api("/api/files",{method:"POST",body:fd});
    renderState(data);
    byId("result").textContent="Files uploaded.";
  }catch(err){
    byId("result").textContent=String(err);
  }
}
async function finalizeTransfer(){
  try{
    const data=await api("/api/finalize",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        token,
        workspace_name:byId("workspace").value.trim(),
        workspace_dir:byId("workspace-dir").value.trim(),
        session_action:byId("session-action").value
      })
    });
    renderState(data);
    byId("result").textContent=JSON.stringify(data.prepared,null,2);
  }catch(err){
    byId("result").textContent=String(err);
  }
}
async function closeSession(){
  try{
    await api("/api/close",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({token})
    });
    byId("result").textContent="Session closed. This page will stop responding shortly.";
  }catch(err){
    byId("result").textContent=String(err);
  }
}
refreshState();
setInterval(refreshState, 4000);
</script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    server_token = ""
    public_base = ""
    tunnel_proc: subprocess.Popen[str] | None = None
    shutdown_server: callable | None = None

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.info("%s - %s", self.address_string(), fmt % args)

    def _json(self, code: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _text(self, code: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _ok_token(self, token: str) -> bool:
        return hmac.compare_digest(token or "", self.server_token)

    def _require_token(self, token: str) -> bool:
        if self._ok_token(token):
            _ensure_session(token)
            return True
        self._json(401, {"error": "Invalid token"})
        return False

    def do_GET(self) -> None:  # noqa: N802
        _evict_expired()
        parsed = urlparse(self.path)
        token = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
        if parsed.path == "/":
            if not self._require_token(token):
                return
            self._html(200, _HTML)
            return
        if parsed.path == "/health":
            self._json(200, {"status": "ok"})
            return
        if parsed.path == "/bootstrap.sh":
            if not self._require_token(token):
                return
            base = self.public_base or f"http://{self.headers.get('Host', '127.0.0.1')}"
            script = self._render_bootstrap(base.rstrip("/"), token)
            self._text(200, script)
            return
        if parsed.path.startswith("/client/"):
            if not self._require_token(token):
                return
            self._serve_client_asset(parsed.path)
            return
        if parsed.path == "/api/state":
            if not self._require_token(token):
                return
            session = _ensure_session(token)
            self._json(200, _session_snapshot(session))
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        _evict_expired()
        path = urlparse(self.path).path
        if path == "/api/client-upload":
            self._client_upload()
        elif path == "/api/files":
            self._upload_files()
        elif path == "/api/finalize":
            self._finalize()
        elif path == "/api/close":
            self._close()
        else:
            self._json(404, {"error": "not found"})

    def _render_bootstrap(self, base_url: str, token: str) -> str:
        return f"""#!/usr/bin/env bash
set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required." >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required." >&2
  exit 1
fi

BASE_DIR="${{TMPDIR:-/tmp}}/claude-history-transfer-$RANDOM"
mkdir -p "$BASE_DIR"
cleanup() {{
  rm -rf "$BASE_DIR"
}}
trap cleanup EXIT

for asset in transfer_client_helper.py export_utils.py claude_history_viewer.py codex_history_viewer.py; do
  curl -fsSL "{base_url}/client/$asset?token={urllib.parse.quote(token)}" -o "$BASE_DIR/$asset"
done

python3 "$BASE_DIR/transfer_client_helper.py" --server "{base_url}" --token "{token}"
"""

    def _serve_client_asset(self, path: str) -> None:
        mapping = {
            "/client/transfer_client_helper.py": Path(__file__).parent / "transfer_client_helper.py",
            "/client/export_utils.py": Path(__file__).parent / "export_utils.py",
            "/client/claude_history_viewer.py": Path(__file__).parent / "claude_history_viewer.py",
            "/client/codex_history_viewer.py": Path(__file__).parent / "codex_history_viewer.py",
        }
        target = mapping.get(path)
        if not target or not target.exists():
            self._json(404, {"error": "asset not found"})
            return
        body = target.read_text(encoding="utf-8")
        self._text(200, body, "text/x-python; charset=utf-8")

    def _client_upload(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._json(400, {"error": "Expected multipart/form-data"})
            return
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length > MAX_UPLOAD_BYTES:
            self._json(413, {"error": "Upload too large"})
            return
        raw = self.rfile.read(min(content_length, MAX_UPLOAD_BYTES + 1))
        if len(raw) > MAX_UPLOAD_BYTES:
            self._json(413, {"error": "Upload too large"})
            return
        try:
            fields = _parse_multipart(content_type, raw)
        except Exception as exc:
            self._json(400, {"error": f"Multipart parse error: {exc}"})
            return
        token = (fields.get("token") or "").strip()
        if not self._require_token(token):
            return
        uploads = fields.get("bundle") or []
        if not uploads:
            self._json(400, {"error": "Missing bundle upload"})
            return
        filename, _, payload = uploads[0]
        session = _ensure_session(token)
        bundle_dir = Path(session["session_dir"]) / "incoming"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = bundle_dir / Path(filename).name
        bundle_path.write_bytes(payload)
        try:
            extract_dir = bundle_dir / "bundle"
            if extract_dir.exists():
                shutil.rmtree(extract_dir)
            extract_dir.mkdir(parents=True, exist_ok=True)
            _safe_extract_zip(bundle_path, extract_dir)
            bundle = _safe_json_load(extract_dir / "session.json")
        except Exception as exc:
            self._json(422, {"error": f"Invalid bundle: {exc}"})
            return
        session["uploaded_bundle"] = {
            "bundle_path": str(bundle_path),
            "extract_dir": str(extract_dir),
            "tool": bundle.get("tool"),
            "session": bundle.get("session", {}),
            "metadata": bundle.get("metadata", {}),
            "analytics": bundle.get("analytics", {}),
        }
        session["suggested_workspace_dir"] = str(_default_workspace_dir(bundle))
        session["session_conflict"] = _destination_session_conflict(bundle)
        session["status"] = "bundle_uploaded"
        _persist_state(session)
        self._json(200, _session_snapshot(session))

    def _upload_files(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._json(400, {"error": "Expected multipart/form-data"})
            return
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length > MAX_UPLOAD_BYTES:
            self._json(413, {"error": "Upload too large"})
            return
        raw = self.rfile.read(min(content_length, MAX_UPLOAD_BYTES + 1))
        if len(raw) > MAX_UPLOAD_BYTES:
            self._json(413, {"error": "Upload too large"})
            return
        try:
            fields = _parse_multipart(content_type, raw)
        except Exception as exc:
            self._json(400, {"error": f"Multipart parse error: {exc}"})
            return
        token = (fields.get("token") or "").strip()
        if not self._require_token(token):
            return
        uploads = fields.get("files") or []
        if not uploads:
            self._json(400, {"error": "No files received"})
            return
        session = _ensure_session(token)
        files_dir = Path(session["session_dir"]) / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        uploaded_files: list[dict[str, Any]] = []
        rejected_files: list[str] = []
        for filename, _, payload in uploads:
            rel_name = filename or "file"
            content_type = mimetypes.guess_type(rel_name)[0] or "application/octet-stream"
            allowed, reason = _is_allowed_upload(rel_name, content_type, payload)
            if not allowed:
                rejected_files.append(reason)
                continue
            safe_parts = [part for part in Path(rel_name).parts if part not in {"", ".", ".."}]
            if not safe_parts:
                safe_parts = [Path(rel_name).name or "file"]
            stored_path = files_dir.joinpath(*safe_parts)
            stored_path.parent.mkdir(parents=True, exist_ok=True)
            stored_path.write_bytes(payload)
            uploaded_files.append(
                {
                    "name": Path(rel_name).name,
                    "relative_path": "/".join(safe_parts),
                    "stored_path": str(stored_path),
                    "size": len(payload),
                }
            )
        existing = {
            str(item.get("relative_path") or item.get("name") or ""): item
            for item in session.get("uploaded_files", [])
        }
        for item in uploaded_files:
            existing[str(item.get("relative_path") or item.get("name") or "")] = item
        session["uploaded_files"] = list(existing.values())
        if session.get("status") == "waiting":
            session["status"] = "files_uploaded"
        snapshot = _session_snapshot(session)
        if rejected_files:
            snapshot["rejected_files"] = rejected_files
        _persist_state(session)
        self._json(200, snapshot)

    def _read_json_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length))
        except Exception:
            self._json(400, {"error": "Invalid JSON body"})
            return None

    def _finalize(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        token = (body.get("token") or "").strip()
        if not self._require_token(token):
            return
        session = _ensure_session(token)
        if not session.get("uploaded_bundle"):
            self._json(400, {"error": "Upload a session bundle first"})
            return
        workspace_name = (body.get("workspace_name") or "").strip() or "workspace"
        workspace_dir_raw = str(body.get("workspace_dir") or "")
        session_action = str(body.get("session_action") or "new").strip().lower()
        if session_action not in {"overwrite", "new"}:
            self._json(400, {"error": "session_action must be 'overwrite' or 'new'"})
            return
        conflict = session.get("session_conflict") or {}
        if conflict.get("exists") and session_action not in {"overwrite", "new"}:
            self._json(400, {"error": "Choose whether to overwrite or create a new session"})
            return
        try:
            workspace_dir = _resolve_workspace_dir(workspace_dir_raw, str(session.get("suggested_workspace_dir") or ""))
            manifest = _prepare_workspace(session, workspace_name, workspace_dir, session_action)
        except Exception as exc:
            self._json(422, {"error": str(exc)})
            return
        self._json(200, _session_snapshot(session))

    def _close(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        token = (body.get("token") or "").strip()
        if not self._require_token(token):
            return
        self._json(200, {"status": "closing"})

        def closer() -> None:
            time.sleep(0.2)
            if self.tunnel_proc and self.tunnel_proc.poll() is None:
                self.tunnel_proc.terminate()
            if self.shutdown_server:
                self.shutdown_server()

        threading.Thread(target=closer, daemon=True).start()


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified session transfer service")
    parser.add_argument("--port", type=int, default=0, help="Port to listen on (0 = auto-select)")
    parser.add_argument("--token", default="", help="One-time auth token. Generated if omitted.")
    parser.add_argument("--tunnel", action="store_true", help="Expose the service with cloudflared.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    SERVER_TEMP.mkdir(parents=True, exist_ok=True)

    token = args.token.strip() or secrets.token_urlsafe(24)
    Handler.server_token = token

    server = ThreadedServer(("127.0.0.1", args.port), Handler)
    Handler.shutdown_server = server.shutdown
    port = server.server_address[1]
    local_url = f"http://127.0.0.1:{port}"
    public_url = local_url
    tunnel_proc = None

    if args.tunnel:
        try:
            tunnel_proc, public_url = start_cloudflare_tunnel(local_url)
            Handler.tunnel_proc = tunnel_proc
        except Exception as exc:
            print(f"WARNING: tunnel start failed: {exc}", file=sys.stderr)

    Handler.public_base = public_url

    print()
    print("  Transfer Service")
    print("  " + "─" * 34)
    print(f"  Local URL : {local_url}/?token={token}")
    print(f"  Token     : {token}")
    if public_url != local_url:
        print(f"  Public URL: {public_url}/?token={token}")
    print()

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
        shutil.rmtree(SERVER_TEMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
