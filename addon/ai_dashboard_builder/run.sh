#!/usr/bin/with-contenv bash
set -euo pipefail

mkdir -p /data/proposals

exec python3 /app/server.py
