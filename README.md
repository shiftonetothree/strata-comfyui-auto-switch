# auto-switch：ComfyUI / Strata 流量中转与互斥切换

用一台机器在 **comfyui-docker** 和 **strata** 之间动态切换：同一时刻只让一个服务运行，
请求先到中转服务，由它决定是直接转发，还是先停掉旧服务、启动新服务再转发。

ComfyUI 的对外入口仍然是 `run_comfyui_proxy.sh`（mitmproxy + Bearer 认证，端口 8189）；
中转服务藏在它后面，所以认证、同源检查和 WebSocket 行为都不变：

```
ComfyUI 客户端 ──Bearer──► :8189 run_comfyui_proxy.sh ──► :8188 relay ──► 127.0.0.1:18188 ──► comfyui-megapak
                            (mitmproxy, 认证)              (仅回环)        容器内 8188
浏览器 / API    ─────────► :8080 relay (strata) ─────────────────────────► 127.0.0.1:18080 ──► strata
                            │ 冷却判断 / 切换 / 启停容器                   容器内 8080
                            └─ :9099 admin API (仅本机)
```

## 1. 规则

**切换条件**：收到服务 B 的请求，且当前服务 A 已经 **超过 2 分钟（120 秒）没有收到任何请求**。

- 条件不满足（A 在 2 分钟内被用过）→ 请求立即返回 `503` + `Retry-After`，**不会**切换。
- 条件满足 → 停止 A 的容器（等待其真正退出，Strata 最多可花 2 分钟卸载模型）→ 启动 B
  → 轮询 B 的健康检查直到就绪 → 转发请求。
- 切换期间，发往 B 的其它请求会**挂起等待**同一个切换完成；发往 A 的请求返回 `503 Busy`。
- 访问当前正在运行的服务：永远直接转发，并刷新它的"最近活动时间"。

WebSocket 帧也算活动（ComfyUI 的进度/预览帧）；只有 ping/pong 控制帧不算，
所以一个正在跑图的 ComfyUI 不会被切走。

**健康/状态轮询不算活动。** ComfyUI Web UI 会轮询 `/system_stats`、`/object_info`、`/queue`，
Strata Web UI / monitor 会轮询 `/health`、`/v1/models`、`/v1/status`、`/metrics` 等；
这些路径即使一直在轮询，也不会刷新冷却、不会触发切换。只有真正的使用请求才会：
ComfyUI 的 `POST /prompt`、`/view`、`/history`，Strata 的 `/v1/chat/completions` 等。
列表在 `config.toml` 的 `ignore_activity_paths`，按前缀匹配。

如果目标服务未运行，发往这些监控路径的请求返回 `503 inactive`（不会因此切换到它）；
目标服务已运行时则正常转发，但不算活跃。

浏览器请求（`Accept: text/html`）会收到一个会自动刷新的中文提示页，API 客户端收到 JSON。
两种响应都带 `Retry-After`。

因为入口是 8189 的认证代理、relay 的 8188 只绑定回环，所以**未通过认证的请求不会触发切换**，
也无法绕过代理直接访问 ComfyUI。

## 2. 端口

| 用途 | 地址 | 说明 |
|---|---|---|
| ComfyUI 入口 | `127.0.0.1:8189` | `run_comfyui_proxy.sh`（Bearer 认证），可用 `LISTEN_HOST` 改绑定 |
| ComfyUI relay | `127.0.0.1:8188` | 仅本机，由中转服务监听；只接受来自 8189 代理的请求 |
| ComfyUI 上游 | `127.0.0.1:18188` | 仅本机，容器内仍是 8188 |
| Strata 入口 | `0.0.0.0:8080` | 与原来一致，由中转服务监听（API key 仍由 Strata 校验） |
| Strata 上游 | `127.0.0.1:18080` | 仅本机，容器内仍是 8080 |
| 管理 API | `127.0.0.1:9099` | 仅本机，查看状态 / 手动切换 |

两个上游都只绑定回环地址；ComfyUI 的 8188 也只绑定回环，避免绕过 8189 的认证。

注意：`auth.py` 对所有路径都要求 `Authorization: Bearer ...`，浏览器的 Web UI 和 WebSocket
无法自动携带这个头（浏览器发 WS 时不能自定义 Authorization）。因此：

- 在本机用浏览器打开 UI：直接用 `http://127.0.0.1:8188`（仅回环，不经过认证代理）。
- 远程/自动化客户端：走 `http://127.0.0.1:8189` 并带 Bearer；远程访问需要先让 8189
  监听 `0.0.0.0`（`LISTEN_HOST=0.0.0.0`）或走 SSH 隧道。

## 3. 快速开始

