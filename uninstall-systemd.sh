#!/usr/bin/env bash
# Disable and remove the auto-switch systemd services.
#
#   sudo ./uninstall-systemd.sh
#
# Containers are not touched; stop them yourself if you want to free the GPU.
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "请用 sudo 运行：sudo $0" >&2
  exit 1
fi

systemctl disable --now auto-switch-proxy.service auto-switch-relay.service 2>/dev/null || true
rm -f /etc/systemd/system/auto-switch-relay.service \
      /etc/systemd/system/auto-switch-proxy.service
systemctl daemon-reload
echo "已移除 auto-switch-relay.service 和 auto-switch-proxy.service（容器未改动）。"
echo "之后仍可手动使用 ./run.sh start / stop。"
