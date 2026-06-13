"""
Enforcement actions: session lock, app kills, /etc/hosts, and proxy management.
All functions require root privileges.
"""

import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import List

log = logging.getLogger("enforcer")

HOSTS_FILE          = "/etc/hosts"
HOSTS_MARKER_START  = "# --- adam-control start ---"
HOSTS_MARKER_END    = "# --- adam-control end ---"

PROXY_PORT          = 8080
PROXY_ADDON         = "/opt/adam-control/proxy_addon.py"
PROXY_PID_FILE      = "/var/run/adam-control-proxy.pid"
IPTABLES_CHAIN      = "ADAM_PROXY"

DISPLAY_MANAGER     = "lightdm"   # Pi OS autologins adam via this


def _run(cmd: list):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception as e:
        log.warning("cmd failed %s: %s", cmd, e)
        return None


# ── Off-hours logout (hard) ───────────────────────────────────────────────────

def enforce_logout(username: str, message: str = "", grace_seconds: int = 0):
    """Actually end the user's GUI session so games/apps stop.

    A plain screen lock is a no-op here (no locker is wired to the lock
    signal, and the machine autologins via lightdm). So we stop the display
    manager — otherwise autologin instantly respawns the session — then
    terminate the user's logind sessions and kill any leftover processes.
    Reversed at on-hours by restore_login().
    """
    if message:
        _notify(username, "Computer Off for the Night", message, urgency="critical")
    if grace_seconds:
        time.sleep(grace_seconds)
    _run(["systemctl", "stop", DISPLAY_MANAGER])   # prevents autologin respawn
    _run(["loginctl", "terminate-user", username])
    _run(["pkill", "-KILL", "-u", username])       # backstop for stragglers
    log.info("Enforced logout for %s", username)


def restore_login():
    """Bring the login screen back when on-hours resume."""
    _run(["systemctl", "start", DISPLAY_MANAGER])
    log.info("Restored login (started %s)", DISPLAY_MANAGER)


# ── Session lock / unlock (legacy, kept for reference) ────────────────────────

def lock_session(username: str, message: str = ""):
    try:
        if message:
            _notify(username, "Computer Locked", message, urgency="critical")
        subprocess.run(["loginctl", "lock-session"], timeout=10, capture_output=True)
        log.info("Locked session for %s", username)
    except Exception as e:
        log.warning("lock_session failed: %s", e)


def unlock_session(username: str):
    try:
        subprocess.run(["loginctl", "unlock-session"], timeout=10, capture_output=True)
        log.info("Unlocked session for %s", username)
    except Exception as e:
        log.warning("unlock_session failed: %s", e)


def send_warning(username: str, minutes_remaining: int):
    msg = f"You have {minutes_remaining} minute(s) of computer time left today."
    _notify(username, "⏰ Time Warning", msg, urgency="normal")
    log.info("Sent %d-min warning to %s", minutes_remaining, username)


def send_site_warning(username: str, site: str, minutes_remaining: int):
    msg = f"You have {minutes_remaining} minute(s) left on {site} today."
    _notify(username, f"⏰ {site}", msg, urgency="normal")


def send_app_warning(username: str, app: str, minutes_remaining: int):
    msg = f"You have {minutes_remaining} minute(s) left for {app} today."
    _notify(username, f"⏰ {app}", msg, urgency="normal")


def send_budget_warning(username: str, minutes_remaining: int):
    msg = f"You have {minutes_remaining} minute(s) of free time left today."
    _notify(username, "⏰ Free Time Running Out", msg, urgency="normal")


def send_activity_counting(username: str, trigger: str, kind: str,
                           free_remaining: int, is_new_site: bool = False):
    if kind == "vpn":
        title = "⏱️ Free time counting — VPN detected"
        msg   = (f"A VPN ({trigger}) is active. Free time counts while a VPN is on. "
                 f"{free_remaining} min remaining.")
    elif kind == "site" and is_new_site:
        title = "⏱️ Free time counting — new site"
        msg   = (f'"{trigger}" is not on the school whitelist, so free time is counting. '
                 f"{free_remaining} min remaining. "
                 f"If this is a school site, ask Dad to add it.")
    elif kind == "site":
        title = "⏱️ Free time counting"
        msg   = f"{trigger} detected. {free_remaining} min remaining."
    else:
        title = "⏱️ Free time counting"
        msg   = f"{trigger} is running. {free_remaining} min remaining."
    _notify(username, title, msg, urgency="normal")


