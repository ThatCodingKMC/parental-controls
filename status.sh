#!/usr/bin/env bash
# Check the current status of adam-control on his computer.
#
# Usage:
#   ./status.sh          — show full status dashboard
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
  logs)
    ssh "$TARGET" "sudo journalctl -u adam-control -f"
    ;;
  proxy)
    ssh "$TARGET" "sudo journalctl -u adam-control-proxy -f"
    ;;
  *)
    echo "Usage: ./status.sh [status|logs|proxy]"
    exit 1
    ;;
esac
