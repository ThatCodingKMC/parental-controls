#!/usr/bin/env bash
# Award Adam free time he's earned (chores, finished work).
#
# Usage:
#   ./award.sh 30      — add 30 min of free time (stacks onto any time he has left)
#   ./award.sh         — show how much awarded time he has left
#   ./award.sh stop    — end his free time now (browser closes, back to school-only)
#
# Free time also opens Discord. When it runs out he gets warnings at
# 30/10/5/1 min, then the browser closes and he's back to school-only mode.
#
# Adam can pause/resume it himself with the "Free Time" button on his panel
# (or `freetime` in a terminal). Paused time is saved, not used up. Time you
# award while it's paused is added to the saved time and stays paused.

set -euo pipefail

TARGET="adams-pc"
CMD="${1:-status}"

case "$CMD" in
  status)
    ssh "$TARGET" "sudo python3 /opt/adam-control/grant.py status" ;;
  stop)
    ssh "$TARGET" "sudo python3 /opt/adam-control/grant.py clear" ;;
  *)
    if [[ ! "$CMD" =~ ^[0-9]+$ ]]; then
      echo "Usage: ./award.sh <minutes> | stop | (no args = status)"; exit 1
    fi
    ssh "$TARGET" "sudo python3 /opt/adam-control/grant.py free $CMD +" ;;
esac
