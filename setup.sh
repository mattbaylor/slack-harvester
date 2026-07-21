#!/usr/bin/env bash
# Slack Harvester — First-time setup
# Run this once per machine. It will:
#   1. Install Python dependencies
#   2. Create a config.json from the example
#   3. Print the loopback api-token and next steps for the browser extension
#
# Credentials are NO LONGER scraped from Chrome (ISSUES #17). They are pushed
# to the harvester by a companion Chrome extension (extension/) that reads the
# LIVE Slack session and keeps it warm. Sign-in happens ONCE in the browser
# where you load that extension; there is no dedicated harvester Chrome profile
# to manage anymore.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/config.json"
EXAMPLE="$SCRIPT_DIR/config.example.json"

echo "=== Slack Harvester Setup ==="
echo ""

# --- Step 1: Python deps -------------------------------------------------
# NOTE: `cryptography` is no longer required at runtime (chrome_creds is
# retired from the live path, ISSUES #17). requirements.txt install is kept
# for any remaining deps; a failure here is non-fatal for the push model.
echo "[1/3] Installing Python dependencies..."
pip3 install -r "$SCRIPT_DIR/requirements.txt" --quiet 2>/dev/null || {
    echo "  pip3 install failed (non-fatal for the push model). Continuing."
}
echo "  Done."

# --- Step 2: Config -------------------------------------------------------
if [ -f "$CONFIG" ]; then
    echo "[2/3] config.json already exists, skipping."
else
    echo "[2/3] Creating config.json..."
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

# Create capture directories
CAPTURE_DIR=$(python3 -c "
import json, os
c = json.load(open('$CONFIG'))
vault = os.path.expanduser(c['vault_path'])
print(os.path.join(vault, c['capture_dir']))
")
mkdir -p "$CAPTURE_DIR/_pending" "$CAPTURE_DIR/_state" 2>/dev/null || true
echo "  Vault capture directory: $CAPTURE_DIR"

# --- Step 3: Extension handoff -------------------------------------------
STATE_DIR=$(python3 -c "
import json, os
c = json.load(open('$CONFIG'))
print(os.path.expanduser(c.get('state_dir', '~/.local/state/slack-harvester')))
")
API_TOKEN_FILE="$STATE_DIR/api-token"

echo ""
echo "[3/3] Browser extension handoff"
echo ""
echo "  The harvester now receives credentials from the companion extension in"
echo "  '$SCRIPT_DIR/extension/'. To finish setup:"
echo ""
echo "  1. Start the harvester once so it generates its loopback api-token:"
echo "         python3 $SCRIPT_DIR/harvester.py"
echo "     (It will start with no credentials and idle-wait for the first push.)"
echo ""
echo "  2. In Chrome, load the unpacked extension:"
echo "         chrome://extensions  ->  Developer mode  ->  Load unpacked"
echo "         ->  select  $SCRIPT_DIR/extension"
echo ""
echo "  3. Sign into Slack WEB once in that Chrome"
echo "     (https://app.slack.com/ — the extension reads the LIVE session)."
echo ""
echo "  4. Copy config.example.json to config.json inside the extension dir and"
echo "     paste the harvester's api-token into its \"apiToken\" field."
echo ""
if [ -f "$API_TOKEN_FILE" ]; then
    echo "     Your api-token (from $API_TOKEN_FILE):"
    echo "         $(cat "$API_TOKEN_FILE")"
else
    echo "     The api-token file does not exist yet. Run the harvester once"
    echo "     (step 1), then read it from: $API_TOKEN_FILE"
fi
echo ""
echo "  Setup complete. The extension will push creds on its refresh alarm and"
echo "  '/health' will flip has_credentials:true after the first successful push."