```bash
cd /mnt/kvm/auto-switch

./run.sh check          # 校验 config.toml
./run.sh start          # 后台启动 relay + ComfyUI 入口代理（会按默认服务收敛状态）
./run.sh status         # 查看实时状态（JSON；代理状态输出到 stderr）
./run.sh logs           # 跟踪 logs/relay.log
./run.sh logs proxy     # 跟踪 logs/comfyui-proxy.out
./run.sh switch strata --force   # 立即切到 Strata（忽略冷却）
./run.sh switch comfyui          # 切回 ComfyUI（受冷却限制）
./run.sh proxy status   # 只查看 8189 入口代理
./run.sh stop           # 停止 relay 和入口代理（不停止已运行的容器）
```

`start` 会按顺序启动 relay（等待管理 API）和 `run_comfyui_proxy.sh`（等待 8189 监听），
两者 PID 分别写入 `run/relay.pid` 和 `run/comfyui-proxy.pid`。
首次启动或重启后，若两个容器都在运行，会按 `default_service`（默认 `comfyui`）保留一个、
停掉另一个；如果默认容器没在运行则启动它。

## 4. 管理 API

只监听 `127.0.0.1:9099`：

```bash
curl -s http://127.0.0.1:9099/status | python3 -m json.tool
curl -s http://127.0.0.1:9099/health
curl -s -X POST 'http://127.0.0.1:9099/switch/strata?force=1'
```

`/status` 返回 active、切换中状态、冷却剩余秒数、每个服务的运行/健康情况、
最近活动时间、最近一次真实请求（`last_request`）、最近一次被忽略的轮询
（`last_ignored`，含来源 IP）和计数等。`/switch/<name>` 必须用 `POST`；`force=1` 跳过冷却，
不加则遵守冷却规则（被拒绝时返回 `409` + `Retry-After`）。

## 5. 配置（`config.toml`）

| 键 | 默认 | 含义 |
|---|---|---|
| `cooldown_seconds` | `120` | 切换所需的最小空闲时间 |
| `default_service` | `comfyui` | 启动时保留的服务（`startup_mode=default` 时） |
| `startup_mode` | `default` | `default`：启动时拉起默认服务；`idle`：只监听，不启动任何容器 |
| `request_timeout_seconds` | `600` | 请求等待切换完成的上限 |
| `startup_reconcile` | `true` | 启动时强制只保留默认服务 |
| `auto_restart_active` | `true` | 当前服务容器意外退出时自动重启 |
| `admin_host` / `admin_port` | `127.0.0.1` / `9099` | 管理 API |
| `services.comfyui.bind_host` | `127.0.0.1` | relay 的 ComfyUI 监听地址（不建议改成 0.0.0.0） |
| `health_path` / `health_json` | 按服务 | 就绪判定 |
| `ignore_activity_paths` | 按服务 | 健康/状态轮询路径，不算活动、不触发切换 |
| `stop_timeout_seconds` | comfyui 60 / strata 180 | 停止等待上限 |
| `startup_timeout_seconds` | comfyui 300 / strata 900 | 启动就绪等待上限 |
| `[proxy] enabled` | `true` | 由 `run.sh` 管理 8189 入口代理 |
| `[proxy] command` | `../comfyui-docker/run_comfyui_proxy.sh` | 入口代理脚本 |
| `[proxy] listen_port` | `8189` | 入口端口 |
| `[proxy] upstream` | `http://127.0.0.1:8188` | 默认指向 relay 的 ComfyUI 监听 |

Strata 的 `/health` 在模型加载期间也会返回 `200`，因此配置了
`health_json = { loaded = true }`，只有 `loaded` 为 true 才算就绪。
ComfyUI 以 `/system_stats` 返回 2xx 为准（模型是按需加载的）。

修改配置后执行 `./run.sh restart` 生效。若不想让 `run.sh` 管理入口代理，设
`[proxy] enabled = false`，然后自己按原来的方式运行 `run_comfyui_proxy.sh`（它默认的
upstream `http://127.0.0.1:8188` 正好指向 relay）。

## 6. 对两个项目的改动

只改了 **宿主机端口映射**（容器内部端口不变）：

- `../comfyui-docker/docker-compose.yml`：`"8188:8188"` → `"127.0.0.1:18188:8188"`
- `../strata/docker-compose.yml`：`"0.0.0.0:8080:8080"` → `"127.0.0.1:18080:8080"`

`run_comfyui_proxy.sh` 不需要修改：它默认的入口 8189 和 upstream 8188 正好是
"入口 → relay" 的位置。两个服务的外部地址（8189 / 8080）保持不变，原有客户端无需修改。

如果不再需要中转，恢复原状：

```bash
./run.sh stop
cd ../comfyui-docker && docker compose up -d --force-recreate
cd ../strata && docker compose up -d --force-recreate
```

（先把上面两处端口改回原值，否则会和已停止的中转端口冲突。）

