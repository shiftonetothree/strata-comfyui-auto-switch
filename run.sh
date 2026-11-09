#!/usr/bin/env bash
# Control script for the auto-switch relay and the ComfyUI entry proxy.
#
#   ./run.sh start            start the relay, then run_comfyui_proxy.sh (idempotent)
#   ./run.sh autostart        start relay in idle mode + proxy (for cron @reboot)
#   ./run.sh stop             stop both (managed containers keep running)
#   ./run.sh restart
#   ./run.sh status           live relay JSON (proxy status on stderr)
#   ./run.sh switch <name> [--force]
#                             activate a service now; --force skips the cooldown
#   ./run.sh logs [relay|proxy]
#                             follow logs/relay.log or logs/comfyui-proxy.out
#   ./run.sh proxy <start|stop|restart|status|logs>
#                             control only the ComfyUI entry proxy (port 8189)
#   ./run.sh run              run the relay in the foreground (Ctrl-C to stop)
#   ./run.sh check            validate config.toml
#
# Environment:
#   RELAY_CONFIG=config.toml  alternate config file
#   PYTHON_BIN=python3        alternate interpreter (also passed to the proxy)
set -euo pipefail
cd "$(dirname -- "$0")"

CONFIG="${RELAY_CONFIG:-config.toml}"
PY="${PYTHON_BIN:-python3}"
PID_FILE="run/relay.pid"
LOG_FILE="logs/relay.log"
OUT_FILE="logs/relay.out"
PROXY_PID_FILE="run/comfyui-proxy.pid"
PROXY_LOG="logs/comfyui-proxy.out"
SYSTEMD_RELAY_UNIT="auto-switch-relay.service"
SYSTEMD_PROXY_UNIT="auto-switch-proxy.service"

admin_url() {
  "$PY" - "$CONFIG" <<'PYEOF'
import sys, tomllib
relay = tomllib.load(open(sys.argv[1], "rb"))["relay"]
host = relay.get("admin_host", "127.0.0.1")
if host in ("0.0.0.0", "::", ""):
    host = "127.0.0.1"
print(f"http://{host}:{relay.get('admin_port', 9099)}")
PYEOF
}

proxy_config() {
  "$PY" - "$CONFIG" <<'PYEOF'
import sys, tomllib
cfg = tomllib.load(open(sys.argv[1], "rb"))
proxy = cfg.get("proxy", {})
services = cfg.get("services", {})
relay = cfg.get("relay", {})
comfy = services.get("comfyui", {})
enabled = proxy.get("enabled", "comfyui" in services)
command = proxy.get("command", "../comfyui-docker/run_comfyui_proxy.sh")
port = proxy.get("listen_port", 8189)
upstream = proxy.get("upstream")
if not upstream:
    host = comfy.get("bind_host") or relay.get("bind_host", "127.0.0.1")
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    upstream = f"http://{host}:{comfy.get('listen_port', 8188)}"
print("true" if enabled else "false")
print(command)
print(port)
print(upstream)
PYEOF
}

mapfile -t PROXY_CONF < <(proxy_config)
PROXY_ENABLED="${PROXY_CONF[0]:-false}"
PROXY_COMMAND="${PROXY_CONF[1]:-}"
PROXY_PORT="${PROXY_CONF[2]:-8189}"
PROXY_UPSTREAM="${PROXY_CONF[3]:-}"

read_pid() {
  local file="$1" pid
  [[ -f "$file" ]] || return 1
  pid="$(cat "$file" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  echo "$pid"
}

is_running() {
  local pid
  pid="$(read_pid "$1")" || return 1
  kill -0 "$pid" 2>/dev/null
}

