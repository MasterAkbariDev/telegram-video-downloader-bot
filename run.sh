#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -d ".venv" ]]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi

if [[ ! -f ".env" ]]; then
    echo ".env not found. Run ./setup.sh first."
    exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

exec python -m bot
