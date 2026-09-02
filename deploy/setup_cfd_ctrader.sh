#!/bin/bash
# One-time deploy of the cTrader candle archiver on the ARM VM.
# Run from the repo root after pulling and configuring .env (CTRADER_* tokens):
#     bash deploy/setup_cfd_ctrader.sh
#
# This runs IN PARALLEL with mt5-consumer (no Conflicts= line) so both write
# candles and you can compare them before cutting over. cTrader writes to the
# `ctrader_staging_candles` table by default; once you trust the feeds match:
#   1. Set CFD_CTRADER_STAGING=false in the service
#   2. Stop+disable mt5-consumer (and the tunnel/feed VM)
#   3. cTrader becomes the sole writer to live_candles
#
# Installs:
#   cfd-ctrader.service               push feed -> 5m candles -> staging table
#   cfd-ctrader-watchdog .service/.timer   5-min forex-aware liveness watchdog
set -e
REPO=/home/ubuntu/algo-strategy

echo "== 1. Sanity: venv + ctrader lib present =="
test -x "$REPO/venv/bin/python" || { echo "venv missing — create it first"; exit 1; }
"$REPO/venv/bin/python" -c "import ctrader_api_client" 2>/dev/null || {
    echo "Installing ctrader-api-client..."
    "$REPO/venv/bin/pip" install -q 'ctrader-api-client>=0.8.0'
}
# psycopg2 for Postgres.
"$REPO/venv/bin/python" -c "import psycopg2" 2>/dev/null || "$REPO/venv/bin/pip" install -q psycopg2-binary

echo "== 2. Make watchdog executable =="
chmod +x "$REPO/deploy/cfd-ctrader-watchdog.sh"

echo "== 3. Install systemd units =="
sudo cp "$REPO/deploy/cfd-ctrader.service"             /etc/systemd/system/
sudo cp "$REPO/deploy/cfd-ctrader-watchdog.service"    /etc/systemd/system/
sudo cp "$REPO/deploy/cfd-ctrader-watchdog.timer"      /etc/systemd/system/
sudo systemctl daemon-reload

echo "== 4. Enable + start (runs alongside mt5-consumer — no conflict) =="
sudo systemctl enable --now cfd-ctrader
sudo systemctl enable --now cfd-ctrader-watchdog.timer

echo ""
echo "Done. The cTrader candle archiver runs 24/5 alongside mt5-consumer."
echo ""
echo "Verify:"
echo "  systemctl status cfd-ctrader --no-pager"
echo "  journalctl -u cfd-ctrader -f           # live logs / candles"
echo "  systemctl list-timers cfd-ctrader-watchdog.timer"
echo ""
echo "Compare feeds (after a few days of parallel accumulation):"
echo "  venv/bin/python -m app.tools.compare_candles --from \$(date +%Y-%m-%d)"
echo ""
echo "Cut over to cTrader as the sole writer (AFTER comparing):"
echo "  1. Edit /etc/systemd/system/cfd-ctrader.service: CFD_CTRADER_STAGING=false"
echo "  2. sudo systemctl daemon-reload && sudo systemctl restart cfd-ctrader"
echo "  3. sudo systemctl disable --now mt5-consumer mt5-tunnel mt5-watchdog.timer"
echo "  4. (Optional) retire the x86 MT5 feed VM entirely."
