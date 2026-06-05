"""
Screen time, free-budget, and per-app/site usage tracker.

Storage at /var/lib/adam-control/usage.json — all counters keyed by
"{category}:{name}:{date}" with integer second values.

Key prefixes:
  session:{username}:{date}    — total active session seconds
  free:{username}:{date}       — free-mode session seconds (budget depletion)
  site:{domain}:{date}         — seconds on a site (from proxy timestamp buckets)
  app:{procname}:{date}        — seconds an app was running
"""

import json
import os
import subprocess
from datetime import date
from pathlib import Path

STATE_FILE      = "/var/lib/adam-control/usage.json"
SITE_USAGE_FILE = "/var/lib/adam-control/site_usage.json"
STATE_DIR       = "/var/lib/adam-control"


# ── I/O helpers ───────────────────────────────────────────────────────────────

def _load(path: str) -> dict:
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {}


def _save(path: str, data: dict):
    Path(STATE_DIR).mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2))


def _today() -> str:
    return str(date.today())


def _purge_old(data: dict, keep_days: int = 30) -> dict:
    cutoff = date.today().toordinal() - keep_days
    return {k: v for k, v in data.items() if _key_ordinal(k) >= cutoff}


def _key_ordinal(key: str) -> int:
    # Keys end with :YYYY-MM-DD
    try:
        return date.fromisoformat(key.rsplit(":", 1)[-1]).toordinal()
    except Exception:
        return 0


def _add(key: str, seconds: int):
    data = _load(STATE_FILE)
    data[key] = data.get(key, 0) + seconds
    data = _purge_old(data)
    _save(STATE_FILE, data)


def _get(key: str) -> int:
    return _load(STATE_FILE).get(key, 0)


# ── Session presence ──────────────────────────────────────────────────────────

def is_user_active(username: str) -> bool:
    """True if the user has an unlocked graphical session."""
    try:
        result = subprocess.run(
            ["loginctl", "list-sessions", "--no-legend"],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == username:
                info = subprocess.run(
                    ["loginctl", "show-session", parts[0], "-p", "Active"],
                    capture_output=True, text=True
                )
                if "Active=yes" in info.stdout:
                    return True
    except Exception:
        pass
    return False


# ── Total session time ────────────────────────────────────────────────────────

def add_session_time(username: str, seconds: int):
    _add(f"session:{username}:{_today()}", seconds)


def get_session_seconds(username: str) -> int:
    return _get(f"session:{username}:{_today()}")


def get_remaining_minutes(username: str, limit_minutes: int) -> int:
    if limit_minutes == 0:
        return 9999
    used = get_session_seconds(username)
    return max(0, (limit_minutes * 60 - used) // 60)


def is_limit_reached(username: str, limit_minutes: int) -> bool:
    if limit_minutes == 0:
        return False
    return get_session_seconds(username) >= limit_minutes * 60


def format_session_report(username: str, limit_minutes: int) -> str:
    used_min = get_session_seconds(username) // 60
    if limit_minutes == 0:
        return f"Session: {used_min}m used  (no hard cap)"
    remaining = max(0, limit_minutes - used_min)
    return f"Session: {used_min}m / {limit_minutes}m  ({remaining}m remaining)"


# ── Free budget ───────────────────────────────────────────────────────────────

def add_free_time(username: str, seconds: int):
    """Deplete the free budget by seconds (called each cycle while in free mode)."""
    _add(f"free:{username}:{_today()}", seconds)


def get_free_used_seconds(username: str) -> int:
    return _get(f"free:{username}:{_today()}")


def get_free_remaining_minutes(username: str, limit_minutes: int) -> int:
    if limit_minutes == 0:
        return 9999
    used = get_free_used_seconds(username)
    return max(0, (limit_minutes * 60 - used) // 60)


def is_free_budget_exhausted(username: str, limit_minutes: int) -> bool:
    if limit_minutes == 0:
        return False
    return get_free_used_seconds(username) >= limit_minutes * 60


def format_budget_report(username: str, limit_minutes: int) -> str:
    used_min = get_free_used_seconds(username) // 60
    if limit_minutes == 0:
        return f"Free budget: {used_min}m used  (unlimited)"
    remaining = max(0, limit_minutes - used_min)
    return f"Free budget: {used_min}m / {limit_minutes}m  ({remaining}m remaining)"


# ── Per-site usage (written by proxy, read here) ──────────────────────────────

def get_site_usage_minutes(domain: str) -> int:
    """Minutes spent on domain today (1-min resolution from proxy timestamps)."""
    data = _load(SITE_USAGE_FILE)
    key = f"{domain}:{_today()}"
    timestamps = data.get(key, [])
    if not timestamps:
        return 0
    buckets = set(int(t) // 60 for t in timestamps)
    return len(buckets)


def is_site_limit_reached(domain: str, limit_minutes: int) -> bool:
    if limit_minutes <= 0:
        return False
    return get_site_usage_minutes(domain) >= limit_minutes


def get_site_remaining_minutes(domain: str, limit_minutes: int) -> int:
    if limit_minutes <= 0:
        return 9999
    return max(0, limit_minutes - get_site_usage_minutes(domain))


def site_usage_report(site_limits: list) -> list:
    rows = []
    for conf in site_limits:
        site  = conf["site"]
        limit = conf.get("minutes", 0)
        used  = get_site_usage_minutes(site)
        rows.append({
            "site":       site,
            "used_min":   used,
            "limit_min":  limit,
            "remaining":  max(0, limit - used),
            "reached":    is_site_limit_reached(site, limit),
            "applies_in": conf.get("applies_in", ["free"]),
        })
    return rows


# ── Per-app usage ─────────────────────────────────────────────────────────────

def add_app_time(app_name: str, seconds: int):
    """Record that app_name was running for seconds."""
    _add(f"app:{app_name.lower()}:{_today()}", seconds)


def get_app_usage_minutes(app_name: str) -> int:
    return _get(f"app:{app_name.lower()}:{_today()}") // 60


def is_app_limit_reached(app_name: str, limit_minutes: int) -> bool:
    if limit_minutes <= 0:
        return False
    return get_app_usage_minutes(app_name) >= limit_minutes


def get_app_remaining_minutes(app_name: str, limit_minutes: int) -> int:
    if limit_minutes <= 0:
        return 9999
    return max(0, limit_minutes - get_app_usage_minutes(app_name))


def app_usage_report(app_limits: list) -> list:
    rows = []
    for conf in app_limits:
        app   = conf["app"]
        limit = conf.get("minutes", 0)
        used  = get_app_usage_minutes(app)
        rows.append({
            "app":        app,
            "used_min":   used,
            "limit_min":  limit,
            "remaining":  max(0, limit - used),
            "reached":    is_app_limit_reached(app, limit),
            "applies_in": conf.get("applies_in", ["free"]),
        })
    return rows
