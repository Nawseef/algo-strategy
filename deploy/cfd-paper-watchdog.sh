#!/bin/bash
# CFD paper-trader watchdog — runs every 5 min via systemd timer.
#
# systemd's Restart=always already recovers crashes. This catches the cases it
# can't: a process that is "active" but hung/silent, or a unit stuck in failed.
#
#   - Ensures mt5-tunnel and cfd-paper are active (restarts if failed).
#   - Only enforces cfd-paper *liveness* when the forex market is OPEN, because
#     the runner intentionally idles (and stays quiet) when the market closes.
#   - Sends a Telegram alert on any restart (creds read from .env, optional).
#
# Runs as root (needs systemctl restart). Deliberately no `set -e`: a watchdog
# must complete all its checks even if one command returns non-zero.

REPO=/home/ubuntu/algo-strategy
PY=$REPO/venv/bin/python
MAX_SILENCE=420   # 7 min without a log line while market open = stuck

cd "$REPO" || exit 0

# ── CFD Telegram creds (DEDICATED — MT5_TELEGRAM_*, never the NSE bot) ──
TG_TOKEN=$(grep -E '^MT5_TELEGRAM_BOT_TOKEN=' "$REPO/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' | head -1)
TG_CHATS=$(grep -E '^MT5_TELEGRAM_CHAT_ID='  "$REPO/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' | head -1)

alert() {
    local msg="$1"
    echo "[$(date)] $msg"
    if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHATS" ]; then
        IFS=',' read -ra CHATS <<< "$TG_CHATS"
        for chat in "${CHATS[@]}"; do
            curl -s -m 10 -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
                -d chat_id="${chat}" --data-urlencode text="[CFD paper] ${msg}" >/dev/null 2>&1 || true
        done
    fi
}

ensure_active() {
    local svc="$1"
    if ! systemctl is-active --quiet "$svc"; then
        alert "$svc not active — restarting"
        systemctl restart "$svc"
    fi
}

# ── 1) Tunnel + paper trader units must be active ────────────
ensure_active mt5-tunnel
ensure_active cfd-paper

# ── 2) Runner liveness — only while the market is open ───────
if $PY -c "from app.utils import forex_hours; import sys; sys.exit(0 if forex_hours.is_market_open() else 1)" 2>/dev/null; then
    LAST=$(journalctl -u cfd-paper -n 1 --output=short-unix --no-pager 2>/dev/null | tail -1 | awk '{print int($1)}')
    NOW=$(date +%s)
    if [ -z "$LAST" ] || [ "$LAST" -eq 0 ]; then
        alert "no logs from cfd-paper while market OPEN — restarting"
        systemctl restart cfd-paper
    else
        SILENCE=$((NOW - LAST))
        if [ "$SILENCE" -gt "$MAX_SILENCE" ]; then
            alert "cfd-paper silent ${SILENCE}s (>${MAX_SILENCE}s) while market OPEN — restarting"
            systemctl restart cfd-paper
        else
            echo "[$(date)] OK: cfd-paper last log ${SILENCE}s ago (market open)"
        fi
    fi
else
    echo "[$(date)] market closed — liveness check skipped"
fi

exit 0