## 7. 切换过程中会发生什么

以 ComfyUI → Strata 为例（请求先经过 8189 的认证代理，再到达 relay）：

1. 认证通过的请求到达 relay:8188；ComfyUI 已空闲 ≥ 120 秒。
2. `docker compose stop comfyui`（释放 GPU 0/显存/内存）。
3. `docker compose up -d strata`（按新端口映射启动；如果 compose 有改动会自动重建）。
4. 每 2 秒探测 `127.0.0.1:18080/health`，直到 `loaded == true`；Strata 冷启动通常需要
   几十秒到几分钟。
5. 请求被转发，之后 Strata 成为 active。
6. 如果启动失败/超时：停止目标容器，`active` 置空，请求收到 `503`，下一次请求可以重试。

停止 Strata 时 compose 的 `stop_grace_period: 2m` 会让模型干净卸载，因此反方向切换
的最坏耗时会明显更长；`request_timeout_seconds` 默认给了 10 分钟。

切换只停容器，不停 8189 入口代理：ComfyUI 不活跃时，发往 8189 的请求仍会被认证代理
接收，再由 relay 返回 `503`（冷却/切换中），浏览器会自动刷新重试。

## 8. 后台运行与开机自启

### systemd（推荐）

```bash
cd /mnt/kvm/auto-switch
sudo ./install-systemd.sh
```

安装两个系统服务：

- `auto-switch-relay.service`：relay，启动参数带 `--startup-mode idle`
- `auto-switch-proxy.service`：`run_comfyui_proxy.sh`（8189 认证入口）

**开机时只启动 relay 和认证代理，不会启动 ComfyUI 或 Strata。** 第一次真实请求
到达时，relay 才会启动对应容器；`ignore_activity_paths` 里的健康/状态轮询不会触发启动。

安装后：

- `./run.sh status`、`./run.sh switch ...` 仍然可用（走管理 API）。
- `./run.sh start/stop` 会提示改用 `systemctl`，避免和 systemd 抢 PID/端口。
- 日志：`journalctl -u auto-switch-relay -f`、`journalctl -u auto-switch-proxy -f`。
- 状态/停止：`systemctl status auto-switch-relay auto-switch-proxy`、
  `sudo systemctl stop auto-switch-relay auto-switch-proxy`。
- 卸载：`sudo ./uninstall-systemd.sh`（不会停止容器）。

安装脚本会先停掉 `run.sh` 手工启动的 relay/代理，但**不会停止已经运行的容器**。
如果安装时 ComfyUI/Strata 还在跑，它们会继续跑；下次开机才是空闲状态。
想立刻空闲：在对应项目目录执行 `docker compose stop`。

### cron（不需要 sudo 的替代方案）

`run.sh autostart` 以 idle 模式启动 relay + 代理（不会启动容器），可以放进用户 crontab：

```cron
@reboot /mnt/kvm/auto-switch/run.sh autostart
```

cron 不负责崩溃重启，systemd 更稳；两种方式不要同时启用。

### 手动

```bash
./run.sh start              # 按 startup_mode=default 拉起 default_service
./run.sh autostart          # idle：只启动 relay + 代理
./run.sh stop               # 停止 relay + 代理（不停止容器）
```

注意：

- `run.sh stop` / `systemctl stop` 都不会停止正在运行的容器；需要释放资源时手动
  `docker compose stop` 或让中转切到另一个服务。
- 入口代理手工运行时日志在 `logs/comfyui-proxy.out`，PID 在 `run/comfyui-proxy.pid`；
  用 `./run.sh proxy status|logs` 查看；systemd 模式下用 `journalctl`。
- 不要再直接执行两个项目里的 `./start.sh` 或 `docker compose up`：那会绕过中转、
  可能让两个服务同时运行。服务的启停交给中转（`run.sh switch` / 首次请求）。

## 9. 测试

```bash
cd /mnt/kvm/auto-switch
python3 -m unittest discover -s tests -t . -v
```

覆盖：冷却规则、并发切换去重、切换失败回退、启动收敛、状态持久化、
HTTP/分块/WebSocket 转发、503 页面、管理 API、配置校验。

## 10. 目录

```
auto-switch/
├── config.toml      # 服务、端口、冷却、健康检查、入口代理
├── run.sh           # start/autostart/stop/status/switch/logs/proxy
├── install-systemd.sh / uninstall-systemd.sh
├── systemd/         # auto-switch-relay.service.in、auto-switch-proxy.service.in
├── relay.py         # 入口
├── relay/           # config / docker / manager / proxy / admin / cli
├── tests/           # 50 个单元与端到端测试
├── logs/            # relay.log（10MB × 5 轮转）、relay.out、comfyui-proxy.out
└── run/             # relay.pid、comfyui-proxy.pid、state.json
```
