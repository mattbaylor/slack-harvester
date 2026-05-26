#!/usr/bin/env bash
# Slack Harvester — First-time setup
# Run this once per machine. It will:
#   1. Install Python dependencies
#   2. Create a config.json from the example
#   3. Open Chrome with a dedicated profile so you can sign into Slack
#   4. Verify credentials are readable
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/config.json"
EXAMPLE="$SCRIPT_DIR/config.example.json"

echo "=== Slack Harvester Setup ==="
echo ""

# --- Step 1: Python deps -------------------------------------------------
echo "[1/4] Installing Python dependencies..."
pip3 install -r "$SCRIPT_DIR/requirements.txt" --quiet 2>/dev/null || {
    echo "  pip3 install failed. You may need: pip3 install cryptography"
    exit 1
}
echo "  Done."

# --- Step 2: Config -------------------------------------------------------
if [ -f "$CONFIG" ]; then
    echo "[2/4] config.json already exists, skipping."
else
    echo "[2/4] Creating config.json..."
    echo ""

    read -rp "  Slack workspace URL (e.g. https://mycompany.slack.com/): " workspace_url
    workspace_url="${workspace_url%/}/"  # Ensure trailing slash

    read -rp "  Vault path (e.g. ~/vault or ~/Documents/obsidian): " vault_path

    read -rp "  Capture directory name inside vault (default: slack-captures): " capture_dir
    capture_dir="${capture_dir:-slack-captures}"

    # Generate config
    python3 -c "
import json, sys

config = json.load(open('$EXAMPLE'))
config['workspace_url'] = '$workspace_url'
config['vault_path'] = '$vault_path'
config['capture_dir'] = '$capture_dir'

json.dump(config, open('$CONFIG', 'w'), indent=2)
print('  Wrote', '$CONFIG')
"
fi

# --- Step 3: Chrome sign-in -----------------------------------------------
CHROME_PROFILE=$(python3 -c "
import json, os
c = json.load(open('$CONFIG'))
print(os.path.expanduser(c['chrome_profile']))
")
WORKSPACE_URL=$(python3 -c "
import json; print(json.load(open('$CONFIG'))['workspace_url'])
")

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ ! -x "$CHROME" ]; then
    CHROME="$(command -v google-chrome || command -v google-chrome-stable || echo "")"
fi

if [ -z "$CHROME" ]; then
    echo ""
    echo "ERROR: Chrome not found. Install Chrome, then re-run this script."
    exit 1
fi

echo ""
echo "[3/4] Opening Chrome for Slack sign-in..."
echo "  Profile: $CHROME_PROFILE"
echo "  URL:     $WORKSPACE_URL"
echo ""
echo "  Sign into Slack in the Chrome window that opens."
echo "  Once you see your Slack workspace loaded, close Chrome and come back here."
echo ""
read -rp "  Press Enter to open Chrome..."

"$CHROME" \
    --user-data-dir="$CHROME_PROFILE" \
    --no-first-run \
    --disable-notifications \
    "$WORKSPACE_URL" 2>/dev/null &

CHROME_PID=$!

echo ""
echo "  Chrome is open (PID: $CHROME_PID)."
echo "  Sign in, wait for Slack to fully load, then close Chrome."
echo ""
read -rp "  Press Enter after you've signed in and closed Chrome..."

# Kill Chrome if still running
kill "$CHROME_PID" 2>/dev/null || true
sleep 1

# --- Step 4: Verify -------------------------------------------------------
echo ""
echo "[4/4] Verifying credentials..."

VAULT_PATH=$(python3 -c "
import json, os
c = json.load(open('$CONFIG'))
print(os.path.expanduser(c['vault_path']))
")

python3 -c "
import sys, os, json
sys.path.insert(0, '$SCRIPT_DIR')
from chrome_creds import read_credentials
from pathlib import Path

config = json.load(open('$CONFIG'))
profile = Path(os.path.expanduser(config['chrome_profile']))
token, cookie = read_credentials(profile)

if token and cookie:
    print(f'  Token:  OK ({len(token)} chars)')
    print(f'  Cookie: OK ({len(cookie)} chars)')
    print()
    print('Setup complete! Run the harvester with:')
    print(f'  python3 $SCRIPT_DIR/harvester.py')
else:
    print('  ERROR: Could not read credentials.')
    print('  Make sure you signed into Slack and the page fully loaded.')
    print('  Re-run this script to try again.')
    sys.exit(1)
" || {
    echo "  Credential verification failed."
    exit 1
}

# Create capture directories
CAPTURE_DIR=$(python3 -c "
import json, os
c = json.load(open('$CONFIG'))
vault = os.path.expanduser(c['vault_path'])
print(os.path.join(vault, c['capture_dir']))
")
mkdir -p "$CAPTURE_DIR/_pending" "$CAPTURE_DIR/_state" 2>/dev/null || true
echo ""
echo "  Vault capture directory: $CAPTURE_DIR"
