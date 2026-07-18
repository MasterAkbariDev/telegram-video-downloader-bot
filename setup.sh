#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

echo ""
echo "============================================"
echo "  Telegram Video Downloader Bot — Setup"
echo "============================================"
echo ""

# --- Python ---
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    error "Python 3 is required but not found."
    echo "Install on Ubuntu/Debian:  sudo apt install -y python3 python3-venv python3-pip"
    echo "Or from: https://www.python.org/downloads/"
    exit 1
fi

PY_VERSION=$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$($PYTHON -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$($PYTHON -c 'import sys; print(sys.version_info.minor)')

if [[ "$PY_MAJOR" -lt 3 ]] || [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 9 ]]; then
    error "Python 3.9+ is required (found $PY_VERSION)."
    exit 1
fi
ok "Python $PY_VERSION found ($PYTHON)"

# ensure venv module is available (Debian/Ubuntu often need python3-venv)
if ! $PYTHON -c "import venv" 2>/dev/null; then
    error "Python venv module is missing."
    echo "Install:  sudo apt install -y python3-venv python3-pip"
    exit 1
fi
ok "Python venv module available"

# --- ffmpeg (required by yt-dlp for merging/converting) ---
if command -v ffmpeg &>/dev/null; then
    ok "ffmpeg found ($(ffmpeg -version 2>&1 | head -1))"
else
    warn "ffmpeg not found — installing is strongly recommended."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        if command -v brew &>/dev/null; then
            info "Installing ffmpeg via Homebrew…"
            brew install ffmpeg
            ok "ffmpeg installed"
        else
            warn "Install Homebrew (https://brew.sh) then run: brew install ffmpeg"
        fi
    elif command -v apt-get &>/dev/null; then
        info "Installing ffmpeg via apt…"
        sudo apt-get update -qq && sudo apt-get install -y ffmpeg
        ok "ffmpeg installed"
    else
        warn "Please install ffmpeg manually: https://ffmpeg.org/download.html"
    fi
fi

# --- Virtual environment ---
_venv_ok() {
    [[ -f ".venv/bin/activate" ]] && [[ -x ".venv/bin/python" ]]
}

if _venv_ok; then
    ok "Virtual environment already exists"
else
    if [[ -d ".venv" ]]; then
        warn "Broken .venv detected (missing bin/activate) — recreating…"
        rm -rf .venv
    else
        info "Creating virtual environment…"
    fi
    if ! $PYTHON -m venv .venv; then
        error "Failed to create virtual environment."
        echo "On Ubuntu/Debian try:  sudo apt install -y python3-venv python3-pip"
        exit 1
    fi
    if ! _venv_ok; then
        error "venv was created but still looks broken."
        exit 1
    fi
    ok "Virtual environment created"
fi

# shellcheck disable=SC1091
source .venv/bin/activate

info "Installing Python dependencies…"
pip install --upgrade pip -q
pip install -r requirements.txt -q
pip install -U "yt-dlp[default,curl-cffi]>=2024.12.23" -q
ok "Dependencies installed"

# Fail loudly if the venv is unusable (common after a bad migrate)
if ! python -c "import telegram" 2>/dev/null; then
    error "python-telegram-bot is missing from .venv — install failed."
    echo "  Try:  rm -rf .venv && ./setup.sh --deps-only"
    exit 1
fi
ok "Import check passed (telegram module found)"

# --- Non-interactive: only install deps (for server migrations) ---
if [[ "${1:-}" == "--deps-only" ]]; then
    mkdir -p downloads data
    echo ""
    ok "Deps-only setup complete."
    echo "  Start:   ./run.sh"
    echo "  Service: ./deploy/install-service.sh"
    echo ""
    exit 0
fi

# --- Bot token & admin ---
echo ""
echo "Get your bot token from @BotFather on Telegram."
echo "Get your numeric user ID from @userinfobot (for admin access)."
echo ""

# Load existing .env values if present — do not wipe a restored .env unless prompted
EXISTING_ENV=""
[[ -f ".env" ]] && EXISTING_ENV=$(cat .env)

_get_env() { echo "$EXISTING_ENV" | grep "^$1=" | cut -d= -f2- || true; }

DEFAULT_TOKEN=$(_get_env BOT_TOKEN)
DEFAULT_ADMINS=$(_get_env ADMIN_IDS)
DEFAULT_API_ID=$(_get_env TELEGRAM_API_ID)
DEFAULT_API_HASH=$(_get_env TELEGRAM_API_HASH)
DEFAULT_PROXY=$(_get_env TELEGRAM_PROXY)

