#!/bin/bash
# One-time deploy of the MT5 CFD consumer stack on the ARM VM.
# Run from the repo root after cloning/pulling and configuring .env:
#     bash deploy/setup_mt5.sh
#
# Installs three systemd units so the consumer runs 24/7:
#   mt5-tunnel      SSH tunnel to the x86 feed VM (:8001), auto-restart
#   mt5-consumer    live 5m candle builder -> research_db.live_candles
#   mt5-watchdog    5-min timer that restarts stuck/failed units (forex-aware)

set -e

REPO=/home/ubuntu/algo-strategy
FEED_HOST=144.24.154.233
FEED_KEY_SRC="$REPO/ssh-key-2026-05-19.key"
FEED_KEY_DST=/home/ubuntu/.ssh/mt5_feed.key

echo "== 1. Python deps (mt5linux + rpyc==5.2.3) into venv =="
"$REPO/venv/bin/pip" install -q "rpyc==5.2.3" mt5linux
# psycopg2 is needed for Postgres storage; install only if missing.
"$REPO/venv/bin/python" -c "import psycopg2" 2>/dev/null || "$REPO/venv/bin/pip" install -q psycopg2-binary

echo "== 2. SSH key for the tunnel (chmod 600) + known_hosts =="
mkdir -p /home/ubuntu/.ssh
cp "$FEED_KEY_SRC" "$FEED_KEY_DST"
chmod 600 "$FEED_KEY_DST"
ssh-keyscan -H "$FEED_HOST" >> /home/ubuntu/.ssh/known_hosts 2>/dev/null || true
sort -u /home/ubuntu/.ssh/known_hosts -o /home/ubuntu/.ssh/known_hosts
chmod 600 /home/ubuntu/.ssh/known_hosts

echo "== 3. Make watchdog executable =="
chmod +x "$REPO/deploy/mt5-watchdog.sh"

echo "== 4. Install systemd units =="
sudo cp "$REPO/deploy/mt5-tunnel.service"    /etc/systemd/system/
sudo cp "$REPO/deploy/mt5-consumer.service"  /etc/systemd/system/
sudo cp "$REPO/deploy/mt5-watchdog.service"  /etc/systemd/system/
sudo cp "$REPO/deploy/mt5-watchdog.timer"    /etc/systemd/system/
sudo cp "$REPO/deploy/mt5-heartbeat.service" /etc/systemd/system/
sudo cp "$REPO/deploy/mt5-heartbeat.timer"   /etc/systemd/system/
sudo systemctl daemon-reload

echo "== 5. Enable + start (tunnel first, then consumer, then timers) =="
sudo systemctl enable --now mt5-tunnel
sleep 4
sudo systemctl enable --now mt5-consumer
sudo systemctl enable --now mt5-watchdog.timer
sudo systemctl enable --now mt5-heartbeat.timer

echo ""
echo "Done. The consumer will run 24/7 and survive reboots."
echo ""
echo "Verify:"
echo "  systemctl status mt5-tunnel mt5-consumer --no-pager"
echo "  journalctl -u mt5-consumer -f            # live logs / candles"
echo "  systemctl list-timers mt5-watchdog.timer mt5-heartbeat.timer"
echo "  $REPO/venv/bin/python -m app.mt5_heartbeat   # send a heartbeat now (test)"
echo ""
echo "Confirm rows landing in Postgres:"
echo "  PGPASSWORD=... psql -h localhost -U algo -d research_db -c \\"
echo "    \"SELECT instrument,count(*),max(session_date) FROM live_candles GROUP BY 1 ORDER BY 1;\""
