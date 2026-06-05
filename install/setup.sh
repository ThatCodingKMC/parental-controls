#!/usr/bin/env bash
# Run this script on Adam's computer (as root) to install the daemon + proxy.
# Usage:  sudo bash setup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TARGET_USER="${1:-adam}"   # pass username as first arg if different

echo "=== adam-control installer ==="
echo "Target user: $TARGET_USER"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Must run as root (sudo bash setup.sh)"
    exit 1
fi

# ── System packages ──────────────────────────────────────────────────────────
echo "-> Installing system packages..."
if command -v apt-get &>/dev/null; then
    apt-get install -y python3 python3-pip \
        libnotify-bin notify-osd \
        iptables \
        libnss3-tools     # for certutil (Firefox cert import)
elif command -v dnf &>/dev/null; then
    dnf install -y python3 python3-pip \
        libnotify nss-tools iptables
fi

echo "-> Installing mitmproxy..."
pip3 install --quiet --break-system-packages mitmproxy 2>/dev/null || \
    pip3 install --quiet mitmproxy

# ── Directories ──────────────────────────────────────────────────────────────
echo "-> Creating directories..."
mkdir -p /etc/adam-control/lists/always/blocked
mkdir -p /etc/adam-control/lists/work/blocked
mkdir -p /etc/adam-control/lists/free/blocked
mkdir -p /var/lib/adam-control
mkdir -p /opt/adam-control

# ── Agent files ───────────────────────────────────────────────────────────────
echo "-> Installing agent files..."
cp "$PROJECT_DIR/agent/daemon.py"      /opt/adam-control/
cp "$PROJECT_DIR/agent/enforcer.py"    /opt/adam-control/
cp "$PROJECT_DIR/agent/tracker.py"     /opt/adam-control/
cp "$PROJECT_DIR/agent/proxy_addon.py" /opt/adam-control/
cp "$PROJECT_DIR/agent/status.py"      /opt/adam-control/
cp "$PROJECT_DIR/agent/grant.py"         /opt/adam-control/
cp "$PROJECT_DIR/agent/status_server.py" /opt/adam-control/
chmod +x /opt/adam-control/grant.py
chmod +x /opt/adam-control/daemon.py

# ── Config ───────────────────────────────────────────────────────────────────
echo "-> Installing config..."
if [[ ! -f /etc/adam-control/schedule.yaml ]]; then
    cp "$PROJECT_DIR/config/schedule.yaml" /etc/adam-control/schedule.yaml
    sed -i "s/target_user: adam/target_user: $TARGET_USER/" /etc/adam-control/schedule.yaml
    echo "   Installed default config (target_user = $TARGET_USER)"
else
    echo "   Config already exists — skipping (run deploy.sh to update)"
fi

# ── List files ────────────────────────────────────────────────────────────────
echo "-> Syncing list files..."
cp -rn "$PROJECT_DIR/config/lists/." /etc/adam-control/lists/ 2>/dev/null || true

# ── mitmproxy CA certificate ──────────────────────────────────────────────────
echo "-> Setting up mitmproxy CA certificate..."

# Generate certs (stored in /root/.mitmproxy/)
mitmdump --ignore-hosts ".*" -p 8079 &
MITM_PID=$!
sleep 3
kill $MITM_PID 2>/dev/null || true
wait $MITM_PID 2>/dev/null || true

CA_CERT="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
if [[ ! -f "$CA_CERT" ]]; then
    echo "ERROR: mitmproxy CA cert not found at $CA_CERT"
    exit 1
fi

# Install into system trust store
cp "$CA_CERT" /usr/local/share/ca-certificates/adam-control-ca.crt
update-ca-certificates
echo "   System CA cert installed."

# Install into Firefox for the target user
USER_HOME="/home/$TARGET_USER"
FIREFOX_PROFILES=$(find "$USER_HOME/.mozilla/firefox" -name "cert9.db" -exec dirname {} \; 2>/dev/null || true)
if [[ -n "$FIREFOX_PROFILES" ]]; then
    while IFS= read -r profile_dir; do
        certutil -A -n "adam-control" -t "CT,," \
            -i "$CA_CERT" -d "sql:$profile_dir" 2>/dev/null && \
            echo "   Firefox cert installed: $profile_dir"
    done <<< "$FIREFOX_PROFILES"
else
    echo "   No Firefox profiles found (will use system trust store for Chrome/Chromium)."
fi

# Install into Chromium NSS store
CHROME_NSS="$USER_HOME/.pki/nssdb"
if [[ -d "$CHROME_NSS" ]]; then
    certutil -A -n "adam-control" -t "CT,," \
        -i "$CA_CERT" -d "sql:$CHROME_NSS" 2>/dev/null && \
        echo "   Chromium cert installed."
fi

# ── iptables persistence ──────────────────────────────────────────────────────
echo "-> Configuring iptables persistence..."
if command -v iptables-save &>/dev/null; then
    apt-get install -y iptables-persistent &>/dev/null 2>&1 || true
fi

# ── systemd services ──────────────────────────────────────────────────────────
echo "-> Installing systemd services..."

# Main daemon
cat > /etc/systemd/system/adam-control.service << EOF
[Unit]
Description=Adam Computer Control Daemon
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/adam-control/daemon.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Proxy service
cat > /etc/systemd/system/adam-control-proxy.service << EOF
[Unit]
Description=Adam Computer Control Proxy (mitmproxy)
After=network.target
Before=adam-control.service

[Service]
Type=simple
ExecStart=/usr/local/bin/mitmdump \
    --mode transparent \
    --listen-port 8080 \
    --set block_global=false \
    -s /opt/adam-control/proxy_addon.py \
    -q
ExecStartPost=/bin/sh -c ' \
    iptables -t nat -N ADAM_PROXY 2>/dev/null || true; \
    iptables -t nat -A ADAM_PROXY -p tcp --dport 80 -m owner ! --uid-owner 0 -j REDIRECT --to-port 8080; \
    iptables -t nat -A ADAM_PROXY -p tcp --dport 443 -m owner ! --uid-owner 0 -j REDIRECT --to-port 8080; \
    iptables -t nat -A OUTPUT -j ADAM_PROXY 2>/dev/null || true'
ExecStopPost=/bin/sh -c ' \
    iptables -t nat -D OUTPUT -j ADAM_PROXY 2>/dev/null || true; \
    iptables -t nat -F ADAM_PROXY 2>/dev/null || true; \
    iptables -t nat -X ADAM_PROXY 2>/dev/null || true'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable adam-control-proxy.service adam-control.service
systemctl restart adam-control-proxy.service
sleep 2
systemctl restart adam-control.service

echo ""
echo "=== Installation complete ==="
echo ""
systemctl status adam-control.service --no-pager -l || true
echo ""
echo "Config:      /etc/adam-control/schedule.yaml"
echo "Lists:       /etc/adam-control/lists/"
echo "Daemon log:  journalctl -u adam-control -f"
echo "Proxy log:   journalctl -u adam-control-proxy -f"
echo "Status:      sudo python3 /opt/adam-control/status.py"
