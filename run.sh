#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/4] Checking Python..."
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. Install Python 3.11+ (e.g. apt install python3 python3-pip)."
    exit 1
fi

echo "[2/4] Installing dependencies..."
python3 -m pip install --user -r requirements.txt

echo "[3/4] Checking .env..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example. Put your token into .env now."
fi

echo "[4/4] Starting bot..."
exec python3 main.py