#!/usr/bin/env python3
"""One-shot client helper for uploading a local Claude or Codex session bundle."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from urllib import error, request


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import claude_history_viewer as chv
import codex_history_viewer as xov


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload a selected Claude/Codex session to a transfer service.")
    parser.add_argument("--server", required=True, help="Transfer service base URL.")
    parser.add_argument("--token", required=True, help="One-time transfer token.")
    parser.add_argument("--claude-dir", default=str(Path.home() / ".claude"), help="Claude state directory.")
    parser.add_argument("--codex-dir", default=str(Path.home() / ".codex"), help="Codex state directory.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum sessions to load per tool.")
    return parser.parse_args()


def _safe_load_sessions(limit: int, claude_dir: Path, codex_dir: Path) -> list[dict]:
    items: list[dict] = []
    try:
        for session in chv.load_sessions(claude_dir, limit):
            items.append(
                {
                    "tool": "claude",
                    "session_id": session.session_id,
                    "cwd": session.cwd,
                    "sort_ts_ms": session.sort_ts_ms,
                    "last_activity": chv.format_ts(session.sort_ts_ms),
                    "prompt": session.last_prompt or session.first_prompt,
                    "session": session,
                }
            )
    except Exception:
        pass

    try:
        for session in xov.load_sessions(codex_dir, limit):
            items.append(
                {
                    "tool": "codex",
                    "session_id": session.session_id,
                    "cwd": session.cwd,
                    "sort_ts_ms": session.sort_ts_ms,
                    "last_activity": xov.format_ts(session.sort_ts_ms),
                    "prompt": session.last_prompt or session.first_prompt,
                    "session": session,
                }
            )
    except Exception:
        pass

    items.sort(key=lambda item: int(item.get("sort_ts_ms") or 0), reverse=True)
    return items


def _encode_multipart(fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    boundary = f"----claudehistory{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    for field_name, filename, content_type, payload in files:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{filename}"\r\n'
                ).encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                payload,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _post_bundle(server: str, token: str, bundle_path: Path) -> dict:
    content_type = mimetypes.guess_type(bundle_path.name)[0] or "application/octet-stream"
    body, header = _encode_multipart(
        {"token": token},
        [("bundle", bundle_path.name, content_type, bundle_path.read_bytes())],
    )
    req = request.Request(
        server.rstrip("/") + "/api/client-upload",
        data=body,
        method="POST",
        headers={"Content-Type": header, "Content-Length": str(len(body))},
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(payload or str(exc)) from exc


def _pick_session(items: list[dict]) -> dict:
    tty_in = None
    tty_out = None
    try:
        tty_in = open("/dev/tty", "r", encoding="utf-8", errors="replace")
        tty_out = open("/dev/tty", "w", encoding="utf-8", errors="replace")
    except OSError:
        tty_in = sys.stdin
        tty_out = sys.stdout

    print()
    print("Available sessions")
    print("──────────────────")
    for idx, item in enumerate(items, start=1):
        prompt = " ".join(str(item.get("prompt") or "").split())
        if len(prompt) > 72:
            prompt = prompt[:69] + "..."
        cwd = str(item.get("cwd") or "unknown")
        print(f"{idx:>3}. [{item['tool']}] {item['last_activity']}  {cwd}")
        if prompt:
            print(f"     {prompt}")
    print()
    print(f"Pick a session number (1-{len(items)}): ", end="", file=tty_out, flush=True)
    choice = tty_in.readline()
    if choice == "":
        raise ValueError("No terminal input available. Run the one-liner from an interactive shell.")
    choice = choice.strip()
    if not choice.isdigit():
        raise ValueError("Invalid selection.")
    selected = int(choice)
    if selected < 1 or selected > len(items):
        raise ValueError("Selection out of range.")
    return items[selected - 1]


def _write_bundle(item: dict, base_dir: Path) -> Path:
    session = item["session"]
    output_dir = base_dir / f"{item['tool']}-session-{item['session_id']}-bundle"
    if item["tool"] == "claude":
        return chv.write_web_bundle(session, output_dir=str(output_dir))
    return xov.write_web_bundle(session, output_dir=str(output_dir))


def main() -> int:
    args = parse_args()
    sessions = _safe_load_sessions(args.limit, Path(args.claude_dir).expanduser(), Path(args.codex_dir).expanduser())
    if not sessions:
        print("No Claude or Codex sessions were found on this machine.", file=sys.stderr)
        return 1

    try:
        selected = _pick_session(sessions)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    temp_dir = Path(tempfile.mkdtemp(prefix="transfer-client-"))
    try:
        bundle_dir = _write_bundle(selected, temp_dir)
        package_zip = bundle_dir / "package.zip"
        print()
        print(f"Uploading {selected['tool']} session {selected['session_id']}...")
        result = _post_bundle(args.server, args.token, package_zip)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    preview = (result.get("uploaded_session") or {}).get("session") or {}
    print("Upload complete.")
    print(f"Server accepted session: {preview.get('session_id', selected['session_id'])}")
    print(f"Working directory: {preview.get('cwd') or preview.get('project') or selected.get('cwd') or 'unknown'}")
    print()
    print("Return to the browser to review the preview and optionally upload workspace files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
