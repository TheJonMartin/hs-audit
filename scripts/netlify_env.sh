#!/usr/bin/env bash
# Push the receiver's environment variables from .env.receiver to Netlify.
# Secrets are scoped to the Production context so deploy previews cannot use them.
# Usage: scripts/netlify_env.sh            (needs the Netlify CLI, logged in and linked to the site)
set -euo pipefail

ENV_FILE="${1:-.env.receiver}"
REQUIRED=(OAUTH_REDIRECT_URI STATE_SIGNING_SECRET TOKEN_FETCH_SECRET TOKEN_PUBLIC_KEY_PEM \
          TOKEN_STORE_NAME HUBSPOT_CLIENT_ID HUBSPOT_CLIENT_SECRET ERROR_ROUTER_URL)
OPTIONAL=(FLOW_ID FLOW_NAME PLATFORM_NAME)
SECRET_VARS=(STATE_SIGNING_SECRET TOKEN_FETCH_SECRET TOKEN_PUBLIC_KEY_PEM HUBSPOT_CLIENT_SECRET)

command -v netlify >/dev/null || { echo "Netlify CLI not found: npm install -g netlify-cli" >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "$ENV_FILE not found. Run: hubspot-audit setup --site-url https://<site>.netlify.app" >&2; exit 1; }

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

missing=()
for name in "${REQUIRED[@]}"; do
  [ -n "${!name:-}" ] || missing+=("$name")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "Blank in $ENV_FILE, fill these in first: ${missing[*]}" >&2
  exit 1
fi

for name in "${REQUIRED[@]}" "${OPTIONAL[@]}"; do
  value="${!name:-}"
  [ -n "$value" ] || continue
  flags=(--context production)
  for secret in "${SECRET_VARS[@]}"; do
    [ "$secret" = "$name" ] && flags+=(--secret)
  done
  netlify env:set "$name" "$value" "${flags[@]}" >/dev/null
  echo "set $name (production)"
done
echo "Done. Deploy with: netlify deploy --prod"
