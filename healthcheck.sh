#!/usr/bin/env bash
# Slack Harvester health check.
# Pings the harvester's /health endpoint. If it's down or unhealthy,
# sends a Slack DM to the authenticated user via the harvester's own creds.
#
# Designed to run via launchd every 5 minutes.

set -euo pipefail

# Ensure cryptography package is importable for chrome_creds.py.
# Set HARVESTER_PYTHONPATH in the launchd plist (or your shell) if your
# Python install needs a non-default site-packages on PYTHONPATH.
if [ -n "${HARVESTER_PYTHONPATH:-}" ]; then
    export PYTHONPATH="${PYTHONPATH:-}:${HARVESTER_PYTHONPATH}"
fi

HEALTH_URL="http://127.0.0.1:7777/health"
STATE_DIR="/Users/matt/.local/state/slack-harvester"
ALERT_COOLDOWN_FILE="$STATE_DIR/.last-alert"
COOLDOWN_SECONDS=1800  # Don't re-alert within 30 minutes

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

# --- Alert: send Slack DM ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/config.json"

# Read Chrome profile path and credentials
chrome_profile=$(python3 - "$SCRIPT_DIR" "$CONFIG" << 'PYEOF'
import json, os, sys
script_dir, config_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, script_dir)
c = json.load(open(config_path))
print(os.path.expanduser(c['chrome_profile']))
PYEOF
) || chrome_profile=""

if [ -n "$chrome_profile" ]; then
    creds=$(python3 - "$SCRIPT_DIR" "$chrome_profile" << 'PYEOF'
import sys, os
sys.path.insert(0, sys.argv[1])
from chrome_creds import read_credentials
from pathlib import Path
token, cookie = read_credentials(Path(sys.argv[2]))
if token and cookie:
    print(token)
    print(cookie)
PYEOF
) || creds=""

    if [ -n "$creds" ]; then
        token=$(echo "$creds" | head -1)
        cookie=$(echo "$creds" | tail -1)

        # Get our own user ID
        user_id=$(curl -s -m 10 \
            -H "Authorization: Bearer $token" \
            -H "Cookie: d=$cookie" \
            -d "" \
            "https://slack.com/api/auth.test" 2>/dev/null \
            | python3 -c "import sys,json; print(json.load(sys.stdin).get('user_id',''))" 2>/dev/null || echo "")

        if [ -n "$user_id" ]; then
            # Open a DM channel to ourselves
            dm_channel=$(curl -s -m 10 \
                -H "Authorization: Bearer $token" \
                -H "Cookie: d=$cookie" \
                -H "Content-Type: application/x-www-form-urlencoded" \
                -d "users=$user_id" \
                "https://slack.com/api/conversations.open" 2>/dev/null \
                | python3 -c "import sys,json; print(json.load(sys.stdin).get('channel',{}).get('id',''))" 2>/dev/null || echo "")

            if [ -n "$dm_channel" ]; then
                msg=":rotating_light: *Slack Harvester is down*\n\n$reason\n\nCheck: \`tail -50 /private/tmp/harvester.log\`\nRestart: \`launchctl kickstart gui/\$(id -u)/com.baylor.slack-harvester\`"

                curl -s -m 10 \
                    -H "Authorization: Bearer $token" \
                    -H "Cookie: d=$cookie" \
                    -H "Content-Type: application/x-www-form-urlencoded" \
                    -d "channel=$dm_channel" \
                    --data-urlencode "text=$msg" \
                    "https://slack.com/api/chat.postMessage" >/dev/null 2>&1

                # Record alert time for cooldown
                date +%s > "$ALERT_COOLDOWN_FILE"
                exit 0
            fi
        fi
    fi
fi

# --- Fallback: macOS notification ---
osascript -e "display notification \"$reason\" with title \"Slack Harvester Down\"" 2>/dev/null || true
date +%s > "$ALERT_COOLDOWN_FILE"
