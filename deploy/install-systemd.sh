#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${SOURCE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
APP_DIR="${APP_DIR:-/opt/epl-tipping}"
SERVICE_NAME="${SERVICE_NAME:-epl-tipping}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn "$SERVICE_USER")}"
DATA_DIR="${DATA_DIR:-/var/lib/epl-tipping/data}"
ENV_FILE="${ENV_FILE:-/etc/epl-tipping.env}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
START_NOW="${START_NOW:-0}"

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

run_service_user() {
  if [ "$(id -un)" = "$SERVICE_USER" ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -u "$SERVICE_USER" "$@"
  else
    run_root runuser -u "$SERVICE_USER" -- "$@"
  fi
}

write_root_file() {
  local mode="$1"
  local path="$2"
  local tmp
  tmp="$(mktemp)"
  cat > "$tmp"
  run_root install -m "$mode" "$tmp" "$path"
  rm -f "$tmp"
}

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl is required for this installer." >&2
  exit 1
fi

UV_BIN="$(command -v uv || true)"
if [ -z "$UV_BIN" ]; then
  echo "uv is required. Install uv, then rerun this script." >&2
  exit 1
fi

if [ "$APP_DIR" != "$SOURCE_DIR" ]; then
  run_root install -d -m 755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$APP_DIR"
  if command -v rsync >/dev/null 2>&1; then
    run_root rsync -a --delete \
      --exclude .venv \
      --exclude .env \
      --exclude '.env.*' \
      --exclude .pytest_cache \
      --exclude __pycache__ \
      --exclude '*.pyc' \
      "$SOURCE_DIR/" "$APP_DIR/"
  else
    tmp_archive="$(mktemp)"
    tar \
      --exclude ./.venv \
      --exclude ./.env \
      --exclude './.env.*' \
      --exclude ./.pytest_cache \
      --exclude '*/__pycache__' \
      --exclude '*.pyc' \
      -C "$SOURCE_DIR" -cf "$tmp_archive" .
    run_root tar -C "$APP_DIR" -xf "$tmp_archive"
    rm -f "$tmp_archive"
  fi
  run_root chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR"
  run_root restorecon -RF "$APP_DIR" 2>/dev/null || true
fi

cd "$APP_DIR"
run_service_user "$UV_BIN" sync --frozen
run_root restorecon -RF "$APP_DIR" 2>/dev/null || true

PYTHON_BIN="$APP_DIR/.venv/bin/python"
UVICORN_BIN="$APP_DIR/.venv/bin/uvicorn"

if [ ! -x "$PYTHON_BIN" ] || [ ! -x "$UVICORN_BIN" ]; then
  echo "The virtualenv is missing expected executables under $APP_DIR/.venv." >&2
  exit 1
fi

run_root install -d -m 750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$DATA_DIR"

for filename in fixtures.json registry.json predictions.json scores.json run_log.json source_state.json season_projections.json projection_runs.json; do
  if [ -f "$APP_DIR/data/$filename" ] && [ ! -f "$DATA_DIR/$filename" ]; then
    run_root install -m 600 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$APP_DIR/data/$filename" "$DATA_DIR/$filename"
  fi
done

if [ ! -f "$ENV_FILE" ]; then
  admin_token="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  cookie_secret="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  write_root_file 600 "$ENV_FILE" <<EOF
ADMIN_TOKEN=$admin_token
ADMIN_COOKIE_SECRET=$cookie_secret
ADMIN_COOKIE_SECURE=true
ADMIN_COOKIE_TTL_SECONDS=86400
FOOTBALL_DATA_TOKEN=replace-with-your-server-only-api-token
FOOTBALL_DATA_BASE_URL=https://api.football-data.org/v4
TIPPING_COMPETITION_CODE=PL
TIPPING_SEASON=2026
TIPPING_DATA_DIR=$DATA_DIR
TIPPING_DISPLAY_TIMEZONE=Australia/Sydney
TIPPING_ALLOWED_HOSTS=
TIPPING_ENABLE_HSTS=true
EOF
  echo "Created $ENV_FILE with generated secrets."
else
  echo "Keeping existing $ENV_FILE."
fi

write_root_file 644 "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=EPL Tipping FastAPI app
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$UVICORN_BIN epl_tipping.main:app --host $HOST --port $PORT --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$DATA_DIR
CapabilityBoundingSet=
AmbientCapabilities=
LockPersonality=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
EOF

write_root_file 644 "/etc/systemd/system/$SERVICE_NAME-cron.service" <<EOF
[Unit]
Description=EPL Tipping due workflow
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$PYTHON_BIN -m epl_tipping.cron run-due --data-dir $DATA_DIR --lookahead-hours 24 --lock-minutes 30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$DATA_DIR
CapabilityBoundingSet=
AmbientCapabilities=
LockPersonality=true
RestrictSUIDSGID=true
EOF

write_root_file 644 "/etc/systemd/system/$SERVICE_NAME-cron.timer" <<EOF
[Unit]
Description=Run EPL Tipping due workflow every 4 hours

[Timer]
OnBootSec=2min
OnUnitActiveSec=4h
AccuracySec=30s
RandomizedDelaySec=20s
Persistent=true

[Install]
WantedBy=timers.target
EOF

write_root_file 644 "/etc/systemd/system/$SERVICE_NAME-projection.service" <<EOF
[Unit]
Description=EPL Tipping queued season simulation worker
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$PYTHON_BIN -m epl_tipping.cron process-simulation --data-dir $DATA_DIR --timeout-seconds 15 --retries 1 --concurrency 5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$DATA_DIR
CapabilityBoundingSet=
AmbientCapabilities=
LockPersonality=true
RestrictSUIDSGID=true
EOF

write_root_file 644 "/etc/systemd/system/$SERVICE_NAME-projection.timer" <<EOF
[Unit]
Description=Poll the EPL Tipping simulation queue

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=5s
Persistent=true

[Install]
WantedBy=timers.target
EOF

run_root systemctl daemon-reload

if [ "$START_NOW" = "1" ]; then
  run_root systemctl enable --now "$SERVICE_NAME.service" "$SERVICE_NAME-cron.timer" "$SERVICE_NAME-projection.timer"
  echo "Started $SERVICE_NAME.service, $SERVICE_NAME-cron.timer, and $SERVICE_NAME-projection.timer."
else
  echo "Installed systemd units without starting them."
  echo "Start later with:"
  echo "  sudo systemctl enable --now $SERVICE_NAME.service $SERVICE_NAME-cron.timer $SERVICE_NAME-projection.timer"
fi
