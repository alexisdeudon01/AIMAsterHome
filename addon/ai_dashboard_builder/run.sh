#!/usr/bin/with-contenv bashio
# shellcheck shell=bash

echo "[AI Dashboard Builder] Starting..."

export ADDON_OPTIONS=/data/options.json

python3 /app/dashboard_builder.py

echo "[AI Dashboard Builder] Done."
