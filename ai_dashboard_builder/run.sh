#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -euo pipefail

bashio::log.info "Starting HA Analyst – Dashboard & Integration Recommender"

# Validate required options
if ! bashio::config.has_value 'anthropic_api_key'; then
    bashio::log.warning "anthropic_api_key is not set – analysis will be unavailable until configured."
fi

mkdir -p /data/proposals

bashio::log.info "Launching web server on port 8099…"
exec python3 /app/server.py