def send_activity_paused(username: str, free_remaining: int):
    msg = f"No free-time activity detected — timer paused. {free_remaining} min remaining."
    _notify(username, "✅ Free time paused", msg, urgency="low")


# ── VPN detection ─────────────────────────────────────────────────────────────

# Interface name prefixes that indicate an active VPN tunnel
_VPN_IFACES = ("tun", "wg", "ppp", "tap", "nordlynx", "proton", "utun", "ipsec")

# Process names associated with VPN clients
_VPN_PROCS = (
    "openvpn", "wireguard", "wg-quick",
    "nordvpn", "expressvpn", "protonvpn", "mullvad",
    "windscribe", "surfshark", "ipvanish", "cyberghost",
    "privateinternetaccess", "pia", "tunnelbear", "hotspot",
)


def vpn_active() -> tuple:
    """
    Return (detected: bool, iface_or_proc: str).
    Checks for active VPN network interfaces first (most reliable),
    then falls back to process name scanning.
    Note: browser-extension VPNs (SOCKS-proxy type) are not detected here.
    """
    # Network interface check — VPN tunnel must be UP to count
    try:
        result = subprocess.run(
            ["ip", "link", "show"], capture_output=True, text=True, timeout=5
        )
        current_iface = ""
        for line in result.stdout.splitlines():
            # Lines like "5: tun0: <...FLAGS...>"
            if line and line[0].isdigit():
                current_iface = line.split(":")[1].strip().split("@")[0]
            if any(current_iface.startswith(p) for p in _VPN_IFACES):
                if "UP" in line and "LOWER_UP" in line:
                    return True, current_iface
    except Exception:
        pass

    # Process name fallback
    try:
        result = subprocess.run(
            ["ps", "-eo", "comm"], capture_output=True, text=True, timeout=5
        )
        running = result.stdout.lower()
        for proc in _VPN_PROCS:
            if proc in running:
                return True, proc
    except Exception:
        pass

    return False, ""


def _notify(username: str, title: str, body: str, urgency: str = "normal"):
    uid = _get_uid(username)
    env = {
        **os.environ,
        "DISPLAY": _get_display(username),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
    }
    try:
        subprocess.run(
            ["sudo", "-u", username, "notify-send",
             "-u", urgency, "-t", "10000", title, body],
            env=env, timeout=5, capture_output=True,
        )
    except Exception:
        pass


# ── App blocking and tracking ─────────────────────────────────────────────────

def get_running_apps(tracked_apps: List[str]) -> List[str]:
    """Return which apps from tracked_apps are currently running (by name match)."""
    running = []
    try:
        result = subprocess.run(
            ["ps", "-eo", "comm,args"],
            capture_output=True, text=True
        )
        proc_text = result.stdout.lower()
        for app in tracked_apps:
            if app.lower() in proc_text:
                running.append(app)
    except Exception as e:
        log.warning("get_running_apps failed: %s", e)
    return running


def kill_blocked_apps(blocked_apps: List[str]):
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,comm,args"],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines()[1:]:
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            pid, comm = parts[0], parts[1]
            args = parts[2] if len(parts) > 2 else ""
            for blocked in blocked_apps:
                if blocked.lower() in comm.lower() or blocked.lower() in args.lower():
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                        log.info("Killed blocked app: %s (pid %s)", comm, pid)
                    except ProcessLookupError:
                        pass
    except Exception as e:
        log.warning("kill_blocked_apps failed: %s", e)


# ── /etc/hosts (fast domain blocking, no proxy needed) ───────────────────────

def apply_hosts_blocklist(domains: List[str]):
    _write_hosts_block(domains)


def clear_hosts_rules():
    _write_hosts_block([])
    log.info("Cleared hosts rules")


