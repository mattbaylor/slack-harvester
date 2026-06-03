#!/usr/bin/env bash
# Slack Harvester — Install as a launchd background service.
#
# Run this AFTER ./setup.sh has succeeded (i.e. config.json exists and
# credentials are readable).
#
# What this does:
#   1. Installs `cryptography` to a stable location (~/.local/lib/slack-harvester-deps).
#      Stable so the launchd-spawned Python finds it regardless of pyenv
#      shims, Python version upgrades, or per-user site changes.
#   2. Renders the launchd plists (main + healthcheck) from .example
#      templates, substituting your paths and chosen label namespace.
#   3. Copies the rendered plists to ~/Library/LaunchAgents/.
#   4. Bootstraps both services so they start now and on every login.
#   5. Verifies the harvester is responding on /health.
#
# Re-runnable. Reloads the services if they already exist.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/config.json"

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: $CONFIG not found. Run ./setup.sh first."
    exit 1
fi

echo "=== Slack Harvester — launchd install ==="
echo ""

# --- Step 1: deps -----------------------------------------------------------

DEPS_PATH="$HOME/.local/lib/slack-harvester-deps"
echo "[1/5] Installing cryptography to $DEPS_PATH"

mkdir -p "$DEPS_PATH"

# Use /usr/bin/python3 explicitly — that's what the launchd job will run.
# This avoids "works on my interactive shell, fails under launchd" where
# pyenv or a brew-python is on PATH interactively but not in launchd's env.
/usr/bin/python3 -m pip install --quiet --target "$DEPS_PATH" cryptography || {
    echo "  pip install failed."
    exit 1
}

# Sanity check: launchd-equivalent env can import cryptography.
env -i PATH="/usr/bin:/bin" HOME="$HOME" PYTHONPATH="$DEPS_PATH" \
    /usr/bin/python3 -c "import cryptography; print('  cryptography', cryptography.__version__, 'OK')" || {
    echo "  cryptography import failed under launchd-equivalent env. Aborting."
    exit 1
}

# --- Step 2: opencode sanity check ------------------------------------------

echo ""
echo "[2/5] Checking opencode is installed and authenticated"

OPENCODE_BIN="$(command -v opencode 2>/dev/null || true)"
if [ -z "$OPENCODE_BIN" ]; then
    echo "  ERROR: opencode not found on PATH."
    echo "  Install it (https://opencode.ai/docs/install) and authenticate"
    echo "  it to a model provider (e.g. GitHub Copilot OAuth) before re-running."
    exit 1
fi
echo "  opencode: $OPENCODE_BIN ($(opencode --version 2>&1 || echo unknown))"

# Probe under launchd-equivalent env. Confirms model auth works without HOME tricks.
PROBE_OUTPUT=$(env -i PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
    HOME="$HOME" "$OPENCODE_BIN" run \
    "Reply with the single word PONG and nothing else. Do not invoke any tools." \
    2>&1 | tail -5) || true
if echo "$PROBE_OUTPUT" | grep -qi PONG; then
    echo "  opencode probe: OK (model auth working)"
else
    echo "  ERROR: opencode probe did not return PONG."
    echo "  Output was:"
    echo "$PROBE_OUTPUT" | sed 's/^/    /'
    echo ""
    echo "  Most likely cause: opencode is not authenticated to a working"
    echo "  model provider. Run \`opencode\` once interactively to set up auth,"
    echo "  then re-run this script."
    exit 1
fi

# --- Step 3: choose label namespace -----------------------------------------

echo ""
echo "[3/5] Choosing launchd label namespace"

# Reverse-DNS labels are conventional. Default to com.<username>.* so two
# users on the same machine don't collide.
DEFAULT_NS="com.$(id -un | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9-')"
read -rp "  Label namespace [default: $DEFAULT_NS]: " LABEL_NS
LABEL_NS="${LABEL_NS:-$DEFAULT_NS}"

HARVESTER_LABEL="$LABEL_NS.slack-harvester"
HEALTHCHECK_LABEL="$LABEL_NS.slack-harvester-healthcheck"
echo "  Harvester:   $HARVESTER_LABEL"
echo "  Healthcheck: $HEALTHCHECK_LABEL"

# --- Step 4: render and install plists --------------------------------------

echo ""
echo "[4/5] Rendering and installing launchd plists"

AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS_DIR"

render_plist() {
    local src="$1"
    local dest="$2"
    local label="$3"
    sed \
        -e "s|{{LABEL}}|$label|g" \
        -e "s|{{HARVESTER_LABEL}}|$HARVESTER_LABEL|g" \
        -e "s|{{REPO_PATH}}|$SCRIPT_DIR|g" \
        -e "s|{{DEPS_PATH}}|$DEPS_PATH|g" \
        "$src" > "$dest"
    echo "  Wrote $dest"
}

HARVESTER_PLIST="$AGENTS_DIR/$HARVESTER_LABEL.plist"
HEALTHCHECK_PLIST="$AGENTS_DIR/$HEALTHCHECK_LABEL.plist"

render_plist "$SCRIPT_DIR/com.example.slack-harvester.plist.example" \
    "$HARVESTER_PLIST" "$HARVESTER_LABEL"
render_plist "$SCRIPT_DIR/com.example.slack-harvester-healthcheck.plist.example" \
    "$HEALTHCHECK_PLIST" "$HEALTHCHECK_LABEL"

# --- Step 5: (re)bootstrap services -----------------------------------------

echo ""
echo "[5/5] Bootstrapping launchd services"

reload_service() {
    local label="$1"
    local plist="$2"
    if launchctl list "$label" >/dev/null 2>&1; then
        echo "  $label: already loaded, reloading..."
        launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
        sleep 1
    fi
    launchctl bootstrap "gui/$(id -u)" "$plist"
    echo "  $label: bootstrapped."
}

reload_service "$HARVESTER_LABEL" "$HARVESTER_PLIST"
reload_service "$HEALTHCHECK_LABEL" "$HEALTHCHECK_PLIST"

# --- Verify -----------------------------------------------------------------

echo ""
echo "Waiting for harvester to come up..."
sleep 15

HEALTH_PORT=$(python3 -c "import json; print(json.load(open('$CONFIG')).get('health_port', 7777))")
HEALTH_URL="http://127.0.0.1:$HEALTH_PORT/health"

if curl -s -m 5 "$HEALTH_URL" | python3 -m json.tool 2>/dev/null; then
    echo ""
    echo "=== Install complete ==="
    echo ""
    echo "Logs:"
    echo "  harvester:   tail -f /private/tmp/harvester.log"
    echo "  healthcheck: tail -f /private/tmp/harvester-healthcheck.log"
    echo ""
    echo "Control:"
    echo "  status:  launchctl print gui/\$(id -u)/$HARVESTER_LABEL"
    echo "  restart: launchctl kickstart -k gui/\$(id -u)/$HARVESTER_LABEL"
    echo "  stop:    launchctl bootout gui/\$(id -u)/$HARVESTER_LABEL"
else
    echo ""
    echo "WARNING: $HEALTH_URL did not respond."
    echo "Check the log: tail -f /private/tmp/harvester.log"
    echo "The service may still be starting (opencode self-test can take ~10s)."
    exit 1
fi
