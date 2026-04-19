#!/bin/bash
# Deploy AF-TimeAgent to house-mars VPS from a dev machine.
# Docker build/run logic lives in scripts/deploy_local.sh (also used by the
# VPS-side cron poller at /root/af-deploy/poll.sh).
#
# Usage: bash deploy.sh

set -e
cd "$(dirname "$0")"

ARCHIVE="/tmp/timeagent.tar.gz"

echo "[1/3] Packaging..."
tar czf "$ARCHIVE" \
  --exclude='__pycache__' --exclude='.env' \
  --exclude='.git' --exclude='.pytest_cache' --exclude='results' .

echo "[2/3] Uploading to VPS..."
scp -q "$ARCHIVE" house-mars:/root/repos/AF-TimeAgent/

echo "[3/3] Building and deploying on VPS..."
ssh house-mars "cd /root/repos/AF-TimeAgent \
  && tar xzf $(basename "$ARCHIVE") && rm $(basename "$ARCHIVE") \
  && bash scripts/deploy_local.sh"

echo "Done. Verify:"
echo "  ssh house-mars 'docker ps --filter name=timeagent'"
echo "  ssh house-mars 'curl -s http://localhost:8004/health | python3 -m json.tool'"
