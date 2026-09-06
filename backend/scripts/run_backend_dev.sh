#!/bin/sh
set -eu

test -f /app/.desktop-auth/launch-auth.token || {
  echo 'BreakTwenty launch token is missing. Start through scripts/start_desktop.js or rotate it with scripts/launch_auth_token.js before direct Compose use.' >&2
  exit 78
}

install -d -m 700 /tmp/breaktwenty-launch-auth
cp /app/.desktop-auth/launch-auth.token /tmp/breaktwenty-launch-auth/launch-auth.token
chmod 600 /tmp/breaktwenty-launch-auth/launch-auth.token

python3 -m app.migration_safety upgrade \
  --database /app/data/breaktwenty.db \
  --alembic-ini /app/alembic.ini

graceful_timeout="${BREAKTWENTY_BACKEND_GRACEFUL_TIMEOUT_SECONDS:-2}"
reload_mode="$(printf '%s' "${BREAKTWENTY_BACKEND_RELOAD:-0}" | tr '[:upper:]' '[:lower:]')"

case "$reload_mode" in
  1|true|yes|on)
    exec uvicorn app.main:app \
      --host 127.0.0.1 \
      --port 8000 \
      --reload \
      --reload-dir /app/app \
      --reload-dir /app/alembic \
      --reload-exclude '/app/tests/*' \
      --reload-exclude '/app/**/__pycache__/*' \
      --reload-exclude '*.pyc' \
      --no-access-log \
      --timeout-graceful-shutdown "$graceful_timeout"
    ;;
  *)
    exec uvicorn app.main:app \
      --host 127.0.0.1 \
      --port 8000 \
      --no-access-log \
      --timeout-graceful-shutdown "$graceful_timeout"
    ;;
esac
