#!/usr/bin/env bash
# Push changes to Adam's computer via git pull + restart.
# Usage:
#   ./deploy.sh           — deploy to default target
#   ./deploy.sh adams-pc  — deploy to a specific host

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/config/schedule.yaml"

DEFAULT_TARGET="adams-pc"
TARGET="${1:-$DEFAULT_TARGET}"

echo "=== adam-control deploy → $TARGET ==="

# Validate YAML locally before pushing
python3 -c "
import yaml, sys
try:
    yaml.safe_load(open('$CONFIG'))
    print('-> Config YAML: OK')
except Exception as e:
    print('ERROR: invalid YAML —', e)
    sys.exit(1)
"

# Push to GitHub first
echo "-> Pushing to GitHub..."
git push

# Pull on Adam's machine and restart
echo "-> Pulling on Adam's machine..."
ssh "$TARGET" "
    cd ~/projectsavingprivateadam && \
    git pull && \
    sudo cp agent/*.py /opt/adam-control/ && \
    sudo cp -r config/lists/. /etc/adam-control/lists/ && \
    sudo cp config/schedule.yaml /etc/adam-control/schedule.yaml && \
    sudo systemctl restart adam-control-proxy && \
    sleep 1 && \
    sudo systemctl restart adam-control && \
    echo 'Done.'
"

echo ""
echo "=== Deploy complete ==="
