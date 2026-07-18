#!/usr/bin/env bash
# Quick check: can this server reach the Telegram Bot API?
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

if [[ ! -f ".env" ]]; then
    echo "ERROR: .env not found. Run ./setup.sh first."
    exit 1
fi

# shellcheck disable=SC1091
source .env

if [[ -z "${BOT_TOKEN:-}" || "$BOT_TOKEN" == "your_bot_token_here" ]]; then
    echo "ERROR: BOT_TOKEN is not set in .env"
    exit 1
fi

CURL_OPTS=(--max-time 20 -sS)
if [[ -n "${TELEGRAM_PROXY:-}" ]]; then
    CURL_OPTS+=(--proxy "$TELEGRAM_PROXY")
    echo "Using proxy: ${TELEGRAM_PROXY##*@}"
fi

echo "Testing connection to api.telegram.org..."
if RESPONSE=$(curl "${CURL_OPTS[@]}" "https://api.telegram.org/bot${BOT_TOKEN}/getMe"); then
    if echo "$RESPONSE" | grep -q '"ok":true'; then
        USERNAME=$(echo "$RESPONSE" | grep -o '"username":"[^"]*"' | cut -d'"' -f4)
        echo "OK — connected as @${USERNAME}"
        exit 0
    fi
    echo "API responded but getMe failed:"
    echo "$RESPONSE"
    exit 1
else
    echo "FAILED — cannot reach Telegram API (timeout or blocked)."
    echo ""
    echo "If Telegram is blocked on this server, add to .env:"
    echo "  TELEGRAM_PROXY=socks5://127.0.0.1:1080"
    exit 1
fi
