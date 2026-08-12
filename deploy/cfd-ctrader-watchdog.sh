#!/bin/bash
# cTrader candle-archiver watchdog — runs every 5 min via systemd timer.
#
# systemd's Restart=always already recovers crashes. This catches what it can't:
# a process that is "active" but hung/silent, or a unit stuck in failed. Unlike
# the MT5 watchdog there is NO tunnel to police — cTrader connects directly.
#
#   - Ensures cfd-ctrader is active (restarts if failed).
#   - Only enforces *liveness* when the forex market is OPEN (the runner idles
#     and stays quiet when the market is closed).
#   - Sends a Telegram alert on any restart (CFD bot creds from .env, optional).
#
# Runs as root (needs systemctl restart). No `set -e`: a watchdog must finish
# all its checks even if one command returns non-zero.
REPO=/home/ubuntu/algo-strategy
PY=$REPO/venv/bin/python
MAX_SILENCE=420   # 7 min without a log line while market open = stuck

cd "$REPO" || exit 0

# ── CFD Telegram creds (DEDICATED CFD bot — never the NSE bot) ──
TG_TOKEN=$(grep -E '^(CTRADER|MT5)_TELEGRAM_BOT_TOKEN=' "$REPO/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' | head -1)
TG_CHATS=$(grep -E '^(CTRADER|MT5)_TELEGRAM_CHAT_ID='  "$REPO/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' | head -1)
alert() {
    local msg="$1"
    echo "[$(date)] $msg"
    if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHATS" ]; then
        IFS=',' read -ra CHATS <<< "$TG_CHATS"
        for chat in "${CHATS[@]}"; do
            curl -s -m 10 -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
                -d chat_id="${chat}" --data-urlencode text="[cTrader archiver] ${msg}" >/dev/null 2>&1 || true
        done
    fi
}

# ── 1) Unit must be active ───────────────────────────────────
if ! systemctl is-active --quiet cfd-ctrader; then
    alert "cfd-ctrader not active — restarting"
    systemctl restart cfd-ctrader
fi

# ── 2) Liveness — only while the market is open ──────────────
if $PY -c "from app.utils import forex_hours; import sys; sys.exit(0 if forex_hours.is_market_open() else 1)" 2>/dev/null; then
    LAST=$(journalctl -u cfd-ctrader -n 1 --output=short-unix --no-pager 2>/dev/null | tail -1 | awk '{print int($1)}')
    NOW=$(date +%s)
    if [ -z "$LAST" ] || [ "$LAST" -eq 0 ]; then
        alert "no logs from cfd-ctrader while market OPEN — restarting"
        systemctl restart cfd-ctrader
    else
        SILENCE=$((NOW - LAST))
        if [ "$SILENCE" -gt "$MAX_SILENCE" ]; then
            alert "cfd-ctrader silent ${SILENCE}s (>${MAX_SILENCE}s) while market OPEN — restarting"
            systemctl restart cfd-ctrader
        else
            echo "[$(date)] OK: cfd-ctrader last log ${SILENCE}s ago (market open)"
        fi
    fi
else
    echo "[$(date)] market closed — liveness check skipped"
fi
exit 0
