#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Machine-readable steps for the Telegram update UI (::step::status::message)
log_step() {
    local status="$1"
    local message="$2"
    printf '::step::%s::%s\n' "$status" "$message"
}

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

RESTART_DELAY="${RESTART_DELAY:-4}"
SERVICE="${SERVICE:-}"

# Auto-detect systemd unit (telegram-bot, or any *telegram*bot*.service in this folder)
_detect_service() {
    if [[ -n "$SERVICE" ]]; then
        echo "$SERVICE"
        return
    fi
    local name
    for name in telegram-bot telegram-video-downloader-bot; do
        if [[ -f "/etc/systemd/system/${name}.service" ]] \
            || [[ -f "/lib/systemd/system/${name}.service" ]] \
            || systemctl cat "${name}.service" &>/dev/null; then
            echo "$name"
            return
        fi
    done
    for unit in /etc/systemd/system/*telegram*bot*.service; do
        if [[ -f "$unit" ]]; then
            basename "$unit" .service
            return
        fi
    done
}

SERVICE="$(_detect_service)"

echo ""
echo "============================================"
echo "  Telegram Video Downloader Bot — Update"
echo "============================================"
echo ""

if [[ ! -d ".git" ]]; then
    log_step error "Not a git repository"
    error "Not a git repository. Clone from GitHub first."
    exit 1
fi

if [[ ! -d ".venv" ]]; then
    log_step error "Virtual environment not found"
    error "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi

# --- Git fetch + sync to remote (deploy-style; survives force-pushes) ---
log_step progress "Fetching from remote"
info "Fetching from remote…"
git fetch --quiet origin
log_step ok "Fetched latest refs"
ok "Fetched latest refs"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
BEFORE="$(git rev-parse --short HEAD)"
REMOTE_REF="origin/${BRANCH}"

if ! git rev-parse --verify --quiet "${REMOTE_REF}" >/dev/null; then
    log_step error "Remote branch ${REMOTE_REF} not found"
    error "Remote branch ${REMOTE_REF} not found after fetch."
    exit 1
fi

log_step progress "Syncing to ${REMOTE_REF}"
info "Syncing working tree to ${REMOTE_REF}…"
# Always match GitHub exactly — ff-only pull fails after history rewrites.
git reset --hard "${REMOTE_REF}"
AFTER="$(git rev-parse --short HEAD)"

if [[ "$BEFORE" == "$AFTER" ]]; then
    PULL_MSG="Already up to date ($BRANCH @ $AFTER)"
else
    PULL_MSG="Code synced ($BRANCH $BEFORE → $AFTER)"
fi
log_step ok "$PULL_MSG"
ok "$PULL_MSG"

# --- Update dependencies ---
# shellcheck disable=SC1091
source .venv/bin/activate

log_step progress "Updating Python dependencies"
info "Updating Python dependencies…"
pip install --upgrade pip -q
pip install -r requirements.txt -q
pip install -U "yt-dlp>=2024.12.23" -q
log_step ok "Dependencies updated"
ok "Dependencies updated"

# --- Deferred restart (lets the bot finish update UI before SIGTERM) ---
if command -v systemctl &>/dev/null; then
    if [[ -n "$SERVICE" ]] && systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
        log_step progress "Scheduling bot restart"
        info "Scheduling $SERVICE restart in ${RESTART_DELAY}s…"
        (
            sleep "$RESTART_DELAY"
            if [[ "$(id -u)" -eq 0 ]]; then
                systemctl restart "$SERVICE"
            else
                sudo systemctl restart "$SERVICE"
            fi
        ) >/dev/null 2>&1 &
        log_step ok "Restart scheduled"
        ok "Restart scheduled (${RESTART_DELAY}s)"
        echo ""
        echo "Check status:  sudo systemctl status $SERVICE"
        echo "View logs:     sudo journalctl -u $SERVICE -f"
    elif [[ -n "$SERVICE" ]]; then
        log_step progress "Starting systemd service"
        info "Service $SERVICE is installed but not running — starting it…"
        if [[ "$(id -u)" -eq 0 ]]; then
            systemctl start "$SERVICE"
        else
            sudo systemctl start "$SERVICE"
        fi
        log_step ok "Service started"
        ok "Started $SERVICE"
        echo ""
        echo "Check status:  sudo systemctl status $SERVICE"
    else
        log_step warn "No systemd service detected"
        warn "No systemd service detected."
        echo "  Install:  chmod +x deploy/install-service.sh && ./deploy/install-service.sh"
        echo "  Or run:   ./run.sh"
    fi
else
    log_step warn "systemctl not found"
    warn "systemctl not found — restart the bot manually: ./run.sh"
fi

echo ""
echo "============================================"
echo -e "${GREEN}  Update complete!${NC}"
echo "============================================"
log_step ok "Update complete"
echo ""
