#!/usr/bin/env bash
# Slack Harvester health check.
# Pings the harvester's /health endpoint. If it's down or unhealthy,
# sends a Slack DM to the authenticated user via the harvester's own creds.
#
# Designed to run via launchd every 5 minutes.

set -euo pipefail

# Credentials are pushed by the extension (ISSUES #17); this script no longer
# imports chrome_creds and does not need `cryptography` on PYTHONPATH. Liveness
# is read straight off the harvester's /health endpoint (has_credentials), and
# the alert is delivered via macOS notification (the harvester DMs itself on
# its own failure paths using its in-memory pushed creds).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/config.json"

# Read state_dir and health port from config.json so this script doesn't
# carry user-specific paths.
if [ -f "$CONFIG" ]; then
    STATE_DIR=$(python3 -c "import json, os; c=json.load(open('$CONFIG')); print(os.path.expanduser(c.get('state_dir', '~/.local/state/slack-harvester')))")
    HEALTH_PORT=$(python3 -c "import json; print(json.load(open('$CONFIG')).get('health_port', 7777))")
else
    STATE_DIR="$HOME/.local/state/slack-harvester"
    HEALTH_PORT=7777
fi
mkdir -p "$STATE_DIR"

HEALTH_URL="http://127.0.0.1:$HEALTH_PORT/health"
ALERT_COOLDOWN_FILE="$STATE_DIR/.last-alert"
COOLDOWN_SECONDS=1800  # Don't re-alert within 30 minutes

# Label of the launchd job, for the restart hint in the alert message. Falls
# back to the conventional template if not set in the plist.
HARVESTER_LABEL="${HARVESTER_LAUNCHD_LABEL:-com.<your-namespace>.slack-harvester}"

# --- Check cooldown ---
if [ -f "$ALERT_COOLDOWN_FILE" ]; then
    last_alert=$(cat "$ALERT_COOLDOWN_FILE")
    now=$(date +%s)
    elapsed=$(( now - last_alert ))
    if [ "$elapsed" -lt "$COOLDOWN_SECONDS" ]; then
        exit 0  # Still in cooldown, skip
    fi
fi

# --- Ping health endpoint ---
healthy=true
reason=""

response=$(curl -s -m 5 "$HEALTH_URL" 2>/dev/null) || {
    healthy=false
    reason="Health endpoint unreachable (harvester not running?)"
}

if $healthy; then
    status=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
    has_creds=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('has_credentials',''))" 2>/dev/null || echo "")

    if [ "$status" != "ok" ]; then
        healthy=false
        reason="Health status: $status (expected: ok)"
    elif [ "$has_creds" = "False" ]; then
        healthy=false
        reason="Credentials missing — token/cookie may have expired"
    fi
fi

# --- If healthy, clear cooldown and exit ---
if $healthy; then
    rm -f "$ALERT_COOLDOWN_FILE"
    exit 0
fi

# --- Alert: macOS notification ---
# Under the push model (ISSUES #17) this script does NOT hold Slack creds — it
# no longer scrapes Chrome, and when the harvester is unreachable/credless
# there is no loopback path to send a Slack DM. So the down-alert is a local
# macOS notification. (The harvester itself DMs on its own failure paths via
# _dm_self using its in-memory pushed creds, e.g. the startup self-test; and
# the extension fires its own logout notification — ISSUES #17 O2.)
osascript -e "display notification \"$reason\" with title \"Slack Harvester Down\"" 2>/dev/null || true
date +%s > "$ALERT_COOLDOWN_FILE"
