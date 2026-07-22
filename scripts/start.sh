#!/bin/sh
set -eu

mkdir -p /app/data /app/napcat-config
chown -R bot:bot /app/data

if [ -n "${NAPCAT_CONFIG_OUTPUT:-}" ]; then
    qq-ai-bot-cli render-napcat-config --output "$NAPCAT_CONFIG_OUTPUT"
fi

setpriv --reuid=10001 --regid=10001 --init-groups qq-ai-bot-cli init-db
exec setpriv --reuid=10001 --regid=10001 --init-groups qq-ai-bot
