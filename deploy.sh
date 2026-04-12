#!/bin/bash
# Deploy AF-TimeAgent to house-mars VPS
# Usage: bash deploy.sh
set -e
cd "$(dirname "$0")"

echo "[1/3] Packaging..."
tar czf /tmp/timeagent.tar.gz \
  --exclude='__pycache__' --exclude='.env' \
  --exclude='.git' --exclude='.pytest_cache' --exclude='results' .

echo "[2/3] Uploading to VPS..."
scp -q /tmp/timeagent.tar.gz house-mars:/root/AF-TimeAgent/

echo "[3/3] Building and deploying on VPS..."
ssh house-mars "cd /root/AF-TimeAgent && tar xzf timeagent.tar.gz && rm timeagent.tar.gz && bash vps_deploy.sh"
