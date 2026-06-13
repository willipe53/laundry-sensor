#!/usr/bin/env bash
# Backstop liveness probe for laundry-sensor.
#
# Hits the /healthz endpoint and, on failure, restarts the systemd unit.
# This is layered *on top of* the systemd WatchdogSec (which already handles
# event-loop hangs); the value here is catching scenarios where the HTTP
# server is fine but the monitor has been stuck for too long, or where
# something has wedged below systemd's notice.
#
# Wire into root's crontab:
#   * * * * * /opt/laundry-sensor/health-check.sh >> /var/log/laundry-health.log 2>&1
#
# Or run via a systemd timer if you prefer.

set -u

URL="${LAUNDRY_HEALTH_URL:-https://127.0.0.1/healthz}"
UNIT="${LAUNDRY_UNIT:-laundry-sensor.service}"
TIMEOUT="${LAUNDRY_HEALTH_TIMEOUT:-10}"
LOG_TAG="laundry-health"

log() {
    printf '%s %s\n' "$(date -Iseconds)" "$*"
    logger -t "$LOG_TAG" -- "$*" || true
}

if curl -fsS -k --max-time "$TIMEOUT" "$URL" > /dev/null; then
    exit 0
fi

log "healthz failed (URL=$URL); restarting $UNIT"
systemctl restart "$UNIT"
exit 1
