#!/usr/bin/env bash
# One-shot setup for the HORIZON Telegram bridge.
#
# Run:  ./setup.sh
#
# Asks for the two secrets (input hidden), writes .env with tight permissions,
# runs the test suite, and starts the bridge. Nothing is echoed back, nothing is
# logged, and the secrets never leave the machine.

set -uo pipefail

cd "$(dirname "$0")"

say()  { printf '%s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

say "HORIZON Telegram bridge — setup"
say ""

command -v python3 >/dev/null 2>&1 || fail "python3 is not installed"

# --- Reuse an existing .env, so a re-run just starts the bridge -------------
if [ -f .env ]; then
    say "Found an existing .env — starting with it."
    say "(Delete .env if you want to enter the secrets again.)"
else
    say "You need two values. Neither is shown as you type."
    say ""
    printf 'Telegram bot token (from @BotFather): '
    read -rs TELEGRAM_BOT_TOKEN
    say ""
    [ -n "${TELEGRAM_BOT_TOKEN}" ] || fail "no token entered"

    printf 'OpenHands API key (app.all-hands.dev -> Settings -> API keys): '
    read -rs OPENHANDS_CLOUD_API_KEY
    say ""
    [ -n "${OPENHANDS_CLOUD_API_KEY}" ] || fail "no OpenHands key entered"

    umask 077
    cat > .env <<EOF
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_ALLOWED_USER_IDS=2065255514
OPENHANDS_MODE=cloud
OPENHANDS_CLOUD_API_KEY=${OPENHANDS_CLOUD_API_KEY}
OPENHANDS_REPOSITORY=tarifberatung24-blip/VZGplattform
OPENHANDS_BRANCH=main
EOF
    chmod 600 .env
    unset TELEGRAM_BOT_TOKEN OPENHANDS_CLOUD_API_KEY
    say "Wrote .env (owner-only permissions)."
fi

say ""
say "Running the tests first..."
if python3 -m unittest discover -s tests >/tmp/horizon-bridge-tests.log 2>&1; then
    say "Tests: OK"
else
    say "Tests failed — see /tmp/horizon-bridge-tests.log"
    fail "refusing to start on a failing test suite"
fi

say ""
say "Starting the bridge. Press Ctrl+C to stop."
say "Send the bot any message in Telegram to begin."
say ""

set -a
# shellcheck disable=SC1091
. ./.env
set +a

exec python3 bridge.py
