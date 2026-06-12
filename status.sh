#!/usr/bin/env bash
# Check the current status of adam-control on his computer.
#
# Usage:
#   ./status.sh          — show full status dashboard (terminal)
#   ./status.sh web      — open the ranked web dashboard in your browser
#   ./status.sh logs     — tail live daemon logs
#   ./status.sh proxy    — tail live proxy logs

set -euo pipefail

DEFAULT_TARGET="adams-pc"
TARGET="${2:-$DEFAULT_TARGET}"
CMD="${1:-status}"

case "$CMD" in
  status|"")
    ssh "$TARGET" "sudo python3 /opt/adam-control/status.py"
    ;;
  web)
    echo "Opening http://localhost:8765 (via SSH tunnel to $TARGET)..."
    echo "Press Ctrl+C to close the tunnel when done."
    ( sleep 2; xdg-open http://localhost:8765 >/dev/null 2>&1 || \
        echo "→ open http://localhost:8765 in your browser" ) &
    ssh -L 8765:localhost:8765 "$TARGET"
    ;;
  logs)
    ssh "$TARGET" "sudo journalctl -u adam-control -f"
    ;;
  proxy)
    ssh "$TARGET" "sudo journalctl -u adam-control-proxy -f"
    ;;
  *)
    echo "Usage: ./status.sh [status|web|logs|proxy]"
    exit 1
    ;;
esac
