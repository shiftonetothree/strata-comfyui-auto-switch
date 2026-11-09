#!/usr/bin/env bash
# Install and enable the auto-switch systemd services.
#
#   sudo ./install-systemd.sh
#
# At boot this starts only:
#   - auto-switch-relay.service  (relay in --startup-mode idle)
#   - auto-switch-proxy.service  (run_comfyui_proxy.sh on 8189)
#
# Neither ComfyUI nor Strata is started at boot: the first real request starts
# the requested service.  Health/status polling is ignored (see config.toml).
set -euo pipefail
cd "$(dirname -- "$0")"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "请用 sudo 运行：sudo $0" >&2
  exit 1
fi

PROJECT_DIR="$(pwd -P)"
RUN_USER="${SUDO_USER:-$(stat -c %U "$PROJECT_DIR")}"
if [[ -z "$RUN_USER" || "$RUN_USER" == "root" ]]; then
  echo "无法确定运行用户（SUDO_USER 为空且目录属于 root）" >&2
  exit 1
fi
HOME_DIR="$(getent passwd "$RUN_USER" | cut -d: -f6)"
if [[ -z "$HOME_DIR" ]]; then
  echo "找不到用户 $RUN_USER 的 home 目录" >&2
  exit 1
fi

COMFY_DIR="$(cd "$PROJECT_DIR/../comfyui-docker" && pwd -P)"
PROXY_SCRIPT="$COMFY_DIR/run_comfyui_proxy.sh"
if [[ ! -f "$PROXY_SCRIPT" || ! -f "$COMFY_DIR/auth.py" ]]; then
  echo "找不到 $PROXY_SCRIPT 或 $COMFY_DIR/auth.py" >&2
  exit 1
fi

RELAY_PYTHON=""
for cand in /usr/bin/python3 /usr/local/bin/python3 "$HOME_DIR/miniconda3/bin/python3"; do
  if [[ -x "$cand" ]] && "$cand" -c 'import tomllib' >/dev/null 2>&1; then
    RELAY_PYTHON="$cand"
    break
  fi
done
if [[ -z "$RELAY_PYTHON" ]]; then
  echo "找不到 Python >= 3.11（需要 tomllib）" >&2
  exit 1
fi

PROXY_PYTHON=""
for cand in "$HOME_DIR/miniconda3/bin/python3" "$HOME_DIR/.venv/bin/python" /usr/bin/python3; do
  if [[ -x "$cand" ]] && "$cand" -c 'import mitmproxy' >/dev/null 2>&1; then
    PROXY_PYTHON="$cand"
    break
  fi
done
if [[ -z "$PROXY_PYTHON" ]]; then
  echo "找不到安装了 mitmproxy 的 Python（可在 $HOME_DIR/miniconda3 或 .venv 中安装）" >&2
  exit 1
fi

echo "project:      $PROJECT_DIR"
echo "run user:     $RUN_USER ($HOME_DIR)"
echo "relay python: $RELAY_PYTHON"
echo "proxy python: $PROXY_PYTHON"

# Stop any manually started instances first, and make sure the units can bind.
systemctl stop auto-switch-proxy.service auto-switch-relay.service 2>/dev/null || true
"$PROJECT_DIR/run.sh" stop >/dev/null 2>&1 || true
for port in 8188 8080 8189 9099; do
  if ss -ltnH "sport = :$port" 2>/dev/null | grep -q .; then
    echo "端口 $port 仍被占用，请先停止旧进程：" >&2
    ss -ltnp "sport = :$port" 2>/dev/null | tail -n +2 >&2 || true
    exit 1
  fi
done

render() {
  local template="$1" output="$2"
  sed \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__USER__|$RUN_USER|g" \
    -e "s|__HOME__|$HOME_DIR|g" \
    -e "s|__RELAY_PYTHON__|$RELAY_PYTHON|g" \
    -e "s|__PROXY_PYTHON__|$PROXY_PYTHON|g" \
    -e "s|__COMFY_DIR__|$COMFY_DIR|g" \
    -e "s|__PROXY_SCRIPT__|$PROXY_SCRIPT|g" \
    "$template" >"$output"
  chmod 644 "$output"
}

render "$PROJECT_DIR/systemd/auto-switch-relay.service.in" \
  /etc/systemd/system/auto-switch-relay.service
render "$PROJECT_DIR/systemd/auto-switch-proxy.service.in" \
  /etc/systemd/system/auto-switch-proxy.service

systemctl daemon-reload
systemctl enable auto-switch-relay.service auto-switch-proxy.service >/dev/null

systemctl start auto-switch-relay.service
for _ in $(seq 1 50); do
  if curl -fsS -m 1 http://127.0.0.1:9099/health >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done
if ! curl -fsS -m 1 http://127.0.0.1:9099/health >/dev/null 2>&1; then
  echo "relay 没有在 10 秒内就绪，最近日志：" >&2
  journalctl -u auto-switch-relay.service -n 50 --no-pager >&2 || true
  exit 1
fi

systemctl start auto-switch-proxy.service
for _ in $(seq 1 50); do
  if ss -ltnH "sport = :8189" 2>/dev/null | grep -q .; then
    break
  fi
  sleep 0.2
done

echo
systemctl --no-pager --full status auto-switch-relay.service auto-switch-proxy.service 2>&1 | head -40 || true
echo
echo "已安装并启用。开机只启动 relay（idle）+ 8189 认证代理，不会启动 ComfyUI/Strata。"
echo "第一次真实请求到达时，才会启动对应服务。"
echo
echo "状态:  ./run.sh status"
echo "日志:  journalctl -u auto-switch-relay -f"
echo "       journalctl -u auto-switch-proxy -f"
echo "停止:  sudo systemctl stop auto-switch-relay auto-switch-proxy"
echo "卸载:  sudo ./uninstall-systemd.sh"
