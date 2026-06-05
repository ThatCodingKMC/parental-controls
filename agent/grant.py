#!/usr/bin/env python3
"""
Grant management for adam-control.
Runs on Adam's machine (as root).

Usage:
  python3 grant.py free 60        — give 60 min of free time from now
  python3 grant.py free 60 +      — extend current free grant by 60 min
  python3 grant.py work 30        — force work mode for 30 min
  python3 grant.py clear          — remove grant, return to normal schedule
  python3 grant.py status         — show current grant
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

GRANT_FILE = "/var/lib/adam-control/grant.json"


def _load() -> dict:
    try:
        p = Path(GRANT_FILE)
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {}


def _save(data: dict):
    Path(GRANT_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(GRANT_FILE).write_text(json.dumps(data, indent=2))


def _clear():
    p = Path(GRANT_FILE)
    if p.exists():
        p.unlink()


def _until_str(minutes: int, extend: bool) -> str:
    existing = _load()
    if extend and existing.get("until"):
        try:
            base = datetime.fromisoformat(existing["until"])
            base = max(base, datetime.now())  # don't extend from the past
        except Exception:
            base = datetime.now()
    else:
        base = datetime.now()
    return (base + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def cmd_grant(mode: str, minutes: int, extend: bool):
    until = _until_str(minutes, extend)
    data = {"mode": mode, "until": until}
    _save(data)
    until_dt = datetime.fromisoformat(until)
    action = "Extended" if extend else "Granted"
    print(f"{action}: {mode} mode until {until_dt.strftime('%H:%M:%S')} ({minutes} min)")


def cmd_clear():
    _clear()
    print("Grant cleared — back to normal schedule.")


def cmd_status():
    data = _load()
    if not data:
        print("No active grant — schedule is in control.")
        return

    mode = data.get("mode", "?")
    until_str = data.get("until")
    if until_str:
        until_dt = datetime.fromisoformat(until_str)
        now = datetime.now()
        if until_dt <= now:
            print("Grant expired — schedule is in control.")
            _clear()
            return
        remaining = int((until_dt - now).total_seconds() // 60)
        print(f"Active grant: {mode.upper()} mode until {until_dt.strftime('%H:%M:%S')} ({remaining} min remaining)")
    else:
        print(f"Active grant: {mode.upper()} mode (indefinite — use 'clear' to remove)")


def active_grant() -> dict | None:
    """Called by daemon.py. Returns grant dict if valid, else None."""
    data = _load()
    if not data:
        return None
    until_str = data.get("until")
    if until_str:
        try:
            if datetime.fromisoformat(until_str) <= datetime.now():
                _clear()
                return None
        except Exception:
            return None
    return data


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    cmd = args[0].lower()

    if cmd == "clear":
        cmd_clear()
    elif cmd == "status":
        cmd_status()
    elif cmd in ("free", "work"):
        if len(args) < 2:
            print(f"Usage: grant.py {cmd} <minutes> [+]")
            sys.exit(1)
        try:
            minutes = int(args[1])
        except ValueError:
            print("Minutes must be a number.")
            sys.exit(1)
        extend = len(args) >= 3 and args[2] == "+"
        cmd_grant(cmd, minutes, extend)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
