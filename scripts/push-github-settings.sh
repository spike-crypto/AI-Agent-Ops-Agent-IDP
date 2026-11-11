#!/usr/bin/env bash
# Copies the deploy settings from .env into the GitHub repo (needs `gh auth login`).
#   bash scripts/push-github-settings.sh owner/repo
set -euo pipefail
REPO="${1:?usage: $0 owner/repo}"
cd "$(dirname "$0")/.."
[ -f .env ] || { echo ".env not found"; exit 1; }

get() { grep -E "^$1=" .env | head -n1 | cut -d= -f2- | tr -d '\r'; }

HOOK="$(get RENDER_DEPLOY_HOOK_URL)"
URL="$(get APP_URL)"
URL="${URL%/}"  # no trailing slash

[ -n "$HOOK" ] || { echo "RENDER_DEPLOY_HOOK_URL is empty in .env"; exit 1; }
[ -n "$URL" ] || { echo "APP_URL is empty in .env"; exit 1; }
case "$URL" in https://*) ;; *) echo "APP_URL must start with https://"; exit 1;; esac

printf '%s' "$HOOK" | gh secret set RENDER_DEPLOY_HOOK_URL --repo "$REPO"
gh variable set APP_URL --repo "$REPO" --body "$URL"
echo "Done: secret RENDER_DEPLOY_HOOK_URL and variable APP_URL are set on $REPO"
