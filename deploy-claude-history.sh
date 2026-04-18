#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/claude-history}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
COMMAND_NAME="${COMMAND_NAME:-claude-history}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root or with sudo." >&2
  exit 1
fi

install -d -m 0755 "$INSTALL_ROOT"
install -m 0644 "$SCRIPT_DIR/claude_history_viewer.py" "$INSTALL_ROOT/claude_history_viewer.py"
install -m 0644 "$SCRIPT_DIR/CLAUDE_RESUME_OBSERVATIONS.md" "$INSTALL_ROOT/CLAUDE_RESUME_OBSERVATIONS.md"
install -m 0755 "$SCRIPT_DIR/claude-history" "$INSTALL_ROOT/claude-history"

cat > "$BIN_DIR/$COMMAND_NAME" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export CLAUDE_HISTORY_VIEWER_PY="$INSTALL_ROOT/claude_history_viewer.py"
exec "$INSTALL_ROOT/claude-history" "\$@"
EOF

chmod 0755 "$BIN_DIR/$COMMAND_NAME"

echo "Installed:"
echo "  viewer: $INSTALL_ROOT/claude_history_viewer.py"
echo "  wrapper: $INSTALL_ROOT/claude-history"
echo "  command: $BIN_DIR/$COMMAND_NAME"
echo
echo "All users with $BIN_DIR in PATH can run:"
echo "  $COMMAND_NAME"
