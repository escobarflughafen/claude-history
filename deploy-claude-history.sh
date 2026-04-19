#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNAME_S="$(uname -s)"
DEFAULT_INSTALL_ROOT="/opt/claude-history"
DEFAULT_BIN_DIR="/usr/local/bin"
if [[ "$UNAME_S" == "Darwin" ]]; then
  DEFAULT_INSTALL_ROOT="/usr/local/share/claude-history"
fi
INSTALL_ROOT="${INSTALL_ROOT:-$DEFAULT_INSTALL_ROOT}"
BIN_DIR="${BIN_DIR:-$DEFAULT_BIN_DIR}"
CLAUDE_COMMAND_NAME="${CLAUDE_COMMAND_NAME:-claude-history}"
CODEX_COMMAND_NAME="${CODEX_COMMAND_NAME:-codex-history}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
REQUIRE_CLOUDFLARED="${REQUIRE_CLOUDFLARED:-0}"

die() {
  echo "Error: $*" >&2
  exit 1
}

info() {
  echo "[deploy] $*"
}

require_file() {
  local path="$1"
  [[ -f "$path" ]] || die "Missing required file: $path"
}

if [[ "${EUID}" -ne 0 ]]; then
  die "Run as root or with sudo."
fi

command -v install >/dev/null 2>&1 || die "'install' command not found"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "Python interpreter not found: $PYTHON_BIN"
command -v bash >/dev/null 2>&1 || die "'bash' command not found"

"$PYTHON_BIN" - <<'PY' || die "Python 3.10+ is required."
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY

require_file "$SCRIPT_DIR/export_utils.py"
require_file "$SCRIPT_DIR/claude_history_viewer.py"
require_file "$SCRIPT_DIR/codex_history_viewer.py"
require_file "$SCRIPT_DIR/claude-history"
require_file "$SCRIPT_DIR/codex-history"

if [[ "$REQUIRE_CLOUDFLARED" == "1" ]]; then
  command -v cloudflared >/dev/null 2>&1 || die "cloudflared is required but not installed"
fi

info "Installing application files into $INSTALL_ROOT"
install -d -m 0755 "$INSTALL_ROOT"
install -m 0644 "$SCRIPT_DIR/export_utils.py" "$INSTALL_ROOT/export_utils.py"
install -m 0644 "$SCRIPT_DIR/claude_history_viewer.py" "$INSTALL_ROOT/claude_history_viewer.py"
install -m 0644 "$SCRIPT_DIR/codex_history_viewer.py" "$INSTALL_ROOT/codex_history_viewer.py"
install -m 0644 "$SCRIPT_DIR/CLAUDE_RESUME_OBSERVATIONS.md" "$INSTALL_ROOT/CLAUDE_RESUME_OBSERVATIONS.md"
install -m 0755 "$SCRIPT_DIR/claude-history" "$INSTALL_ROOT/claude-history"
install -m 0755 "$SCRIPT_DIR/codex-history" "$INSTALL_ROOT/codex-history"

info "Installing launcher commands into $BIN_DIR"
install -d -m 0755 "$BIN_DIR"

cat > "$BIN_DIR/$CLAUDE_COMMAND_NAME" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export PYTHON_BIN="${PYTHON_BIN}"
export CLAUDE_HISTORY_VIEWER_PY="$INSTALL_ROOT/claude_history_viewer.py"
exec "$INSTALL_ROOT/claude-history" "\$@"
EOF

cat > "$BIN_DIR/$CODEX_COMMAND_NAME" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export PYTHON_BIN="${PYTHON_BIN}"
export CODEX_HISTORY_VIEWER_PY="$INSTALL_ROOT/codex_history_viewer.py"
exec "$INSTALL_ROOT/codex-history" "\$@"
EOF

chmod 0755 "$BIN_DIR/$CLAUDE_COMMAND_NAME" "$BIN_DIR/$CODEX_COMMAND_NAME"

info "Running post-install validation"
"$PYTHON_BIN" -m py_compile \
  "$INSTALL_ROOT/export_utils.py" \
  "$INSTALL_ROOT/claude_history_viewer.py" \
  "$INSTALL_ROOT/codex_history_viewer.py"
bash -n "$INSTALL_ROOT/claude-history" "$INSTALL_ROOT/codex-history"
bash -n "$BIN_DIR/$CLAUDE_COMMAND_NAME" "$BIN_DIR/$CODEX_COMMAND_NAME"

CLAUDE_HELP="$("$PYTHON_BIN" "$INSTALL_ROOT/claude_history_viewer.py" --help >/dev/null 2>&1 && echo ok || true)"
CODEX_HELP="$("$PYTHON_BIN" "$INSTALL_ROOT/codex_history_viewer.py" --help >/dev/null 2>&1 && echo ok || true)"
[[ "$CLAUDE_HELP" == "ok" ]] || die "Installed Claude viewer failed --help validation"
[[ "$CODEX_HELP" == "ok" ]] || die "Installed Codex viewer failed --help validation"

echo "Installed successfully:"
echo "  platform: $UNAME_S"
echo "  install root: $INSTALL_ROOT"
echo "  shared module: $INSTALL_ROOT/export_utils.py"
echo "  claude viewer: $INSTALL_ROOT/claude_history_viewer.py"
echo "  claude wrapper: $INSTALL_ROOT/claude-history"
echo "  claude command: $BIN_DIR/$CLAUDE_COMMAND_NAME"
echo "  codex viewer: $INSTALL_ROOT/codex_history_viewer.py"
echo "  codex wrapper: $INSTALL_ROOT/codex-history"
echo "  codex command: $BIN_DIR/$CODEX_COMMAND_NAME"
echo
echo "Available to all users with $BIN_DIR in PATH:"
echo "  $CLAUDE_COMMAND_NAME"
echo "  $CODEX_COMMAND_NAME"
echo
echo "Notes:"
echo "  - requires Python 3.10+"
echo "  - local export and local bundle serving work with Python only"
echo "  - temporary public tunnel mode also requires 'cloudflared' in PATH"
if [[ "$UNAME_S" == "Darwin" ]]; then
  echo "  - on macOS, ensure /usr/local/bin is in PATH for all intended users"
fi
if command -v cloudflared >/dev/null 2>&1; then
  echo "  - detected cloudflared: $(cloudflared --version | head -n 1)"
else
  echo "  - cloudflared not detected; tunnel mode will fail until installed"
fi
