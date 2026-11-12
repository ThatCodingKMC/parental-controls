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
  python3 grant.py pause          — pause free time (remaining minutes are saved)
  python3 grant.py resume         — resume paused free time

A paused free grant is stored as {"mode": "free", "paused": true,
"remaining": <seconds>}. While paused the clock doesn't run and the daemon
treats it as no grant (school-only). Adam can pause/resume his own time via
freetime.py; only the parent can add time.
"""

import json
import os
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
    # Atomic replace so the daemon never reads a half-written file.
    p = Path(GRANT_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)


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
    existing = _load()
    if mode == "free" and extend and existing.get("paused"):
        # Adding to paused time: bank it, stay paused — he starts it himself.
        total = int(existing.get("remaining", 0)) + minutes * 60
        _save({"mode": "free", "paused": True, "remaining": total})
        print(f"Added {minutes} min to his paused free time ({total // 60} min saved, still paused)")
        _tell_adam(f"🎉 You earned {minutes} min of free time",
                   f"You have {total // 60} min saved. Press Free Time to start it.")
        return
    until = _until_str(minutes, extend)
    data = {"mode": mode, "until": until}
    _save(data)
    until_dt = datetime.fromisoformat(until)
    action = "Extended" if extend else "Granted"
    print(f"{action}: {mode} mode until {until_dt.strftime('%H:%M:%S')} ({minutes} min)")
    if mode == "free":
        _tell_adam(f"🎉 You earned {minutes} min of free time",
                   f"Free time until {until_dt.strftime('%-I:%M %p')}.")


def _tell_adam(title: str, body: str):
    """Best-effort desktop toast so Adam sees the award land."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import enforcer
        enforcer._notify("adam", title, body)
    except Exception:
        pass


def paused_grant() -> dict | None:
    """The paused free grant, if there is one."""
    data = _load()
    return data if data.get("paused") else None


def remaining_seconds() -> int:
    """Seconds of free time left, running or paused (0 if none)."""
    data = _load()
    if data.get("mode") != "free":
        return 0
    if data.get("paused"):
        return max(0, int(data.get("remaining", 0)))
    try:
        return max(0, int((datetime.fromisoformat(data["until"]) - datetime.now()).total_seconds()))
    except Exception:
        return 0


def pause() -> int | None:
    """Pause a running free grant. Returns seconds saved, or None if there
    was no running free time to pause."""
    g = active_grant()
    if not g or g.get("mode") != "free" or not g.get("until"):
        return None
    left = remaining_seconds()
    if left <= 0:
        _clear()
        return None
    _save({"mode": "free", "paused": True, "remaining": left})
    return left


def resume() -> datetime | None:
    """Restart paused free time. Returns the new end time, or None if
    nothing was paused. Never creates time: it only restarts what was saved."""
    data = _load()
    if not data.get("paused"):
        return None
    left = max(0, int(data.get("remaining", 0)))
    if left <= 0:
        _clear()
        return None
    until = datetime.now().replace(microsecond=0) + timedelta(seconds=left)
    _save({"mode": "free", "until": until.isoformat(timespec="seconds")})
    return until


def cmd_clear():
    _clear()
    print("Grant cleared — back to normal schedule.")


def cmd_status():
    data = _load()
    if not data:
        print("No active grant — schedule is in control.")
        return

    mode = data.get("mode", "?")
    if data.get("paused"):
        print(f"Free time PAUSED — {int(data.get('remaining', 0)) // 60} min saved (Adam starts it with the Free Time button)")
        return
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
    if not data or data.get("paused"):
        return None   # paused free time = no grant in effect
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
    elif cmd == "pause":
        left = pause()
        print(f"Paused — {left // 60} min saved." if left else "No running free time to pause.")
    elif cmd == "resume":
        until = resume()
        print(f"Resumed — free time until {until:%H:%M:%S}." if until else "Nothing paused.")
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
