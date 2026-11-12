#!/usr/bin/python3 -Es
"""
Adam's own Free Time switch. He runs it through sudo (a sudoers rule allows
exactly this file, no password), so it can only pause or resume time his
parent already awarded. It can't add time.

  freetime          — toggle: pause if running, start if paused
  freetime pause
  freetime resume
  freetime status

The daemon watches the grant file and applies the change within a second
or two. Pausing closes the browser and goes back to school-only mode.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grant
import enforcer

USER = "adam"


def _tell(title: str, body: str):
    print(f"{title}: {body}")
    enforcer._notify(USER, title, body)


def _mins(secs: int) -> str:
    m = max(1, round(secs / 60)) if secs > 0 else 0
    return f"{m} min"


def do_pause():
    left = grant.pause()
    if left:
        _tell("⏸ Free time paused",
              f"{_mins(left)} saved for later. Back to school-only mode.")
    else:
        do_status()


def do_resume():
    until = grant.resume()
    if until:
        _tell("▶ Free time started",
              f"{_mins(grant.remaining_seconds())} — runs until {until:%-I:%M %p}. "
              "Press Free Time again to pause it.")
    else:
        do_status()


def do_status():
    if grant.paused_grant():
        _tell("⏸ Free time is paused",
              f"{_mins(grant.remaining_seconds())} saved. Press Free Time to start it.")
    elif (g := grant.active_grant()) and g.get("mode") == "free":
        _tell("▶ Free time is running",
              f"{_mins(grant.remaining_seconds())} left. Press Free Time to pause it.")
    else:
        _tell("No free time saved", "Finish chores or work and ask Dad to award some.")


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "toggle").lower()
    if cmd == "toggle":
        if grant.paused_grant():
            do_resume()
        elif (g := grant.active_grant()) and g.get("mode") == "free":
            do_pause()
        else:
            do_status()
    elif cmd == "pause":
        do_pause()
    elif cmd in ("resume", "start"):
        do_resume()
    elif cmd == "status":
        do_status()
    else:
        print("Usage: freetime [toggle|pause|resume|status]")
        sys.exit(1)


if __name__ == "__main__":
    main()
