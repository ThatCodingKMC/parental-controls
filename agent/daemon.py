#!/usr/bin/env python3
"""
adam-control daemon — runs as root via systemd on Adam's computer.

Every check_interval seconds it:
  1. Loads config + list files
  2. Determines mode: locked / work / free
     - Grant overrides schedule/budget (but not computer_hours lock)
     - mode_strategy: "schedule" → clock-based windows
     - mode_strategy: "budget"   → free-time pool depleted by active session time
  3. Writes proxy_rules.json (picked up by the mitmproxy addon within 5s)
  4. Enforces /etc/hosts blocklist, app kills, and session lock
  5. Tracks session time, free budget, and per-app usage
  6. Issues desktop warnings before limits hit
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import enforcer
import tracker
import grant as grant_mod

CONFIG_PATH  = "/etc/adam-control/schedule.yaml"
PROXY_RULES  = "/var/lib/adam-control/proxy_rules.json"
LOG_PATH     = "/var/log/adam-control.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("daemon")

_last_mode: Optional[str]     = None
_last_counting: Optional[bool] = None

# Sticky activity category — survives quiet periods (persistent apps, idle)
# so the budget keeps counting until he actually switches to schoolwork.
_last_category: Optional[str] = None   # "free" | "work" | None
_last_trigger: str            = ""

# Warning state — reset when coming out of locked
_warned_budget: set   = set()   # {5, 1} minutes warned
_warned_session: set  = set()
_warned_sites: dict   = {}      # site -> set of minutes warned
_warned_apps: dict    = {}      # app  -> set of minutes warned

# Apps over their daily limit — stays blocked until midnight
_apps_blocked_today: set = set()


# ── Config loading ────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _config_dir() -> Path:
    return Path(CONFIG_PATH).parent


def _clean_entries(text: str) -> list:
    """Parse a domain/app list: drop blank lines and both full-line and
    inline (`domain  # note`) comments, so a trailing comment can't get
    glued onto a domain and silently break the match."""
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _load_domain_dir(subpath: str) -> list:
    d = _config_dir() / "lists" / subpath
    if not d.is_dir():
        return []
    domains = []
    for f in sorted(d.glob("*.txt")):
        domains.extend(_clean_entries(f.read_text()))
    return list(dict.fromkeys(domains))


def _load_url_file(subpath: str) -> list:
    # URL patterns are regexes (may legitimately contain '#'), so only
    # full-line comments are stripped here, not inline.
    p = _config_dir() / "lists" / subpath / "urls.txt"
    if not p.exists():
        return []
    return [l.strip() for l in p.read_text().splitlines()
            if l.strip() and not l.strip().startswith("#")]


def _load_allowed_file(subpath: str) -> list:
    p = _config_dir() / "lists" / subpath / "allowed.txt"
    if not p.exists():
        return []
    return _clean_entries(p.read_text())


def _load_app_list(subpath: str) -> list:
    p = _config_dir() / "lists" / subpath / "blocked_apps.txt"
    if not p.exists():
        return []
    return _clean_entries(p.read_text())


# ── Schedule helpers ──────────────────────────────────────────────────────────

def _parse_time(s: str) -> dtime:
    h, m = map(int, s.split(":"))
    return dtime(h, m)


def _in_window(t: dtime, start: dtime, end: dtime) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # crosses midnight


def _hours_on_off(config: dict) -> tuple:
    """Read computer_hours on/off robustly.

    Bare `on:`/`off:` in YAML are coerced to the booleans True/False (the
    "Norway problem"), so a key lookup for "on"/"off" silently misses and the
    lock never engages. Accept every form — quoted strings, the coerced
    booleans, and wake/sleep aliases — and warn loudly if none are found.
    """
    h = config.get("computer_hours", {}) or {}
    on  = h.get("on",  h.get(True,  h.get("wake")))
    off = h.get("off", h.get(False, h.get("sleep")))
    if on is None or off is None:
        log.warning("computer_hours has no usable on/off (keys=%s) — lock disabled",
                    list(h.keys()))
        on  = on  or "00:00"
        off = off or "23:59"
    return str(on), str(off)


def _is_on_hours(config: dict) -> bool:
    on, off = _hours_on_off(config)
    now = datetime.now().time().replace(second=0, microsecond=0)
    return _in_window(now, _parse_time(on), _parse_time(off))


def _schedule_mode(config: dict) -> str:
    """Work out mode purely from clock-based schedule windows."""
    now = datetime.now().time().replace(second=0, microsecond=0)
    day = datetime.now().weekday()
    key = "weekend" if day >= 5 else "weekday"
    for w in config.get("schedule", {}).get(key, []):
        start = _parse_time(w["start"])
        end   = _parse_time(w["end"])
        if _in_window(now, start, end):
            return w.get("mode", "work")
    return "work"  # default within ON hours


def _budget_mode(config: dict, username: str) -> str:
    """Work out mode from free-time budget."""
    budget_cfg = config.get("budget", {})
    day = datetime.now().weekday()
    key = "weekend" if day >= 5 else "weekday"
    limit = budget_cfg.get("free_minutes", {}).get(key, 0)

    # Check available_hours restriction
    avail = budget_cfg.get("available_hours", {})
    if avail:
        start = _parse_time(avail.get("start", "00:00"))
        end   = _parse_time(avail.get("end",   "23:59"))
        now   = datetime.now().time().replace(second=0, microsecond=0)
        if not _in_window(now, start, end):
            return "work"

    if tracker.is_free_budget_exhausted(username, limit):
        return "work"

    return "free"


def current_mode(config: dict, username: str) -> str:
    """
    Priority:
      1. Outside computer_hours → locked  (grants cannot override)
      2. Active grant           → grant mode
      3. mode_strategy          → schedule or budget
    """
    if not _is_on_hours(config):
        return "locked"

    active = grant_mod.active_grant()
    if active:
        m = active.get("mode", "work")
        if m in ("free", "work"):
            return m

    strategy = config.get("mode_strategy", "schedule")
    if strategy == "budget":
        return _budget_mode(config, username)
    return _schedule_mode(config)


def _get_free_limit(config: dict) -> int:
    day = datetime.now().weekday()
    key = "weekend" if day >= 5 else "weekday"
    return config.get("budget", {}).get("free_minutes", {}).get(key, 0)


def _get_daily_limit(config: dict) -> int:
    day = datetime.now().weekday()
    key = "weekend" if day >= 5 else "weekday"
    return config.get("daily_limit", {}).get(key, 0)


# ── Proxy rules builder ───────────────────────────────────────────────────────

def build_proxy_rules(config: dict, mode: str, username: str) -> dict:
    if mode == "locked":
        return {"mode": "locked", "block_domains": [],
                "block_url_patterns": [], "allow_domains": [],
                "site_limits_reached": []}

    blocking = config.get("blocking", {})
    always   = blocking.get("always", {})
    work     = blocking.get("work", {})

    # Always-blocked domains apply in every mode
    block_domains = (
        list(always.get("block_domains") or [])
        + _load_domain_dir("always/blocked")
    )

    # URL patterns: always-on, plus work-specific if in work mode
    block_url_patterns = (
        list(always.get("block_urls") or [])
        + _load_url_file("always")
    )
    if mode == "work":
        block_url_patterns += (
            list(work.get("block_urls") or [])
            + _load_url_file("work")
        )

    # Whitelist only applies in work mode — free mode allows everything not always-blocked
    allow_domains = []
    if mode == "work":
        allow_domains = (
            list(work.get("allow_domains") or [])
            + _load_allowed_file("work")
        )

    site_limits_reached = [
        conf["site"]
        for conf in config.get("site_limits", [])
        if mode in conf.get("applies_in", ["free"])
        and tracker.is_site_limit_reached(conf["site"], conf.get("minutes", 0))
    ]

    return {
        "mode":                mode,
        "block_domains":       list(dict.fromkeys(block_domains)),
        "block_url_patterns":  list(dict.fromkeys(block_url_patterns)),
        "allow_domains":       list(dict.fromkeys(allow_domains)),
        "site_limits_reached": site_limits_reached,
    }


def write_proxy_rules(rules: dict):
    Path(PROXY_RULES).parent.mkdir(parents=True, exist_ok=True)
    Path(PROXY_RULES).write_text(json.dumps(rules, indent=2))


# ── Free activity detection ───────────────────────────────────────────────────
#
# Budget ticks only when free-category activity is detected.
# "Free activity" = a request to a work-blocked site, OR a known free app running.
# Work-only activity (Khan Academy, etc.) does NOT tick the budget.

def _domain_matches(domain: str, pattern: str) -> bool:
    """True if domain equals or is a subdomain of pattern (both www-stripped)."""
    domain  = domain.lower().lstrip("www.")
    pattern = pattern.lower().lstrip("www.")
    return domain == pattern or domain.endswith("." + pattern)


# Whitelisted "helper" domains (CDN / auth / static assets) load alongside
# basically every site, so they're neutral for classification: they never mark
# activity as free, and on their own they don't mark it as schoolwork either.
_HELPER_DOMAINS = {
    "gstatic.com", "googleapis.com", "googleusercontent.com", "gvt1.com",
    "gvt2.com", "msauth.net", "msftauth.net", "msecnd.net", "office.net",
    "officeapps.live.com", "sharepointonline.com", "live.com",
}

LAST_CAT_FILE = "/var/lib/adam-control/last_category.json"


def _load_last_category() -> tuple:
    try:
        d = json.loads(Path(LAST_CAT_FILE).read_text())
        return d.get("category"), d.get("trigger", "")
    except Exception:
        return None, ""


def _save_last_category(category: Optional[str], trigger: str):
    try:
        Path(LAST_CAT_FILE).write_text(
            json.dumps({"category": category, "trigger": trigger})
        )
    except Exception:
        pass


def _recent_site_category(config: dict, window_sec: int) -> tuple:
    """
    Classify recent browser activity from site_usage.json.

    Returns (category, trigger):
      "free" + domain → a non-whitelisted site was hit within the window
      "work" + ""     → only real school sites were hit (no free, no helpers)
      None   + ""     → nothing classifiable in the window (quiet / idle)

    Any single non-whitelisted hit wins (→ free), matching the non-gameable
    rule "any non-school activity counts." Helper/CDN domains are ignored so
    they neither trigger counting nor mask it during schoolwork.
    """
    import time as _time
    from datetime import date as _date

    work_whitelist = set(
        list(config.get("blocking", {}).get("work", {}).get("allow_domains") or [])
        + _load_allowed_file("work")
    )
    if not work_whitelist:
        log.warning("Work whitelist is empty — cannot classify activity")
        return None, ""

    try:
        data = json.loads(Path("/var/lib/adam-control/site_usage.json").read_text())
    except Exception:
        return None, ""

    cutoff_bucket = int((_time.time() - window_sec) // 60)
    today = str(_date.today())

    free_domain = ""
    has_work = False
    for key, buckets in data.items():
        if not key.endswith(f":{today}") or not buckets:
            continue
        if max(buckets) < cutoff_bucket:
            continue
        domain = key.rsplit(":", 1)[0]
        if any(_domain_matches(domain, h) for h in _HELPER_DOMAINS):
            continue  # neutral
        if any(_domain_matches(domain, w) for w in work_whitelist):
            has_work = True
        else:
            free_domain = domain  # any free hit wins

    if free_domain:
        return "free", free_domain
    if has_work:
        return "work", ""
    return None, ""


def _free_app_active(config: dict) -> tuple:
    """Return (detected: bool, app_name: str) for the first free-time app running.
    Free apps = things in app_limits + work blocked apps (blocked during work = leisure app).
    """
    free_apps = list(dict.fromkeys(
        [conf["app"] for conf in config.get("app_limits", [])]
        + list(config.get("blocking", {}).get("work", {}).get("block_apps") or [])
        + _load_app_list("work")
    ))
    if not free_apps:
        return False, ""
    running = enforcer.get_running_apps(free_apps)
    return (True, running[0]) if running else (False, "")


def _free_activity_detected(config: dict, interval: int) -> tuple:
    """
    Return (counting: bool, trigger: str, kind: str).

    VPN or a running native free app always counts. Browser activity uses a
    *sticky* category: once he's on a free site the budget keeps counting
    through quiet periods (persistent apps like web Discord, loaded videos,
    .io games) until a navigation to a school site flips him to "work".
    Without stickiness the budget barely moved, since loaded apps make almost
    no fresh HTTP requests for the proxy to see.
    """
    global _last_category, _last_trigger

    # VPN and native free apps are unambiguous — count immediately.
    vpn_hit, vpn_name = enforcer.vpn_active()
    if vpn_hit:
        return True, vpn_name, "vpn"
    app_hit, app_name = _free_app_active(config)
    if app_hit:
        return True, app_name, "app"

    # Browser activity, with sticky category across quiet gaps.
    window = max(interval * 3, 180)
    category, trigger = _recent_site_category(config, window)

    if category is None:
        # Nothing observed this window — hold the last known category.
        category, trigger = _last_category, _last_trigger
    elif category != _last_category or (trigger and trigger != _last_trigger):
        _last_category, _last_trigger = category, trigger
        _save_last_category(category, trigger)

    if category == "free":
        return True, trigger or "recent activity", "site"
    return False, "", ""


# ── Warning helpers ───────────────────────────────────────────────────────────

def _warn_at(current: int, thresholds: set, warned: set,
             send_fn, *args) -> set:
    """Call send_fn(*args, minutes) for each threshold not yet warned."""
    for t in sorted(thresholds, reverse=True):
        if current <= t and t not in warned:
            send_fn(*args, t)
            warned = warned | {t}
    return warned


# ── App enforcement ───────────────────────────────────────────────────────────

def _enforce_apps(config: dict, mode: str, username: str, interval: int):
    global _apps_blocked_today, _warned_apps

    blocking    = config.get("blocking", {})
    always_apps = (
        list(blocking.get("always", {}).get("block_apps") or [])
        + _load_app_list("always")
    )
    work_apps = (
        list(blocking.get("work", {}).get("block_apps") or [])
        + _load_app_list("work")
    ) if mode == "work" else []

    kill_now = list(dict.fromkeys(
        always_apps + work_apps + list(_apps_blocked_today)
    ))
    if kill_now:
        enforcer.kill_blocked_apps(kill_now)

    # Per-app time limits — only track during free mode
    if mode == "free":
        tracked = [
            conf["app"] for conf in config.get("app_limits", [])
            if "free" in conf.get("applies_in", ["free"])
        ]
        running = enforcer.get_running_apps(tracked)

        for app_conf in config.get("app_limits", []):
            app   = app_conf["app"]
            limit = app_conf.get("minutes", 0)
            if app not in running or limit <= 0:
                continue
            tracker.add_app_time(app, interval)
            if tracker.is_app_limit_reached(app, limit):
                log.info("App limit reached: %s (%d min)", app, limit)
                _apps_blocked_today.add(app)
                enforcer.kill_blocked_apps([app])
            else:
                remaining = tracker.get_app_remaining_minutes(app, limit)
                warned_set = _warned_apps.get(app, set())
                _warned_apps[app] = _warn_at(
                    remaining, {5, 1}, warned_set,
                    enforcer.send_app_warning, username, app
                )


# ── Main cycle ────────────────────────────────────────────────────────────────

def run_cycle(config: dict):
    global _last_mode, _warned_budget, _warned_session, _warned_sites
    global _apps_blocked_today

    username  = config.get("target_user", "adam")
    interval  = config.get("check_interval", 60)
    lock_msg  = config.get("lock_message", "Computer time is over.")

    mode = current_mode(config, username)
    daily_limit = _get_daily_limit(config)
    free_limit  = _get_free_limit(config)

    # Hard daily session cap overrides everything (except already locked)
    if mode != "locked" and tracker.is_limit_reached(username, daily_limit):
        mode = "locked"
        lock_msg = f"Daily limit ({daily_limit} min) reached. Come find me."

    user_active = tracker.is_user_active(username)

    # ── Warnings ──

    if mode != "locked" and user_active:
        # Session cap warnings
        sess_rem = tracker.get_remaining_minutes(username, daily_limit)
        _warned_session = _warn_at(
            sess_rem, {5, 1}, _warned_session, enforcer.send_warning, username
        )

        # Free budget warnings (budget mode only)
        if config.get("mode_strategy") == "budget" and mode == "free":
            bud_rem = tracker.get_free_remaining_minutes(username, free_limit)
            _warned_budget = _warn_at(
                bud_rem, {60, 30, 10, 5, 1}, _warned_budget,
                enforcer.send_budget_warning, username
            )

        # Per-site warnings
        for conf in config.get("site_limits", []):
            if mode not in conf.get("applies_in", ["free"]):
                continue
            site = conf["site"]
            rem  = tracker.get_site_remaining_minutes(site, conf.get("minutes", 0))
            ws   = _warned_sites.get(site, set())
            _warned_sites[site] = _warn_at(
                rem, {5, 1}, ws, enforcer.send_site_warning, username, site
            )

    # Reset warnings when coming out of locked
    if mode != "locked" and _last_mode == "locked":
        _warned_budget  = set()
        _warned_session = set()
        _warned_sites   = {}
        _warned_apps    = {}

    # Reset per-day blocked-app list at midnight (first cycle of new day)
    if not hasattr(run_cycle, "_last_date") or run_cycle._last_date != str(__import__("datetime").date.today()):
        _apps_blocked_today = set()
        run_cycle._last_date = str(__import__("datetime").date.today())

    # ── Enforce state ──

    enforcer.block_proxy_bypass()   # keep IPv6/QUIC holes closed every cycle

    grant_active = grant_mod.active_grant()
    source = f"GRANT until {grant_active['until']}" if grant_active else config.get("mode_strategy", "schedule").upper()
    log.debug("mode=%s (%s) last=%s", mode, source, _last_mode)

    if mode == "locked":
        write_proxy_rules(build_proxy_rules(config, "locked", username))
        if _last_mode != "locked":
            log.info("→ LOCKED (%s)", source)
            enforcer.clear_hosts_rules()
            # First locked cycle: warn, give a short grace, then log him out.
            enforcer.enforce_logout(username, lock_msg, grace_seconds=10)
        else:
            # Keep enforcing each cycle so a re-login gets killed too.
            enforcer.enforce_logout(username)

    else:
        rules = build_proxy_rules(config, mode, username)
        write_proxy_rules(rules)

        if _last_mode == "locked":
            log.info("→ %s (%s)", mode.upper(), source)
            enforcer.restore_login()   # bring lightdm back at on-hours
        elif _last_mode != mode:
            log.info("→ %s (%s)", mode.upper(), source)

        enforcer.apply_hosts_blocklist(rules["block_domains"])
        _enforce_apps(config, mode, username, interval)

    # ── Track time + activity state ──

    counting  = False
    trigger   = ""
    kind      = ""

    if mode != "locked" and user_active:
        tracker.add_session_time(username, interval)
        if mode == "free":
            counting, trigger, kind = _free_activity_detected(config, interval)
            if counting:
                tracker.add_free_time(username, interval)
                log.debug("Free activity detected (%s: %s) — budget ticked", kind, trigger)

    # Write live activity state for status page and notifications
    free_limit = _get_free_limit(config)
    _write_activity_state(mode, counting, trigger, kind, username, free_limit)

    # Notify Adam when counting state changes
    _notify_activity_change(config, username, counting, trigger, kind, free_limit)

    _last_mode = mode


# ── Activity state file ───────────────────────────────────────────────────────

ACTIVITY_STATE = "/var/lib/adam-control/activity_state.json"

def _write_activity_state(mode: str, counting: bool, trigger: str,
                          kind: str, username: str, free_limit: int):
    from datetime import datetime as _dt
    free_rem = tracker.get_free_remaining_minutes(username, free_limit)
    free_used = tracker.get_free_used_seconds(username) // 60
    state = {
        "mode":                 mode,
        "counting":             counting,
        "trigger":              trigger,
        "trigger_type":         kind,       # "site" | "app" | ""
        "free_remaining_minutes": free_rem,
        "free_used_minutes":    free_used,
        "free_limit_minutes":   free_limit,
        "updated":              _dt.now().isoformat(timespec="seconds"),
    }
    try:
        Path(ACTIVITY_STATE).write_text(json.dumps(state, indent=2))
    except Exception as e:
        log.warning("Could not write activity state: %s", e)


# ── Activity-change notifications ─────────────────────────────────────────────

def _notify_activity_change(config: dict, username: str, counting: bool,
                            trigger: str, kind: str, free_limit: int):
    global _last_counting

    if _last_counting is None:
        _last_counting = counting
        return

    if counting == _last_counting:
        return   # no change

    _last_counting = counting
    free_rem = tracker.get_free_remaining_minutes(username, free_limit)

    if counting:
        if kind == "site":
            # Check whether this looks like a school site that needs whitelisting
            work_whitelist = set(
                list(config.get("blocking", {}).get("work", {}).get("allow_domains") or [])
                + _load_allowed_file("work")
            )
            if work_whitelist:
                enforcer.send_activity_counting(
                    username, trigger, kind, free_rem, is_new_site=True
                )
            else:
                enforcer.send_activity_counting(
                    username, trigger, kind, free_rem, is_new_site=False
                )
        else:
            enforcer.send_activity_counting(
                username, trigger, kind, free_rem, is_new_site=False
            )
    else:
        enforcer.send_activity_paused(username, free_rem)


def main():
    global _last_category, _last_trigger
    log.info("adam-control daemon starting")

    if os.geteuid() != 0:
        log.error("Must run as root")
        sys.exit(1)

    _last_category, _last_trigger = _load_last_category()
    log.info("Seeded activity category: %s", _last_category)

    enforcer.block_proxy_bypass()   # close IPv6/QUIC holes so nothing skips the proxy
    log.info("Closed proxy-bypass holes (IPv6 + QUIC)")

    import status_server
    port = status_server.start()
    if port:
        log.info("Status page: http://localhost:%d", port)
    else:
        log.warning("Status server could not start (port in use?)")

    def handle_signal(signum, frame):
        log.info("Signal %d — shutting down", signum)
        enforcer.clear_hosts_rules()
        write_proxy_rules({"mode": "locked", "block_domains": [],
                           "block_url_patterns": [], "allow_domains": [],
                           "site_limits_reached": []})
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT,  handle_signal)

    interval = 60
    while True:
        try:
            config   = load_config()
            interval = config.get("check_interval", 60)
            run_cycle(config)
        except FileNotFoundError:
            log.error("Config not found at %s", CONFIG_PATH)
        except yaml.YAMLError as e:
            log.error("Config parse error: %s", e)
        except Exception as e:
            log.exception("Unexpected error: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
