#!/usr/bin/env bash
# Install or reinstall the systemd service for this bot.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="telegram-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
RUN_USER="$(whoami)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
    error "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    error ".env not found. Run ./setup.sh first."
    exit 1
fi

if ! id "$RUN_USER" &>/dev/null; then
    error "User '$RUN_USER' does not exist on this system."
    exit 1
fi

if [[ "$(id -u)" -ne 0 ]]; then
    SUDO="sudo"
else
    SUDO=""
fi

info "Install directory: $INSTALL_DIR"
info "Run as user:       $RUN_USER"

if [[ "$RUN_USER" == "root" ]]; then
    warn "Running as root works, but a dedicated user is safer for production."
fi

$SUDO tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Telegram Video Downloader Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/.venv/bin/python -m bot
Restart=on-failure
RestartSec=10
EnvironmentFile=${INSTALL_DIR}/.env

[Install]
WantedBy=multi-user.target
EOF

ok "Service file written to $SERVICE_FILE"

$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE_NAME"
$SUDO systemctl restart "$SERVICE_NAME"

ok "Service enabled and started"

echo ""
echo "Useful commands:"
echo "  Status:  sudo systemctl status $SERVICE_NAME"
echo "  Logs:    sudo journalctl -u $SERVICE_NAME -f"
echo "  Restart: sudo systemctl restart $SERVICE_NAME"
echo "  Stop:    sudo systemctl stop $SERVICE_NAME"
echo ""
