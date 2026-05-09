#!/bin/bash
# Daily scheduler — runs daily-ops and feedback on a cron-like schedule.
# Designed for the Docker scheduler service.
set -euo pipefail

DAILY_OPS_HOUR=${DAILY_OPS_HOUR:-2}       # UTC hour for daily ops (default 02:00)
FEEDBACK_INTERVAL_MIN=${FEEDBACK_INTERVAL_MIN:-60}  # Minutes between feedback runs

echo "[scheduler] Starting with daily-ops at ${DAILY_OPS_HOUR}:00 UTC, feedback every ${FEEDBACK_INTERVAL_MIN} min"

while true; do
    current_hour=$(date -u +%H)
    current_min=$(date -u +%M)

    # Daily ops at configured hour (plus a few min jitter)
    if [ "$current_hour" = "$DAILY_OPS_HOUR" ] && [ "${current_min#0}" -lt 10 ]; then
        echo "[scheduler] $(date -u -Iseconds) Running daily-ops..."
        python /app/main.py daily-ops --output /app/data/reports/daily_ops.json || true
        echo "[scheduler] $(date -u -Iseconds) Running feedback loop..."
        python /app/scripts/feedback_loop.py || true
        # Sleep past the 10-minute window
        sleep 600
    fi

    # Feedback loop on interval
    if [ $((10#${current_min#0} % FEEDBACK_INTERVAL_MIN)) -lt 5 ]; then
        echo "[scheduler] $(date -u -Iseconds) Running feedback loop..."
        python /app/scripts/feedback_loop.py || true
        sleep 300
    fi

    sleep 60
done
