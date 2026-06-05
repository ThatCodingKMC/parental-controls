"""
Local status web server for adam-control.
Runs as a background thread inside the daemon.
Adam bookmarks http://localhost:8765 to check his status.

Shows: current mode, free budget, what's triggering the timer,
per-site limits, per-app limits, and a plain-English explanation
of what to do if something looks wrong.
"""

import http.server
import json
import threading
from datetime import datetime
from pathlib import Path

PORT = 8765

USAGE_FILE    = "/var/lib/adam-control/usage.json"
SITE_FILE     = "/var/lib/adam-control/site_usage.json"
PROXY_FILE    = "/var/lib/adam-control/proxy_rules.json"
ACTIVITY_FILE = "/var/lib/adam-control/activity_state.json"
CONFIG_FILE   = "/etc/adam-control/schedule.yaml"


def _read(path: str) -> dict:
    try:
        p = Path(path)
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _pct(used, limit):
    if not limit:
        return 0
    return min(100, int(used / limit * 100))


def _bar_color(pct):
    if pct < 60:
        return "#4caf50"
    if pct < 85:
        return "#ff9800"
    return "#f44336"


def _build_page() -> str:
    activity = _read(ACTIVITY_FILE)
    proxy    = _read(PROXY_FILE)

    mode        = activity.get("mode", proxy.get("mode", "?"))
    counting    = activity.get("counting", False)
    trigger     = activity.get("trigger", "")
    kind        = activity.get("trigger_type", "")
    free_rem    = activity.get("free_remaining_minutes", 0)
    free_used   = activity.get("free_used_minutes", 0)
    free_limit  = activity.get("free_limit_minutes", 0)
    updated     = activity.get("updated", "")

    # Load config for site/app limits (best-effort)
    site_limits = []
    app_limits  = []
    username    = "adam"
    try:
        import yaml
        cfg = yaml.safe_load(open(CONFIG_FILE))
        site_limits = cfg.get("site_limits", [])
        app_limits  = cfg.get("app_limits", [])
        username    = cfg.get("target_user", "adam")
    except Exception:
        pass

    # Load per-site usage from site_usage.json
    from datetime import date
    today = str(date.today())
    site_data = _read(SITE_FILE)

    def site_mins(domain):
        key = f"{domain}:{today}"
        ts = site_data.get(key, [])
        return len(set(int(t) // 60 for t in ts)) if ts else 0

    # Load per-app usage from usage.json
    usage_data = _read(USAGE_FILE)
    def app_mins(app):
        return usage_data.get(f"app:{app.lower()}:{today}", 0) // 60

    # ── Mode badge ──
    mode_color = {"free": "#4caf50", "work": "#2196f3",
                  "locked": "#f44336"}.get(mode, "#888")
    mode_label = {"free": "Free Time", "work": "Work Mode",
                  "locked": "Locked"}.get(mode, mode.upper())

    # ── Free budget bar ──
    budget_html = ""
    if free_limit > 0:
        pct = _pct(free_used, free_limit)
        color = _bar_color(pct)
        budget_html = f"""
        <div class="section">
          <div class="section-title">Free Time Budget</div>
          <div class="bar-wrap">
            <div class="bar" style="width:{pct}%;background:{color}"></div>
          </div>
          <div class="bar-label">{free_used} min used &nbsp;/&nbsp; {free_limit} min total &nbsp;·&nbsp; <strong>{free_rem} min remaining</strong></div>
        </div>"""

    # ── Timer status ──
    if mode == "locked":
        timer_html = '<div class="status-box locked">🔒 Computer is locked</div>'
    elif mode == "free" and counting:
        if kind == "vpn":
            detail = f'<div class="trigger">VPN active: <code>{trigger}</code></div>'
            detail += '<div class="hint">Free time counts while a VPN is on.</div>'
        elif kind == "site":
            detail = f'<div class="trigger">Site detected: <code>{trigger}</code></div>'
            if trigger:
                detail += '<div class="hint">If this is a school site you need, screenshot this and tell Dad to add it.</div>'
        else:
            detail = f'<div class="trigger">App running: <code>{trigger}</code></div>'
        timer_html = f'<div class="status-box counting">⏱️ Free time is counting{detail}</div>'
    elif mode == "free" and not counting:
        timer_html = '<div class="status-box paused">✅ Free time is <strong>paused</strong> — no free-time activity detected</div>'
    else:  # work mode
        timer_html = '<div class="status-box work">📚 Work mode — free time is not counting</div>'

    # ── Site limits table ──
    site_rows = ""
    for conf in site_limits:
        site  = conf["site"]
        limit = conf.get("minutes", 0)
        used  = site_mins(site)
        pct   = _pct(used, limit)
        color = _bar_color(pct)
        rem   = max(0, limit - used)
        status = "🚫 Blocked" if used >= limit else f"{rem} min left"
        site_rows += f"""
          <tr>
            <td>{site}</td>
            <td>
              <div class="bar-wrap sm">
                <div class="bar" style="width:{pct}%;background:{color}"></div>
              </div>
            </td>
            <td>{used}m / {limit}m</td>
            <td>{status}</td>
          </tr>"""

    # ── App limits table ──
    app_rows = ""
    for conf in app_limits:
        app   = conf["app"]
        limit = conf.get("minutes", 0)
        used  = app_mins(app)
        pct   = _pct(used, limit)
        color = _bar_color(pct)
        rem   = max(0, limit - used)
        status = "🚫 Blocked" if used >= limit else f"{rem} min left"
        app_rows += f"""
          <tr>
            <td>{app}</td>
            <td>
              <div class="bar-wrap sm">
                <div class="bar" style="width:{pct}%;background:{color}"></div>
              </div>
            </td>
            <td>{used}m / {limit}m</td>
            <td>{status}</td>
          </tr>"""

    tables = ""
    if site_rows:
        tables += f"""
        <div class="section">
          <div class="section-title">Site Limits</div>
          <table><tr><th>Site</th><th>Usage</th><th>Time</th><th>Status</th></tr>
          {site_rows}</table>
        </div>"""
    if app_rows:
        tables += f"""
        <div class="section">
          <div class="section-title">App Limits</div>
          <table><tr><th>App</th><th>Usage</th><th>Time</th><th>Status</th></tr>
          {app_rows}</table>
        </div>"""

    # ── Ranked free-time usage ──
    # Load work whitelist — anything not in here is "free time" activity
    work_whitelist = set()
    try:
        import yaml
        cfg2 = yaml.safe_load(open(CONFIG_FILE))
        for d in cfg2.get("blocking", {}).get("work", {}).get("allow_domains") or []:
            work_whitelist.add(d)
        allowed_path = Path("/etc/adam-control/lists/work/allowed.txt")
        if allowed_path.exists():
            for line in allowed_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    work_whitelist.add(line)
    except Exception:
        pass

    # Keywords that suggest a site might be school-related but isn't whitelisted
    _EDU_HINTS = {
        "edu", "school", "learn", "academ", "univers", "college",
        "course", "class", "grade", "tutor", "study", "homework",
        "flvs", "efsc", "instructure", "canvas", "blackboard",
        "brightspace", "moodle",
    }

    def _looks_educational(domain: str) -> bool:
        d = domain.lower()
        if d.endswith(".edu"):
            return True
        return any(hint in d for hint in _EDU_HINTS)

    def _in_whitelist(domain: str) -> bool:
        domain = domain.lower().lstrip("www.")
        for w in work_whitelist:
            w = w.lower().lstrip("www.")
            if domain == w or domain.endswith("." + w):
                return True
        return False

    # Compute minutes per domain from site_usage.json (all visits today)
    ranked = []
    for key, timestamps in site_data.items():
        if not key.endswith(f":{today}"):
            continue
        domain = key.rsplit(":", 1)[0]
        mins = len(set(int(t) // 60 for t in timestamps)) if timestamps else 0
        if mins == 0:
            continue
        whitelisted = _in_whitelist(domain)
        edu_flag    = not whitelisted and _looks_educational(domain)
        ranked.append((mins, domain, whitelisted, edu_flag))

    ranked.sort(key=lambda x: -x[0])

    ranked_rows = ""
    for mins, domain, whitelisted, edu_flag in ranked:
        if whitelisted:
            tag = '<span class="tag school">School ✓</span>'
        elif edu_flag:
            tag = '<span class="tag warn">⚠️ School? Tell Dad</span>'
        else:
            tag = '<span class="tag free">Free time</span>'
        ranked_rows += f"<tr><td><code>{domain}</code></td><td>{mins}m</td><td>{tag}</td></tr>"

    if ranked_rows:
        tables += f"""
        <div class="section">
          <div class="section-title">Today's usage — ranked by time</div>
          <p class="ranked-note">Everything in "Free time" counted against your budget.
          If a school site shows "⚠️ School?" — screenshot this and tell Dad.</p>
          <table><tr><th>Domain</th><th>Time</th><th>Type</th></tr>
          {ranked_rows}</table>
        </div>"""

    updated_str = ""
    if updated:
        try:
            dt = datetime.fromisoformat(updated)
            updated_str = dt.strftime("%-I:%M:%S %p")
        except Exception:
            updated_str = updated

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Computer Status</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
          background:#0f0f1a; color:#e0e0e0; padding:24px; }}
  h1 {{ font-size:22px; margin-bottom:4px }}
  .sub {{ color:#666; font-size:13px; margin-bottom:20px }}
  .badge {{ display:inline-block; padding:6px 16px; border-radius:20px;
            font-weight:700; font-size:15px; color:#fff;
            background:{mode_color}; margin-bottom:20px }}
  .section {{ background:#1a1a2e; border:1px solid #2a2a4a;
              border-radius:12px; padding:18px; margin-bottom:16px }}
  .section-title {{ font-size:12px; text-transform:uppercase; letter-spacing:.08em;
                    color:#666; margin-bottom:10px }}
  .bar-wrap {{ background:#2a2a4a; border-radius:4px; height:10px;
               overflow:hidden; margin:6px 0 }}
  .bar-wrap.sm {{ height:7px; }}
  .bar {{ height:100%; border-radius:4px; transition:width .3s }}
  .bar-label {{ font-size:13px; color:#aaa; margin-top:4px }}
  .status-box {{ border-radius:10px; padding:14px 16px; font-size:15px; line-height:1.5 }}
  .status-box.counting {{ background:#2d1f00; border:1px solid #ff9800 }}
  .status-box.paused   {{ background:#0d2010; border:1px solid #4caf50 }}
  .status-box.work     {{ background:#0d1a2d; border:1px solid #2196f3 }}
  .status-box.locked   {{ background:#2d0d0d; border:1px solid #f44336 }}
  .trigger {{ margin-top:8px; font-size:14px; color:#ffb74d }}
  .hint {{ margin-top:6px; font-size:13px; color:#888;
           border-top:1px solid #444; padding-top:6px }}
  code {{ background:#111; padding:2px 6px; border-radius:4px;
          font-family:monospace; font-size:13px; color:#e0e0e0 }}
  table {{ width:100%; border-collapse:collapse; font-size:13px }}
  th {{ text-align:left; color:#555; font-weight:600; padding:4px 6px 8px }}
  td {{ padding:5px 6px; border-top:1px solid #1e1e3a; vertical-align:middle }}
  .refresh {{ font-size:12px; color:#444; margin-top:20px; text-align:center }}
  a {{ color:#4caf50 }}
  .tag {{ display:inline-block; padding:2px 8px; border-radius:10px;
          font-size:11px; font-weight:600 }}
  .tag.school {{ background:#0d2010; color:#4caf50; border:1px solid #2d5a3d }}
  .tag.free   {{ background:#1a1a00; color:#aaa; border:1px solid #333 }}
  .tag.warn   {{ background:#2d1f00; color:#ffb74d; border:1px solid #664400 }}
  .ranked-note {{ font-size:12px; color:#555; margin-bottom:10px; line-height:1.5 }}
</style>
</head>
<body>
<h1>Computer Status</h1>
<div class="sub">Updates every 30 seconds · Last checked {updated_str}</div>
<div class="badge">{mode_label}</div>

<div class="section">{timer_html}</div>

{budget_html}
{tables}

<div class="refresh">
  Auto-refreshes every 30 sec &nbsp;·&nbsp;
  <a href="/">Refresh now</a>
</div>
</body>
</html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            content = _build_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception:
            self.send_error(500)

    def log_message(self, *args):
        pass  # suppress access log noise


def start():
    """Start the status HTTP server in a background daemon thread."""
    try:
        server = http.server.HTTPServer(("127.0.0.1", PORT), _Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True, name="status-server")
        t.start()
        return PORT
    except OSError as e:
        return None  # port already in use — not fatal