def _write_hosts_block(domains: List[str]):
    current = Path(HOSTS_FILE).read_text()
    lines = current.splitlines()

    # Strip old block
    new_lines, inside = [], False
    for line in lines:
        if line.strip() == HOSTS_MARKER_START:
            inside = True
        elif line.strip() == HOSTS_MARKER_END:
            inside = False
        elif not inside:
            new_lines.append(line)

    if domains:
        new_lines.append(HOSTS_MARKER_START)
        seen = set()
        for domain in domains:
            domain = domain.strip().lstrip("www.")
            if domain in seen:
                continue
            seen.add(domain)
            new_lines.append(f"127.0.0.1 {domain}")
            new_lines.append(f"127.0.0.1 www.{domain}")
        new_lines.append(HOSTS_MARKER_END)

    Path(HOSTS_FILE).write_text("\n".join(new_lines) + "\n")
    _flush_dns()


def _flush_dns():
    for cmd in [
        ["systemd-resolve", "--flush-caches"],
        ["resolvectl",      "flush-caches"],
        ["service",         "nscd", "restart"],
    ]:
        try:
            subprocess.run(cmd, capture_output=True, timeout=5)
            return
        except Exception:
            continue


# ── Transparent proxy (mitmproxy) ─────────────────────────────────────────────

def start_proxy():
    """Start mitmproxy in transparent mode with iptables redirect."""
    if _proxy_running():
        return
    try:
        proc = subprocess.Popen(
            [
                "mitmdump",
                "--mode",       "transparent",
                "--listen-port", str(PROXY_PORT),
                "--set",        "block_global=false",
                "--ssl-insecure",  # needed for some sites; CA cert handles trust
                "-s",           PROXY_ADDON,
                "-q",           # quiet — our addon logs to its own file
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        Path(PROXY_PID_FILE).write_text(str(proc.pid))
        _setup_iptables()
        log.info("Proxy started (pid %d)", proc.pid)
    except FileNotFoundError:
        log.error("mitmdump not found — install with: pip3 install mitmproxy")
    except Exception as e:
        log.error("Failed to start proxy: %s", e)


def stop_proxy():
    """Stop mitmproxy and remove iptables rules."""
    _teardown_iptables()
    pid_file = Path(PROXY_PID_FILE)
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            pid_file.unlink(missing_ok=True)
            log.info("Proxy stopped")
        except Exception as e:
            log.warning("stop_proxy: %s", e)


def _proxy_running() -> bool:
    pid_file = Path(PROXY_PID_FILE)
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)  # signal 0 = existence check
        return True
    except (ProcessLookupError, ValueError):
        pid_file.unlink(missing_ok=True)
        return False


def _setup_iptables():
    """Redirect non-root HTTP/HTTPS traffic through the local proxy."""
    cmds = [
        ["iptables", "-t", "nat", "-N", IPTABLES_CHAIN],
        # Redirect HTTP and HTTPS for all users except root (to avoid loop)
        ["iptables", "-t", "nat", "-A", IPTABLES_CHAIN,
         "-p", "tcp", "--dport", "80",
         "-m", "owner", "!", "--uid-owner", "0",
         "-j", "REDIRECT", "--to-port", str(PROXY_PORT)],
        ["iptables", "-t", "nat", "-A", IPTABLES_CHAIN,
         "-p", "tcp", "--dport", "443",
         "-m", "owner", "!", "--uid-owner", "0",
         "-j", "REDIRECT", "--to-port", str(PROXY_PORT)],
        ["iptables", "-t", "nat", "-A", "OUTPUT", "-j", IPTABLES_CHAIN],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, capture_output=True, timeout=5)
        except Exception as e:
            log.warning("iptables setup: %s", e)


def _teardown_iptables():
    """Remove proxy iptables rules."""
    cmds = [
        ["iptables", "-t", "nat", "-D", "OUTPUT", "-j", IPTABLES_CHAIN],
        ["iptables", "-t", "nat", "-F", IPTABLES_CHAIN],
        ["iptables", "-t", "nat", "-X", IPTABLES_CHAIN],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, capture_output=True, timeout=5)
        except Exception:
            pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_uid(username: str) -> int:
    import pwd
    return pwd.getpwnam(username).pw_uid


def _get_display(username: str) -> str:
    try:
        result = subprocess.run(
            ["sudo", "-u", username, "bash", "-c",
             "echo ${DISPLAY:-:0}"],
            capture_output=True, text=True, timeout=5,
        )
        d = result.stdout.strip()
        return d if d else ":0"
    except Exception:
        return ":0"
