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

warn() {
  echo "[deploy] Warning: $*" >&2
}

prompt_yes_no() {
  local prompt="$1"
  local answer
  while true; do
    printf "%s [y/N]: " "$prompt" >&2
    IFS= read -r answer || return 1
    case "${answer,,}" in
      y|yes) return 0 ;;
      n|no|"") return 1 ;;
    esac
  done
}

is_interactive() {
  [[ -t 0 && -t 1 ]]
}

install_hint_for() {
  local tool="$1"
  case "$tool" in
    cloudflared)
      if [[ "$UNAME_S" == "Darwin" ]]; then
        echo "brew install cloudflared"
      else
        echo "Install cloudflared from your package manager or from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
      fi
      ;;
    claude)
      echo "Install the Claude Code CLI and ensure 'claude' is in PATH for all intended users."
      ;;
    codex)
      echo "Install the Codex CLI and ensure 'codex' is in PATH for all intended users."
      ;;
    *)
      echo "Install '$tool' and ensure it is in PATH."
      ;;
  esac
}

require_file() {
  local path="$1"
  [[ -f "$path" ]] || die "Missing required file: $path"
}

detect_tool_version() {
  local tool="$1"
  local version_output=""
  if ! command -v "$tool" >/dev/null 2>&1; then
    return 1
  fi
  version_output="$("$tool" --version 2>/dev/null | head -n 1 || true)"
  if [[ -z "$version_output" ]]; then
    version_output="$("$tool" version 2>/dev/null | head -n 1 || true)"
  fi
  if [[ -n "$version_output" ]]; then
    printf '%s\n' "$version_output"
  else
    printf 'installed (version unavailable)\n'
  fi
  return 0
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

PYTHON_VERSION="$("$PYTHON_BIN" -V 2>&1)"
CLOUDFLARED_STATUS="missing"
CLOUDFLARED_VERSION=""
if CLOUDFLARED_VERSION="$(detect_tool_version cloudflared)"; then
  CLOUDFLARED_STATUS="ok"
fi

CLAUDE_STATUS="missing"
CLAUDE_VERSION=""
if CLAUDE_VERSION="$(detect_tool_version claude)"; then
  CLAUDE_STATUS="ok"
fi

CODEX_STATUS="missing"
CODEX_VERSION=""
if CODEX_VERSION="$(detect_tool_version codex)"; then
  CODEX_STATUS="ok"
fi

if [[ "$CLOUDFLARED_STATUS" != "ok" ]]; then
  warn "cloudflared was not detected or is not runnable."
  warn "Tunnel mode will not work until it is installed."
  warn "Install hint: $(install_hint_for cloudflared)"
  if [[ "$REQUIRE_CLOUDFLARED" == "1" ]]; then
    die "cloudflared is required for this deployment."
  fi
  if is_interactive; then
    if ! prompt_yes_no "Continue installation without cloudflared support for tunnel mode?"; then
      die "Installation aborted so cloudflared can be installed first."
    fi
  fi
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
echo "  - detected python: $PYTHON_VERSION"
echo "  - local export and local bundle serving work with Python only"
echo "  - temporary public tunnel mode requires a working 'cloudflared' in PATH"
if [[ "$UNAME_S" == "Darwin" ]]; then
  echo "  - on macOS, ensure /usr/local/bin is in PATH for all intended users"
fi

if [[ "$CLAUDE_STATUS" == "ok" ]]; then
  echo "  - detected claude: $CLAUDE_VERSION"
else
  echo "  - claude not detected; Claude resume commands will fail until installed"
  echo "    install hint: $(install_hint_for claude)"
fi

if [[ "$CODEX_STATUS" == "ok" ]]; then
  echo "  - detected codex: $CODEX_VERSION"
else
  echo "  - codex not detected; Codex resume and fork commands will fail until installed"
  echo "    install hint: $(install_hint_for codex)"
fi

if [[ "$CLOUDFLARED_STATUS" == "ok" ]]; then
  echo "  - detected cloudflared: $CLOUDFLARED_VERSION"
else
  echo "  - cloudflared not detected; tunnel mode will fail until installed"
  echo "    install hint: $(install_hint_for cloudflared)"
fi
