#!/bin/bash
# One-time deploy of the CFD PAPER TRADER on the ARM VM.
# Run from the repo root after pulling and configuring .env:
#     bash deploy/setup_cfd_paper.sh
#
# The paper trader is a SUPERSET of the plain consumer (app.main_mt5): it builds
# + archives the same 5m candles AND runs strategies through the paper executor.
# It therefore REPLACES mt5-consumer — running both would double the poll load
# on the 1 GB feed VM. This script disables mt5-consumer and enables cfd-paper.
#
# Reuses the existing mt5-tunnel.service (SSH tunnel to the x86 feed VM :8001);
# run deploy/setup_mt5.sh first if the tunnel/venv aren't set up yet.
#
# Installs:
#   cfd-paper.service           feed -> 5m candles -> strategies -> paper executor
#   cfd-paper-watchdog .service/.timer   5-min forex-aware liveness watchdog

set -e

REPO=/home/ubuntu/algo-strategy

echo "== 1. Sanity: venv + tunnel present =="
test -x "$REPO/venv/bin/python" || { echo "venv missing — run deploy/setup_mt5.sh first"; exit 1; }
"$REPO/venv/bin/python" -c "import psycopg2" 2>/dev/null || "$REPO/venv/bin/pip" install -q psycopg2-binary

echo "== 2. Make watchdog executable =="
chmod +x "$REPO/deploy/cfd-paper-watchdog.sh"

echo "== 3. Install systemd units =="
sudo cp "$REPO/deploy/cfd-paper.service"          /etc/systemd/system/
sudo cp "$REPO/deploy/cfd-paper-watchdog.service" /etc/systemd/system/
sudo cp "$REPO/deploy/cfd-paper-watchdog.timer"   /etc/systemd/system/
sudo systemctl daemon-reload

echo "== 4. Stop the plain consumer (cfd-paper replaces it) =="
if systemctl is-enabled --quiet mt5-consumer 2>/dev/null; then
    sudo systemctl disable --now mt5-consumer || true
    echo "   mt5-consumer disabled (cfd-paper now owns the feed)."
fi

echo "== 5. Ensure the tunnel is up, then enable + start the paper trader =="
sudo systemctl enable --now mt5-tunnel
sleep 4
sudo systemctl enable --now cfd-paper
sudo systemctl enable --now cfd-paper-watchdog.timer

echo ""
echo "Done. The CFD paper trader runs 24/7 and survives reboots."
echo ""
echo "Verify:"
echo "  systemctl status mt5-tunnel cfd-paper --no-pager"
echo "  journalctl -u cfd-paper -f            # live logs / candles / signals / fills"
echo "  systemctl list-timers cfd-paper-watchdog.timer"
echo ""
echo "Select which strategies run (optional; default = all registered):"
echo "  add to .env:  CFD_PAPER_STRATEGIES=your_strategy_id,another_id"
echo "  then:         sudo systemctl restart cfd-paper"
echo ""
echo "Inspect paper trades in Postgres:"
echo "  PGPASSWORD=... psql -h localhost -U algo -d research_db -c \\"
echo "    \"SELECT strategy_id,instrument,direction,net_pnl_usd,exit_reason,exit_time_ms"
echo "       FROM cfd_paper_trades WHERE mode='PAPER' ORDER BY exit_time_ms DESC LIMIT 20;\""
