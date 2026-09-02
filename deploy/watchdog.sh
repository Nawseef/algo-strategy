#!/bin/bash
# Watchdog script: Restart algo services if they are stuck during market hours.
# Runs every 15 minutes via systemd timer. Only acts during market hours (9:00-16:30 IST, Mon-Fri).

set -e

CURRENT_HOUR=$(date +%H)
DAY_OF_WEEK=$(date +%u)  # 1=Monday, 7=Sunday

# Only check during market hours (9:00 - 16:30 IST) on weekdays
if [ "$DAY_OF_WEEK" -gt 5 ]; then
    exit 0  # Weekend
fi

if [ "$CURRENT_HOUR" -lt 9 ] || [ "$CURRENT_HOUR" -gt 16 ]; then
    exit 0  # Outside market hours
fi

# Check last log timestamp for each service
check_and_restart() {
    local SERVICE="$1"
    local MAX_SILENCE_SECONDS=900  # 15 minutes without logs = stuck

    # Get the timestamp of the last journal entry for this service
    LAST_LOG_EPOCH=$(journalctl -u "$SERVICE" -n 1 --output=short-unix --no-pager 2>/dev/null | tail -1 | awk '{print int($1)}')
    NOW_EPOCH=$(date +%s)

    if [ -z "$LAST_LOG_EPOCH" ] || [ "$LAST_LOG_EPOCH" -eq 0 ]; then
        echo "[$(date)] WARNING: No logs found for $SERVICE. Restarting..."
        systemctl restart "$SERVICE"
        return
    fi

    SILENCE_SECONDS=$((NOW_EPOCH - LAST_LOG_EPOCH))

    if [ "$SILENCE_SECONDS" -gt "$MAX_SILENCE_SECONDS" ]; then
        echo "[$(date)] WARNING: $SERVICE silent for ${SILENCE_SECONDS}s (>${MAX_SILENCE_SECONDS}s). Restarting..."
        systemctl restart "$SERVICE"
    else
        echo "[$(date)] OK: $SERVICE last log ${SILENCE_SECONDS}s ago"
    fi
}

check_and_restart "algo-paper"
check_and_restart "algo-research"
