#!/usr/bin/env python3
"""
Print current state and today's usage.
Usage:  sudo python3 /opt/adam-control/status.py
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import tracker
import grant as grant_mod

CONFIG_PATH = "/etc/adam-control/schedule.yaml"
PROXY_RULES = "/var/lib/adam-control/proxy_rules.json"


def _parse_time(s):
    from datetime import time as dtime
    h, m = map(int, s.split(":"))
    return dtime(h, m)


def _in_window(now, start_str, end_str):
    start = _parse_time(start_str)
    end   = _parse_time(end_str)
    t = now.time().replace(second=0, microsecond=0)
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def bar(used, limit, width=20):
    if limit == 0:
        return "[" + "∞" * width + "]"
    filled = min(width, int(used / limit * width))
    color = "\033[92m" if filled < width * 0.7 else "\033[93m" if filled < width else "\033[91m"
    return color + "[" + "█" * filled + "░" * (width - filled) + "]\033[0m"


def main():
    try:
        config = yaml.safe_load(open(CONFIG_PATH))
    except FileNotFoundError:
        print("Config not found. Is the daemon installed?")
        sys.exit(1)

    username = config.get("target_user", "adam")
    now      = datetime.now()
    day      = now.weekday()
    day_key  = "weekend" if day >= 5 else "weekday"
    strategy = config.get("mode_strategy", "schedule")

    proxy = {}
    if Path(PROXY_RULES).exists():
        try:
            proxy = json.loads(Path(PROXY_RULES).read_text())
        except Exception:
            pass

    active_grant = grant_mod.active_grant()

    print(f"\n╔══ adam-control @ {now.strftime('%A %H:%M:%S')} {'═'*24}")
    print(f"║  User:      {username}  (active: {tracker.is_user_active(username)})")
    print(f"║  Strategy:  {strategy}")
    print(f"║  Mode now:  {proxy.get('mode', '?').upper()}", end="")
    if active_grant:
        print(f"  ← GRANT until {active_grant.get('until', '?')[11:16]}", end="")
    print()

    # ── Session cap ──
    daily_limit = config.get("daily_limit", {}).get(day_key, 0)
    sess_sec  = tracker.get_session_seconds(username)
    sess_min  = sess_sec // 60
    print(f"╠══ Session {'═'*40}")
    if daily_limit > 0:
        print(f"║  {bar(sess_min, daily_limit)}  {sess_min}m / {daily_limit}m")
    else:
        print(f"║  {sess_min}m used today  (no hard cap)")

    # ── Free budget ──
    if strategy == "budget":
        free_limit = config.get("budget", {}).get("free_minutes", {}).get(day_key, 0)
        free_min   = tracker.get_free_used_seconds(username) // 60
        avail      = config.get("budget", {}).get("available_hours", {})
        avail_str  = ""
        if avail and avail.get("start", "00:00") != "00:00":
            avail_str = f"  (available {avail['start']}–{avail['end']})"
        print(f"╠══ Free Budget {'═'*36}")
        print(f"║  {bar(free_min, free_limit)}  {free_min}m / {free_limit}m{avail_str}")

    # ── Schedule (shown even in budget mode for reference) ──
    windows = config.get("schedule", {}).get(day_key, [])
    if windows:
        print(f"╠══ Schedule ({day_key}) {'═'*32}")
        for w in windows:
            marker = " ◄ NOW" if _in_window(now, w["start"], w["end"]) else ""
            print(f"║  {w['start']}–{w['end']}  [{w.get('mode','?'):8}]{marker}")

    # ── Per-site limits ──
    site_limits = config.get("site_limits", [])
    if site_limits:
        print(f"╠══ Site Limits {'═'*36}")
        for row in tracker.site_usage_report(site_limits):
            status = "\033[91m✗ BLOCKED\033[0m" if row["reached"] else "\033[92m✓\033[0m"
            print(f"║  {bar(row['used_min'], row['limit_min'], 12)}  "
                  f"{row['site']:28} {row['used_min']:3}m/{row['limit_min']:3}m  {status}")

    # ── Per-app limits ──
    app_limits = config.get("app_limits", [])
    if app_limits:
        print(f"╠══ App Limits {'═'*37}")
        for row in tracker.app_usage_report(app_limits):
            status = "\033[91m✗ BLOCKED\033[0m" if row["reached"] else "\033[92m✓\033[0m"
            print(f"║  {bar(row['used_min'], row['limit_min'], 12)}  "
                  f"{row['app']:28} {row['used_min']:3}m/{row['limit_min']:3}m  {status}")

    # ── Proxy live state ──
    allow  = proxy.get("allow_domains", [])
    blocks = proxy.get("block_domains", [])
    urls   = proxy.get("block_url_patterns", [])
    hit    = proxy.get("site_limits_reached", [])
    print(f"╠══ Proxy (live) {'═'*35}")
    if allow:
        print(f"║  WHITELIST mode: {len(allow)} allowed domains")
    else:
        print(f"║  Blacklist: {len(blocks)} domains, {len(urls)} URL patterns")
    if hit:
        print(f"║  Site limits hit today: {', '.join(hit)}")

    # ── Grant ──
    if active_grant:
        print(f"╠══ Active Grant {'═'*35}")
        print(f"║  Mode: {active_grant.get('mode','?').upper()}  until {active_grant.get('until','?')[11:16]}")

    print(f"╚{'═'*51}\n")


if __name__ == "__main__":
    main()
