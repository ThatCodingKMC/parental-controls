"""
mitmproxy addon for adam-control.

The daemon writes /var/lib/adam-control/proxy_rules.json every cycle.
This addon reads that file every RELOAD_INTERVAL seconds and enforces:
  - Domain-level blocking
  - URL path-level blocking (regex patterns)
  - Whitelist (allow_domains) mode
  - Per-site time-limit enforcement

Run via enforcer.py (transparent proxy mode with iptables redirect).
"""

import json
import re
import time
from datetime import date
from pathlib import Path

from mitmproxy import http
from mitmproxy.net.check import is_valid_host

RULES_FILE  = "/var/lib/adam-control/proxy_rules.json"
USAGE_FILE  = "/var/lib/adam-control/site_usage.json"
RELOAD_SECS = 5   # re-read rules file at most every N seconds

BLOCK_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Blocked</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #0f0f1a; color: #e0e0e0;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh;
  }}
  .card {{
    background: #1a1a2e; border: 1px solid #2a2a4a;
    border-radius: 16px; padding: 48px; max-width: 480px; text-align: center;
  }}
  .icon {{ font-size: 64px; margin-bottom: 16px; }}
  h1 {{ font-size: 28px; color: #e94560; margin-bottom: 12px; }}
  .reason {{
    display: inline-block; background: #12122a;
    border-radius: 8px; padding: 8px 18px;
    color: #e94560; font-family: monospace; font-size: 14px;
    margin: 16px 0;
  }}
  p {{ color: #888; line-height: 1.6; }}
</style>
</head>
<body>
<div class="card">
  <div class="icon">🔒</div>
  <h1>Site Blocked</h1>
  <div class="reason">{reason}</div>
  <p>{message}</p>
</div>
</body>
</html>
"""


class AdamControl:
    def __init__(self):
        self._rules: dict = {}
        self._rules_mtime: float = 0.0
        self._last_reload: float = 0.0

    # ── Rule loading ─────────────────────────────────────────────────────────

    def _maybe_reload(self):
        now = time.monotonic()
        if now - self._last_reload < RELOAD_SECS:
            return
        self._last_reload = now
        try:
            p = Path(RULES_FILE)
            mtime = p.stat().st_mtime
            if mtime != self._rules_mtime:
                self._rules = json.loads(p.read_text())
                self._rules_mtime = mtime
        except Exception:
            pass

    # ── Domain helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _root_domain(host: str) -> str:
        host = host.lower().split(":")[0]  # strip port
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    @staticmethod
    def _domain_matches(host: str, pattern: str) -> bool:
        host = host.lower().split(":")[0]
        pattern = pattern.lower().lstrip("www.")
        root = AdamControl._root_domain(host)
        return root == pattern or host == pattern or host.endswith("." + pattern)

    # ── Block decision ───────────────────────────────────────────────────────

    def _check(self, host: str, url: str) -> tuple[bool, str, str]:
        """Return (blocked, reason, detail_message)."""
        r = self._rules
        if not r:
            return False, "", ""

        mode = r.get("mode", "")

        # Everything blocked when locked
        if mode == "locked":
            return True, "Computer is locked", "Come find Dad if you need access."

        # Whitelist: if allow_domains is set, block anything not in it
        allow = r.get("allow_domains", [])
        if allow:
            if not any(self._domain_matches(host, a) for a in allow):
                return True, "Not in allowed list", "Only school sites are allowed right now."

        # Always-blocked domains (same in both modes)
        for d in r.get("block_domains", []):
            if self._domain_matches(host, d):
                return True, d, "This site is blocked right now."

        # URL patterns (always-on + work-specific, merged by daemon)
        for pattern in r.get("block_url_patterns", []):
            try:
                if re.search(pattern, url, re.IGNORECASE):
                    return True, "URL pattern", "This part of the site is blocked."
            except re.error:
                pass

        # Per-site time limits reached
        root = self._root_domain(host)
        for site in r.get("site_limits_reached", []):
            if self._domain_matches(host, site):
                return (
                    True,
                    f"{root} — time limit",
                    f"You've used your daily allowance for {root}. Come back tomorrow.",
                )

        return False, "", ""

    # ── Site usage recording ─────────────────────────────────────────────────

    def _record_visit(self, host: str):
        root = self._root_domain(host)
        today = str(date.today())
        key = f"{root}:{today}"
        now = time.time()
        try:
            p = Path(USAGE_FILE)
            data: dict = json.loads(p.read_text()) if p.exists() else {}

            # Store the current minute-bucket number rather than raw timestamps.
            # Max 1440 entries per domain per day regardless of request volume.
            minute_bucket = int(now // 60)
            buckets: list = data.get(key, [])
            if minute_bucket not in buckets:
                buckets.append(minute_bucket)
            data[key] = buckets

            # Purge old days
            today_ord = date.today().toordinal()
            data = {
                k: v for k, v in data.items()
                if _key_ordinal(k) >= today_ord - 30
            }

            p.write_text(json.dumps(data))
        except Exception:
            pass

    # ── Safe search enforcement ──────────────────────────────────────────────

    @staticmethod
    def _enforce_safe_search(flow: http.HTTPFlow):
        """Force safe search on major search engines by injecting query params."""
        host = flow.request.pretty_host.lower()
        path = flow.request.path.split("?")[0]

        # Google — safe=active
        if re.search(r"(^|\.)google\.", host) and path in ("/search", "/"):
            if flow.request.query.get("safe") != "active":
                flow.request.query["safe"] = "active"

        # Bing — adlt=strict
        elif re.search(r"(^|\.)bing\.com$", host) and path.startswith("/search"):
            if flow.request.query.get("adlt") != "strict":
                flow.request.query["adlt"] = "strict"

        # DuckDuckGo — kp=1 (strict safe search)
        elif re.search(r"(^|\.)duckduckgo\.com$", host):
            if flow.request.query.get("kp") != "1":
                flow.request.query["kp"] = "1"

        # Yahoo Search — vm=r (strict)
        elif re.search(r"(^|\.)yahoo\.com$", host) and "/search" in path:
            if flow.request.query.get("vm") != "r":
                flow.request.query["vm"] = "r"

        # Brave Search — safesearch=strict
        elif re.search(r"(^|\.)search\.brave\.com$", host):
            if flow.request.query.get("safesearch") != "strict":
                flow.request.query["safesearch"] = "strict"

    # ── mitmproxy hook ───────────────────────────────────────────────────────

    def request(self, flow: http.HTTPFlow):
        self._maybe_reload()

        host = flow.request.pretty_host
        url  = flow.request.pretty_url

        blocked, reason, message = self._check(host, url)
        if blocked:
            flow.response = http.Response.make(
                403,
                BLOCK_HTML.format(reason=reason, message=message or "Come find Dad if you need access."),
                {"Content-Type": "text/html; charset=utf-8"},
            )
            return

        # Force safe search on search engines (modifies request before it leaves)
        self._enforce_safe_search(flow)

        # Record visit for per-site timer (only track main-frame requests)
        if flow.request.method in ("GET", "POST"):
            self._record_visit(host)


def _key_ordinal(key: str) -> int:
    try:
        return date.fromisoformat(key.split(":")[1]).toordinal()
    except Exception:
        return 0


addons = [AdamControl()]
