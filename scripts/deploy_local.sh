#!/bin/bash
# Deploy AF-TimeAgent from the current directory (run on the VPS).
# Called by deploy.sh (dev) via SSH or by /root/af-deploy/poll.sh (cron).

set -e
cd "$(dirname "$0")/.."

if [[ -f /root/af-deploy/secrets.env ]]; then
  set -a; source /root/af-deploy/secrets.env; set +a
fi

: "${GITHUB_PAT:?GITHUB_PAT must be set (via /root/af-deploy/secrets.env or env)}"
: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY must be set}"
: "${SEC_USER_AGENT:?SEC_USER_AGENT must be set}"

docker stop timeagent 2>/dev/null || true
docker rm   timeagent 2>/dev/null || true
docker build -q --build-arg GITHUB_PAT="$GITHUB_PAT" -t af-timeagent .
# --init reaps stray children; port bound to loopback (mars-net peers use
# timeagent:8004; nothing off-box should reach it directly).
# TIMEAGENT_LISTEN_ENABLED=0 in secrets.env pauses the NOTIFY consumer.
docker run -d --name timeagent --restart always --init --network mars-net \
  -p 127.0.0.1:8004:8004 \
  -e MARS_DB_HOST=mars-db -e MARS_DB_PORT=5432 -e MARS_DB_NAME=MARS \
  -e MARS_DB_USER=postgres -e MARS_DB_PASSWORD=postgres \
  -e OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
  -e SEC_USER_AGENT="$SEC_USER_AGENT" \
  -e AGENT_LISTEN_ENABLED="${TIMEAGENT_LISTEN_ENABLED:-1}" \
  af-timeagent