if [[ -n "$DEFAULT_TOKEN" && "$DEFAULT_TOKEN" != "your_bot_token_here" ]]; then
    read -rp "Bot token [keep current: ${DEFAULT_TOKEN:0:12}…]: " BOT_TOKEN
    BOT_TOKEN=${BOT_TOKEN:-$DEFAULT_TOKEN}
else
    while true; do
        read -rp "Enter your BotFather token: " BOT_TOKEN
        BOT_TOKEN=$(echo "$BOT_TOKEN" | tr -d '[:space:]')
        [[ -n "$BOT_TOKEN" ]] && break
        error "Token cannot be empty."
    done
fi

if [[ -n "$DEFAULT_ADMINS" ]]; then
    read -rp "Admin user ID(s), comma-separated [$DEFAULT_ADMINS]: " ADMIN_IDS
    ADMIN_IDS=${ADMIN_IDS:-$DEFAULT_ADMINS}
else
    while true; do
        read -rp "Admin Telegram user ID(s), comma-separated: " ADMIN_IDS
        ADMIN_IDS=$(echo "$ADMIN_IDS" | tr -d '[:space:]')
        [[ -n "$ADMIN_IDS" ]] && break
        error "At least one admin ID is required."
    done
fi

echo ""
echo "2 GB uploads (optional) — get API ID + API Hash from https://my.telegram.org/apps"
read -rp "Enable 2 GB upload mode? [y/N] " ENABLE_2GB
API_ID_LINE=""
API_HASH_LINE=""
if [[ "$ENABLE_2GB" =~ ^[Yy]$ ]]; then
    read -rp "API ID${DEFAULT_API_ID:+ [$DEFAULT_API_ID]}: " API_ID
    API_ID=${API_ID:-$DEFAULT_API_ID}
    read -rp "API Hash${DEFAULT_API_HASH:+ [hidden]}: " API_HASH
    API_HASH=${API_HASH:-$DEFAULT_API_HASH}
    [[ -n "$API_ID" ]] && API_ID_LINE="TELEGRAM_API_ID=${API_ID}"
    [[ -n "$API_HASH" ]] && API_HASH_LINE="TELEGRAM_API_HASH=${API_HASH}"
fi

echo ""
read -rp "Need a proxy to reach Telegram? [y/N] " USE_PROXY
PROXY_LINE=""
if [[ "$USE_PROXY" =~ ^[Yy]$ ]]; then
    read -rp "Proxy URL (e.g. socks5://127.0.0.1:1080)${DEFAULT_PROXY:+ [$DEFAULT_PROXY]}: " PROXY_URL
    PROXY_URL=${PROXY_URL:-$DEFAULT_PROXY}
    PROXY_URL=$(echo "$PROXY_URL" | tr -d '[:space:]')
    [[ -n "$PROXY_URL" ]] && PROXY_LINE="TELEGRAM_PROXY=${PROXY_URL}"
fi

cat > .env <<EOF
# Generated by setup.sh — do not commit this file
BOT_TOKEN=${BOT_TOKEN}
ADMIN_IDS=${ADMIN_IDS}
${API_ID_LINE}
${API_HASH_LINE}
${PROXY_LINE}
EOF

ok "Configuration saved to .env"

# Keep existing optional keys that interactive setup does not re-prompt for
if [[ -n "$EXISTING_ENV" ]]; then
    for key in QUALITY COOKIES_FILE YTDLP_PROXY INSTAGRAM_MIN_INTERVAL COMPRESS_TARGET_MB TELEGRAM_PROXY; do
        val=$(_get_env "$key")
        if [[ -n "$val" ]] && ! grep -q "^${key}=" .env 2>/dev/null; then
            echo "${key}=${val}" >> .env
        fi
    done
fi

# --- Downloads directory ---
mkdir -p downloads data

echo ""
echo "============================================"
echo -e "${GREEN}  Setup complete!${NC}"
echo "============================================"
echo ""
echo "Start the bot:"
echo "  ./run.sh"
echo ""
echo "Install as a systemd service:"
echo "  chmod +x deploy/install-service.sh && ./deploy/install-service.sh"
echo ""
echo "Deps only (migration / reinstall packages, keep .env):"
echo "  ./setup.sh --deps-only"
echo ""
echo "Update later:"
echo "  ./update.sh"
echo ""
echo "If the bot times out on startup, test Telegram connectivity:"
echo "  chmod +x check-telegram.sh && ./check-telegram.sh"
echo ""
echo "Admins: open the bot → /admin or ⚙️ Admin panel"
echo ""
echo "For groups, disable privacy mode in @BotFather:"
echo "  /setprivacy → select your bot → Disable"
echo ""
echo "Then add the bot to any group — members can paste"
echo "YouTube, Instagram, Spotify, or other links freely."
echo ""
