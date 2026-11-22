#!/usr/bin/env bash
# Run the QuantDinger API backend natively (no Docker) in PAPER-ONLY mode.
#
#   scripts/quantdinger_local.sh start        # init + start Postgres, Redis, API on :5055
#   scripts/quantdinger_local.sh issue-token  # log in as the local admin, issue a paper-only T+R agent token
#                                             # (QD_MAX_ORDER_NOTIONAL / QD_MAX_DAILY_NOTIONAL override the
#                                             #  gateway-side caps; aipt's risk engine stays the primary limit)
#   scripts/quantdinger_local.sh status|stop|logs
#
# Live trading is disabled three ways: LIVE_TRADING_ENABLED=false,
# AGENT_LIVE_TRADING_ENABLED=false, and the agent token is issued with paper_only=true.
# Secrets are generated locally into var/quantdinger/qd.env (mode 600) and never committed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QD_BACKEND="$ROOT/vendor/QuantDinger/backend_api_python"
VAR="$ROOT/var/quantdinger"
PGDATA="$VAR/pgdata"
PG_BIN="${PG_BIN:-/opt/homebrew/opt/postgresql@17/bin}"
REDIS_BIN="${REDIS_BIN:-/opt/homebrew/bin}"
PG_PORT="${QD_PG_PORT:-55432}"
REDIS_PORT="${QD_REDIS_PORT:-56379}"
API_PORT="${QD_API_PORT:-5055}"
ENV_FILE="$VAR/qd.env"
PY="$QD_BACKEND/.venv-qd/bin/python"

mkdir -p "$VAR/logs"

rand() { "$PY" -c "import secrets; print(secrets.token_urlsafe($1))"; }

