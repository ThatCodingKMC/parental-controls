#!/usr/bin/env bash
# Grant or revoke computer time from your machine.
#
# Usage:
#   ./grant.sh free 60      — give 60 min of free time starting now
#   ./grant.sh free 30 +    — extend current free grant by 30 min
#   ./grant.sh work 45      — force work mode for 45 min
#   ./grant.sh clear        — cancel grant, back to normal schedule
#   ./grant.sh status       — show what's currently active

set -euo pipefail

DEFAULT_TARGET="adams-pc"

# Allow passing a different host as the last argument
ARGS=("$@")
if [[ ${#ARGS[@]} -gt 0 && "${ARGS[-1]}" != *"+"* ]] && \
   [[ "${ARGS[-1]}" != "free" && "${ARGS[-1]}" != "work" && \
      "${ARGS[-1]}" != "clear" && "${ARGS[-1]}" != "status" ]] && \
   [[ ! "${ARGS[-1]}" =~ ^[0-9]+$ ]]; then
    TARGET="${ARGS[-1]}"
    ARGS=("${ARGS[@]::${#ARGS[@]}-1}")
else
    TARGET="$DEFAULT_TARGET"
fi

if [[ ${#ARGS[@]} -eq 0 ]]; then
    echo "Usage: ./grant.sh <free|work|clear|status> [minutes] [+] [host]"
    exit 1
fi

CMD="${ARGS[0]}"

case "$CMD" in
  free|work)
    if [[ ${#ARGS[@]} -lt 2 ]]; then
        echo "Usage: ./grant.sh $CMD <minutes> [+]"
        exit 1
    fi
    MINUTES="${ARGS[1]}"
    EXTEND="${ARGS[2]:-}"
    ssh "$TARGET" "sudo python3 /opt/adam-control/grant.py $CMD $MINUTES ${EXTEND:+$EXTEND}"
    ;;
  clear)
    ssh "$TARGET" "sudo python3 /opt/adam-control/grant.py clear"
    ;;
  status)
    ssh "$TARGET" "sudo python3 /opt/adam-control/grant.py status"
    ;;
  *)
    echo "Unknown command: $CMD"
    echo "Commands: free, work, clear, status"
    exit 1
    ;;
esac