wait_for_pid_exit() {
  local pid=$1 tries=$2
  local i
  for ((i = 0; i < tries; i++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

listen_in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    if ss -ltnH "sport = :$port" 2>/dev/null | grep -q .; then
      return 0
    fi
    return 1
  fi
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    return 0
  fi
  return 1
}

# --------------------------------------------------------------- systemd

systemd_active() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl is-active --quiet "$1" 2>/dev/null
}

systemd_hint() {
  echo "this service is managed by systemd; use:" >&2
  echo "  sudo systemctl start|stop|restart auto-switch-relay auto-switch-proxy" >&2
  echo "  systemctl status auto-switch-relay auto-switch-proxy" >&2
}

# --------------------------------------------------------------- relay process

cmd_start_relay() {
  if is_running "$PID_FILE"; then
    echo "relay is already running (pid $(read_pid "$PID_FILE"))"
    return 0
  fi
  mkdir -p run logs
  rm -f "$PID_FILE"
  "$PY" relay.py --config "$CONFIG" --check >/dev/null
  nohup "$PY" -u relay.py --config "$CONFIG" "$@" >>"$OUT_FILE" 2>&1 &
  local pid=$!
  echo "$pid" >"$PID_FILE"

  local url
  url="$(admin_url)"
  local i
  for ((i = 0; i < 100; i++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "relay exited during startup. Last output:" >&2
      tail -n 40 "$OUT_FILE" >&2 || true
      rm -f "$PID_FILE"
      return 1
    fi
    if curl -fsS -m 1 "$url/health" >/dev/null 2>&1; then
      echo "relay started (pid $pid); admin: $url/status"
      return 0
    fi
    sleep 0.2
  done
  echo "relay is running (pid $pid) but the admin API has not answered yet." >&2
  echo "watch startup with: ./run.sh logs" >&2
}

cmd_stop_relay() {
  if ! is_running "$PID_FILE"; then
    echo "relay is not running"
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid="$(read_pid "$PID_FILE")"
  kill -TERM "$pid" 2>/dev/null || true
  if wait_for_pid_exit "$pid" 600; then
    echo "relay stopped (pid $pid)"
  else
    echo "relay did not stop in time; sending SIGKILL" >&2
    kill -KILL "$pid" 2>/dev/null || true
    wait_for_pid_exit "$pid" 40 || true
    echo "relay killed (pid $pid)"
  fi
  rm -f "$PID_FILE"
}

# --------------------------------------------------------------- entry proxy

proxy_running() {
  is_running "$PROXY_PID_FILE"
}

proxy_start() {
  if systemd_active "$SYSTEMD_PROXY_UNIT"; then
    echo "ComfyUI entry proxy is running under systemd ($SYSTEMD_PROXY_UNIT)"
    return 0
  fi
  if [[ "$PROXY_ENABLED" != "true" ]]; then
    echo "ComfyUI entry proxy is disabled in config.toml ([proxy] enabled = false)"
    return 0
  fi
  if proxy_running; then
    echo "ComfyUI entry proxy already running (pid $(read_pid "$PROXY_PID_FILE"), port $PROXY_PORT)"
    return 0
  fi
  rm -f "$PROXY_PID_FILE"
  if listen_in_use "$PROXY_PORT"; then
    echo "port $PROXY_PORT is already in use; stop the old run_comfyui_proxy.sh first:" >&2
    ss -ltnp "sport = :$PROXY_PORT" 2>/dev/null | tail -n +2 >&2 || true
    return 1
  fi
  mkdir -p run logs
  echo "starting ComfyUI entry proxy on 127.0.0.1:$PROXY_PORT -> $PROXY_UPSTREAM"

  # setsid puts the script and mitmdump in a new session/process group, so the
  # whole proxy can be stopped reliably later.
  setsid bash "$PROXY_COMMAND" "$PROXY_UPSTREAM" "$PROXY_PORT" \
    >>"$PROXY_LOG" 2>&1 < /dev/null &
  local pid=$!
  echo "$pid" >"$PROXY_PID_FILE"

  local i
  for ((i = 0; i < 150; i++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "ComfyUI entry proxy exited during startup. Last output:" >&2
      tail -n 40 "$PROXY_LOG" >&2 || true
      rm -f "$PROXY_PID_FILE"
      return 1
    fi
    if listen_in_use "$PROXY_PORT"; then
      echo "ComfyUI entry proxy started (pid $pid); entry: http://127.0.0.1:$PROXY_PORT"
      return 0
    fi
    sleep 0.2
  done
  echo "ComfyUI entry proxy did not open port $PROXY_PORT. Last output:" >&2
  tail -n 40 "$PROXY_LOG" >&2 || true
  proxy_stop
  return 1
}

proxy_stop() {
  if systemd_active "$SYSTEMD_PROXY_UNIT"; then
    systemd_hint
    return 1
  fi
  local pid
  pid="$(read_pid "$PROXY_PID_FILE")" || return 0
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PROXY_PID_FILE"
    return 0
  fi
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  if wait_for_pid_exit "$pid" 200; then
    echo "ComfyUI entry proxy stopped (pid $pid)"
  else
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    wait_for_pid_exit "$pid" 40 || true
    echo "ComfyUI entry proxy killed (pid $pid)"
  fi
  rm -f "$PROXY_PID_FILE"
}

proxy_status() {
  if [[ "$PROXY_ENABLED" != "true" ]]; then
    echo "proxy:   disabled (config.toml [proxy] enabled = false)"
    return 0
  fi
  if proxy_running; then
    echo "proxy:   running (pid $(read_pid "$PROXY_PID_FILE")), entry http://127.0.0.1:$PROXY_PORT -> $PROXY_UPSTREAM"
  elif listen_in_use "$PROXY_PORT"; then
    echo "proxy:   running (systemd or external process), entry http://127.0.0.1:$PROXY_PORT -> $PROXY_UPSTREAM"
  else
    echo "proxy:   NOT running (entry http://127.0.0.1:$PROXY_PORT)"
  fi
}

# ------------------------------------------------------------------ commands

cmd_start() {
  if systemd_active "$SYSTEMD_RELAY_UNIT" || systemd_active "$SYSTEMD_PROXY_UNIT"; then
    systemd_hint
    return 0
  fi
  local relay_was_started=0
  if ! is_running "$PID_FILE"; then
    cmd_start_relay || return 1
    relay_was_started=1
  else
    echo "relay is already running (pid $(read_pid "$PID_FILE"))"
  fi

  if ! proxy_start; then
    if [[ "$relay_was_started" -eq 1 ]]; then
      echo "stopping the relay again because the ComfyUI entry proxy failed to start" >&2
      cmd_stop_relay
    fi
    return 1
  fi
}

cmd_autostart() {
  if systemd_active "$SYSTEMD_RELAY_UNIT" || systemd_active "$SYSTEMD_PROXY_UNIT"; then
    systemd_hint
    return 0
  fi
  local relay_was_started=0
  if ! is_running "$PID_FILE"; then
    cmd_start_relay --startup-mode idle || return 1
    relay_was_started=1
  else
    echo "relay is already running; autostart leaves its mode unchanged"
  fi
  if ! proxy_start; then
    if [[ "$relay_was_started" -eq 1 ]]; then
      echo "stopping the relay again because the ComfyUI entry proxy failed to start" >&2
      cmd_stop_relay
    fi
    return 1
  fi
}

cmd_stop() {
  if systemd_active "$SYSTEMD_RELAY_UNIT" || systemd_active "$SYSTEMD_PROXY_UNIT"; then
    systemd_hint
    return 1
  fi
  proxy_stop
  cmd_stop_relay
}

cmd_restart() {
  if systemd_active "$SYSTEMD_RELAY_UNIT" || systemd_active "$SYSTEMD_PROXY_UNIT"; then
    systemd_hint
    return 1
  fi
  cmd_stop
  cmd_start
}

cmd_status() {
  local url
  url="$(admin_url)"
  if ! curl -fsS -m 10 "$url/status" | "$PY" -c 'import json,sys; print(json.dumps(json.load(sys.stdin), indent=2, ensure_ascii=False))'; then
    if systemd_active "$SYSTEMD_RELAY_UNIT"; then
      echo "relay unit is active but the admin API at $url did not answer; check journalctl -u $SYSTEMD_RELAY_UNIT" >&2
    else
      echo "relay is not running" >&2
    fi
    tail -n 20 "$LOG_FILE" >&2 || true
    proxy_status >&2
    return 1
  fi
  proxy_status >&2
}

cmd_switch() {
  local target="${1:-}"
  if [[ -z "$target" ]]; then
    echo "usage: $0 switch <comfyui|strata> [--force]" >&2
    return 2
  fi
  shift || true
  local force=0
  if [[ "${1:-}" == "--force" ]]; then
    force=1
  fi
  local url
  url="$(admin_url)"
  local body status
  body="$(mktemp)"
  status="$(curl -sS -m 1200 -o "$body" -w '%{http_code}' -X POST "$url/switch/$target?force=$force" || true)"
  "$PY" - "$body" <<'PYEOF'
import json, sys
raw = open(sys.argv[1], encoding="utf-8").read()
try:
    print(json.dumps(json.loads(raw), indent=2, ensure_ascii=False))
except ValueError:
    print(raw)
PYEOF
  rm -f "$body"
  echo "HTTP $status"
  [[ "$status" == "200" ]]
}

cmd_logs() {
  local what="${1:-relay}"
  mkdir -p logs
  case "$what" in
    relay|"")
      touch "$LOG_FILE"
      tail -n 100 -F "$LOG_FILE"
      ;;
    proxy|comfyui)
      proxy_logs
      ;;
    *)
      echo "usage: $0 logs [relay|proxy]" >&2
      return 2
      ;;
  esac
}

proxy_logs() {
  if systemd_active "$SYSTEMD_PROXY_UNIT"; then
    exec journalctl -u "$SYSTEMD_PROXY_UNIT" -n 100 -f
  fi
  mkdir -p logs
  touch "$PROXY_LOG"
  tail -n 100 -F "$PROXY_LOG"
}

cmd_proxy() {
  case "${1:-status}" in
    start) proxy_start ;;
    stop) proxy_stop ;;
    restart) proxy_stop; proxy_start ;;
    status) proxy_status ;;
    logs) proxy_logs ;;
    *)
      echo "usage: $0 proxy [start|stop|restart|status|logs]" >&2
      return 2
      ;;
  esac
}

cmd_run() {
  mkdir -p run logs
  RELAY_CONSOLE=1 exec "$PY" -u relay.py --config "$CONFIG"
}

cmd_check() {
  "$PY" relay.py --config "$CONFIG" --check
}

case "${1:-}" in
  start) cmd_start ;;
  autostart) cmd_autostart ;;
  stop) cmd_stop ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  switch) shift; cmd_switch "$@" ;;
  logs) shift; cmd_logs "$@" ;;
  proxy) shift; cmd_proxy "$@" ;;
  run|foreground) cmd_run ;;
  check) cmd_check ;;
  ""|-h|--help|help)
    sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    echo "unknown command: $1" >&2
    sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
    ;;
esac
