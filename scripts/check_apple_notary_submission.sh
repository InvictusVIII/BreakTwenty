#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "error: $*" >&2
  exit 1
}

submission_id="${1:?Usage: scripts/check_apple_notary_submission.sh <submission-id>}"

if [ "$(uname -s)" != "Darwin" ]; then
  fail "Apple notary submission checks require macOS notarytool."
fi

command -v xcrun >/dev/null || fail "Xcode command line tools are required."

if [ -n "${APPLE_API_KEY_PATH:-}" ]; then
  export APPLE_API_KEY="$APPLE_API_KEY_PATH"
fi
: "${APPLE_API_KEY:?APPLE_API_KEY must point to AuthKey_${APPLE_API_KEY_ID:-...}.p8.}"
: "${APPLE_API_KEY_ID:?APPLE_API_KEY_ID is required.}"
: "${APPLE_API_ISSUER:?APPLE_API_ISSUER is required.}"
[ -f "$APPLE_API_KEY" ] || fail "APPLE_API_KEY must be a file path to AuthKey_${APPLE_API_KEY_ID}.p8: ${APPLE_API_KEY}"

info="$(xcrun notarytool info "$submission_id" \
  --key "$APPLE_API_KEY" \
  --key-id "$APPLE_API_KEY_ID" \
  --issuer "$APPLE_API_ISSUER" \
  --output-format json)"
printf '%s\n' "$info"

status="$(printf '%s\n' "$info" | node -e "let data=''; process.stdin.on('data', c => data += c); process.stdin.on('end', () => console.log((JSON.parse(data).status || '').trim()));")"
case "$status" in
  Accepted|Invalid|Rejected)
    xcrun notarytool log "$submission_id" \
      --key "$APPLE_API_KEY" \
      --key-id "$APPLE_API_KEY_ID" \
      --issuer "$APPLE_API_ISSUER" \
      || true
    ;;
esac
