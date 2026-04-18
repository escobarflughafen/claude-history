#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/claude-history}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
CLAUDE_COMMAND_NAME="${CLAUDE_COMMAND_NAME:-claude-history}"
CODEX_COMMAND_NAME="${CODEX_COMMAND_NAME:-codex-history}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root or with sudo." >&2
  exit 1
fi

install -d -m 0755 "$INSTALL_ROOT"
install -m 0644 "$SCRIPT_DIR/export_utils.py" "$INSTALL_ROOT/export_utils.py"
install -m 0644 "$SCRIPT_DIR/claude_history_viewer.py" "$INSTALL_ROOT/claude_history_viewer.py"
install -m 0644 "$SCRIPT_DIR/codex_history_viewer.py" "$INSTALL_ROOT/codex_history_viewer.py"
install -m 0644 "$SCRIPT_DIR/CLAUDE_RESUME_OBSERVATIONS.md" "$INSTALL_ROOT/CLAUDE_RESUME_OBSERVATIONS.md"
install -m 0755 "$SCRIPT_DIR/claude-history" "$INSTALL_ROOT/claude-history"
install -m 0755 "$SCRIPT_DIR/codex-history" "$INSTALL_ROOT/codex-history"

cat > "$BIN_DIR/$CLAUDE_COMMAND_NAME" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export CLAUDE_HISTORY_VIEWER_PY="$INSTALL_ROOT/claude_history_viewer.py"
exec "$INSTALL_ROOT/claude-history" "\$@"
EOF

cat > "$BIN_DIR/$CODEX_COMMAND_NAME" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export CODEX_HISTORY_VIEWER_PY="$INSTALL_ROOT/codex_history_viewer.py"
exec "$INSTALL_ROOT/codex-history" "\$@"
EOF

chmod 0755 "$BIN_DIR/$CLAUDE_COMMAND_NAME" "$BIN_DIR/$CODEX_COMMAND_NAME"

echo "Installed:"
echo "  shared module: $INSTALL_ROOT/export_utils.py"
echo "  viewer: $INSTALL_ROOT/claude_history_viewer.py"
echo "  wrapper: $INSTALL_ROOT/claude-history"
echo "  command: $BIN_DIR/$CLAUDE_COMMAND_NAME"
echo "  viewer: $INSTALL_ROOT/codex_history_viewer.py"
echo "  wrapper: $INSTALL_ROOT/codex-history"
echo "  command: $BIN_DIR/$CODEX_COMMAND_NAME"
echo
echo "All users with $BIN_DIR in PATH can run:"
echo "  $CLAUDE_COMMAND_NAME"
echo "  $CODEX_COMMAND_NAME"