write_env() {
  [[ -f "$ENV_FILE" ]] && return
  umask 077
  local pg_pass admin_pass
  pg_pass="$(rand 24)"; admin_pass="$(rand 18)"
  cat > "$ENV_FILE" <<EOF
SECRET_KEY=$(rand 48)
CREDENTIAL_ENCRYPTION_KEY=$("$PY" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
QD_PG_PASSWORD=$pg_pass
DATABASE_URL=postgresql://quantdinger:$pg_pass@127.0.0.1:$PG_PORT/quantdinger
DB_TYPE=postgresql
ADMIN_USER=quantdinger
ADMIN_PASSWORD=$admin_pass
ADMIN_EMAIL=admin@localhost.invalid
REDIS_HOST=127.0.0.1
REDIS_PORT=$REDIS_PORT
CACHE_ENABLED=true
CELERY_TASKS_ENABLED=false
KAFKA_EVENT_PUBLISH_ENABLED=false
LIVE_TRADING_ENABLED=false
AGENT_LIVE_TRADING_ENABLED=false
ENABLE_REGISTRATION=false
QD_PROCESS_ROLE=legacy
PYTHON_API_PORT=$API_PORT
PYTHON_API_HOST=127.0.0.1
TZ=America/New_York
EOF
  chmod 600 "$ENV_FILE"
  echo "generated $ENV_FILE"
}

load_env() { set -a; # shellcheck disable=SC1090
  source "$ENV_FILE"; set +a; }

start_pg() {
  if [[ ! -d "$PGDATA" ]]; then
    local pwfile; pwfile="$(mktemp)"; echo "$QD_PG_PASSWORD" > "$pwfile"
    "$PG_BIN/initdb" -D "$PGDATA" -U quantdinger --pwfile="$pwfile" -A scram-sha-256 >/dev/null
    rm -f "$pwfile"
    fresh=1
  fi
  if ! "$PG_BIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
    "$PG_BIN/pg_ctl" -D "$PGDATA" -l "$VAR/logs/postgres.log" -o "-p $PG_PORT -c unix_socket_directories='' -c listen_addresses=127.0.0.1" start >/dev/null
    sleep 2
  fi
  if [[ "${fresh:-0}" == 1 ]]; then
    PGPASSWORD="$QD_PG_PASSWORD" "$PG_BIN/createdb" -h 127.0.0.1 -p "$PG_PORT" -U quantdinger quantdinger
    PGPASSWORD="$QD_PG_PASSWORD" "$PG_BIN/psql" -q -h 127.0.0.1 -p "$PG_PORT" -U quantdinger -d quantdinger \
      -f "$QD_BACKEND/migrations/init.sql" >/dev/null
    echo "postgres initialised"
  fi
}

start_redis() {
  if ! "$REDIS_BIN/redis-cli" -p "$REDIS_PORT" ping >/dev/null 2>&1; then
    "$REDIS_BIN/redis-server" --port "$REDIS_PORT" --bind 127.0.0.1 --daemonize yes \
      --dir "$VAR" --logfile "$VAR/logs/redis.log" --save "" >/dev/null
  fi
}

start_api() {
  if curl -fs "http://127.0.0.1:$API_PORT/api/agent/v1/health" >/dev/null 2>&1; then
    echo "api already running on :$API_PORT"; return
  fi
  # Own session so the API survives the shell that started it.
  QUANTDINGER_ENV_FILE="$ENV_FILE" "$PY" - "$QD_BACKEND" "$VAR" <<'PYEOF'
import os, subprocess, sys
backend, var = sys.argv[1], sys.argv[2]
log = open(os.path.join(var, "logs", "api.log"), "ab")
proc = subprocess.Popen([sys.executable, "run.py"], cwd=backend, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=log, start_new_session=True)
open(os.path.join(var, "api.pid"), "w").write(str(proc.pid))
PYEOF
  for _ in $(seq 1 60); do
    curl -fs "http://127.0.0.1:$API_PORT/api/agent/v1/health" >/dev/null 2>&1 && { echo "api up on :$API_PORT"; return; }
    sleep 1
  done
  echo "api did not come up; see $VAR/logs/api.log" >&2; exit 1
}

issue_token() {
  local jwt
  jwt="$(curl -fs -X POST "http://127.0.0.1:$API_PORT/api/auth/login" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASSWORD\"}" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["data"]["token"])')"
  curl -fs -X POST "http://127.0.0.1:$API_PORT/api/agent/v1/me/tokens" \
    -H "Authorization: Bearer $jwt" -H 'Content-Type: application/json' \
    -d "{\"name\":\"aipt-paper\",\"scopes\":\"R,T\",\"markets\":\"USStock\",\"paper_only\":true,\"rate_limit_per_min\":120,\"max_order_notional\":${QD_MAX_ORDER_NOTIONAL:-15000},\"max_daily_notional\":${QD_MAX_DAILY_NOTIONAL:-150000}}" \
    | "$PY" -c 'import json,sys; d=json.load(sys.stdin)["data"]; assert d["paper_only"] is True; print(d["token"])'
}

case "${1:-}" in
  start) write_env; load_env; start_pg; start_redis; start_api ;;
  issue-token) load_env; issue_token ;;
  status)
    load_env
    "$PG_BIN/pg_ctl" -D "$PGDATA" status | head -1 || true
    "$REDIS_BIN/redis-cli" -p "$REDIS_PORT" ping || true
    curl -s "http://127.0.0.1:$API_PORT/api/agent/v1/health"; echo ;;
  stop)
    [[ -f "$VAR/api.pid" ]] && kill "$(cat "$VAR/api.pid")" 2>/dev/null || true
    "$REDIS_BIN/redis-cli" -p "$REDIS_PORT" shutdown nosave 2>/dev/null || true
    "$PG_BIN/pg_ctl" -D "$PGDATA" stop >/dev/null 2>&1 || true
    echo stopped ;;
  logs) tail -n 80 "$VAR/logs/api.log" ;;
  *) echo "usage: $0 start|issue-token|status|stop|logs" >&2; exit 2 ;;
esac
