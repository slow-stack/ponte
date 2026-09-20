<div align="center">

# ponte

**A persistent SSH tunnel daemon — reverse, local & SOCKS** · 持久 SSH 隧道守护工具（反向 / 本地 / SOCKS）

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blue.svg)](#cross-platform-service-management)
[![CI](https://github.com/modusensus/ponte/actions/workflows/ci.yml/badge.svg)](https://github.com/modusensus/ponte/actions/workflows/ci.yml)
[![Codecov](https://codecov.io/gh/modusensus/ponte/branch/main/graph/badge.svg)](https://codecov.io/gh/modusensus/ponte)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**Language / 语言：** [English](#english) · [中文](#中文)

</div>

---

# English

> Keep `ssh -N` forwardings alive across network drops and reboots — reconnect
> with exponential backoff + jitter, and register an OS-level auto-start
> service so it survives crashes.

## ✦ Features

- 🧭 **All three forwarding kinds** — `[[tunnels]]` rules are tagged by
  `kind`: `remote` (`-R`, the default), `local` (`-L`) and `dynamic` (`-D`, a
  SOCKS5 proxy). Mix them freely — they share one SSH connection. Duplicate listen
  ports are rejected at config load, before `ExitOnForwardFailure` can turn a
  typo into an endless reconnect loop.
- 🧵 **Many tunnels, one daemon** — `[[profiles]]` gives each SSH endpoint a
  name, its own key and its own forwarding rules. Every profile is supervised
  independently (own connection, reconnect budget, health checks and status
  file section), so one unreachable server no longer drags the others down,
  and `ponte status` / `ponte watch` have a row per tunnel. A pre-`profiles`
  config is read as a single profile named `default`.
- 🔁 **Self-healing** — infinite reconnect with exponential backoff + full
  jitter (`max_retries=0` = retry forever), so a drop never becomes a dead
  tunnel. A session that stays up ≥ `stable_after` seconds resets the retry
  budget, so a long-running tunnel is never abandoned after a few flaky drops.
- 🛟 **Crash recovery** — `install` registers an OS auto-start service:
  boot-or-logon Scheduled Task (Windows), systemd user unit (Linux), launchd agent (macOS).
- 💚 **Health checks** — periodic local-process + remote-port probing, with
  clear diagnostics instead of a black box. A "zombie" SSH process (alive but
  ports down) is force-reconnected after 3 consecutive failed checks, and
  checks back off exponentially during outages so the server's `MaxStartups`
  is never hammered.
- 🔔 **It tells you when it breaks** — after `[notify].on_consecutive_failures`
  failed attempts in a row, ponte pushes to an ntfy topic and/or a JSON webhook,
  at most once per `cooldown` for the same tunnel, and re-arms only after a
  session that stays up ≥ `stable_after` seconds. `ponte notify-test` proves the
  channel works *before* the outage. Off by default: nothing leaves your machine
  unless you enable it.
- 🩺 **`ponte doctor`** — one command that checks the config, the key file and
  its permissions, SSH reachability, listening ports, auto-start status and the
  notify channel, each row ending in a concrete fix instead of a black box.
- 📊 **A dashboard and a metrics endpoint** — `ponte serve` puts the same status
  on HTTP: `/` is a self-contained dashboard (no CDN, no JavaScript), `/healthz`
  answers `503` when a tunnel is actually broken, `/metrics` speaks Prometheus
  and `/status.json` is exactly `ponte status --json`. Loopback-only by default;
  exposing it needs an explicit token.
- 🖥️ **Cross-platform** — resolves `ssh` automatically, per-platform runtime
  paths, and portable remote-port probing (`socket` → `ss`/`lsof`/`netstat`).

## 🚀 Quick start

```bash
pipx install ponte-cli  # or: pip install ponte-cli   (pipx keeps it isolated)
ponte init              # create the config file and print its path
$EDITOR ~/.config/ponte/config.toml   # set host/user, point identity_file at your key
ponte test              # verify SSH connectivity
ponte start             # run the daemon in the background
ponte status            # check process + remote ports
ponte install           # register auto-start + crash restart
```

No config file yet? `ponte init` writes one from the shipped template. The
config lives **outside** the package, so `pip install -U ponte-cli` never
touches it. The PyPI distribution is `ponte-cli`, while the command and the
import package stay `ponte`; a checkout installs the same way (`pipx install .`).

## ⌨️ Commands

| Command | Purpose |
|---------|---------|
| `init [--path P] [--force]` | write a config file from the template (never overwrites without `--force`) |
| `start` / `start --foreground` | start daemon in background / foreground (debug) |
| `stop` / `restart` | graceful stop / stop-then-start |
| `status [--json]` | per-profile health, ports and tunnel statistics (`--json` for scripts) |
| `watch [--interval S]` | live dashboard: per-profile health, session uptime, reconnects, event feed |
| `logs [-n N] [--follow]` | view / tail the daemon log |
| `test [--profile NAME]` | quick SSH connectivity check (every profile by default) |
| `check [--profile NAME]` | verify tunnel ports are listening (`-R` on the server, `-L`/`-D` locally) |
| `doctor [--offline] [--timeout S]` | one-shot checkup of config, key, connectivity, ports, auto-start and notifications, each row with a fix |
| `notify-test [--profile NAME]` | send a test alert through the configured ntfy / webhook channels |
| `serve [--host H] [--port P] [--token T] [--open]` | local HTTP dashboard, `/healthz` probe, Prometheus `/metrics`, `/status.json` snapshot |
| `install` / `uninstall` | register / remove the OS auto-start service |
| `config` | print the effective configuration, its source file and any warnings |

Global options (before the command): `--config/-c PATH` pin a config file,
`--version/-V` print the version. Unknown/typo'd config keys are reported by
`ponte config` instead of being silently ignored.

## 📊 Web dashboard & monitoring

`ponte watch` is for the machine you are sitting at; `ponte serve` is for
everything else — a browser, a phone on the same host, Uptime Kuma, Prometheus.

```bash
ponte serve            # http://127.0.0.1:8787/  (loopback only by default)
ponte serve --open     # ...and open it in your browser
```

| Endpoint | What it answers |
|----------|-----------------|
| `/` | the dashboard: per-tunnel health, session age, availability, port state, last disconnect with its reason, event feed |
| `/healthz` | `200` while the tunnels work, `503` as soon as one is broken — the endpoint to point a monitor at |
| `/metrics` | Prometheus text exposition: session age, cumulative up/down time, availability, reconnects, port-listening state |
| `/status.json` | exactly the payload of `ponte status --json` |

**Why `/healthz` and `/metrics` disagree on purpose.** `/healthz` fails, so a
monitor can alert; `/metrics` always answers `200` and reports state as numbers,
because a scrape failure would hide *why* a tunnel went down — which is exactly
what a graph exists to show. And `/healthz` reports `starting` (with `200`) until
the first health check completes, so restarting the daemon does not page you.

**Security.** The dashboard names your servers, users and forwarded ports — it
is a map of your infrastructure, not a status line. So `ponte serve` binds
`127.0.0.1` and nothing else. Binding a LAN or public address is possible, but
only together with a token; ponte *refuses* the combination of "exposed" and
"no token" instead of warning about it:

```toml
[serve]
host = "0.0.0.0"                  # opt in, deliberately
port = 8787
token = "a-long-random-string"    # required for any non-loopback host
refresh = 5                       # dashboard auto-refresh, seconds
# ipv6 hosts are fine too: host = "::1"
```

Clients then pass `?token=...` (handy for scrapers) or `Authorization: Bearer
...`. All four endpoints are read-only, re-read the daemon status per request and
send `Cache-Control: no-store`, so a page can never show a stale "healthy" for a
tunnel that has since died.

```yaml
# prometheus.yml
scrape_configs:
  - job_name: ponte
    static_configs:
      - targets: ["127.0.0.1:8787"]
    # with a token: metrics_path: /metrics?token=a-long-random-string
```

Alert on `ponte_profile_port_listening == 0` for the signal that matters most:
a live process whose forwarded port is gone is the classic silent failure.

## 🛠️ Cross-platform service management

| Platform | Mechanism | Generated artifact |
|----------|-----------|--------------------|
| Windows | Scheduled Task (boot or logon) | `Register-ScheduledTask` (`pythonw -m ponte.main --config <file> start --foreground`) |
| Linux | systemd **user** unit | `~/.config/systemd/user/ponte.service` |
| macOS | launchd LaunchAgent | `~/Library/LaunchAgents/com.modusensus.ponte.plist` |

The daemon is always launched with an explicit `--config`, so a service running
under a different account (e.g. a SYSTEM Scheduled Task) still reads *your*
config instead of silently falling back to another one.

## 🏗️ Architecture

```
ponte (local daemon, Python)
  main.py ──▶ daemon.py ──▶ retry.py ──▶ core.py ──▶ ssh -N (-R/-L/-D)
  (typer     (lifecycle    (infinite    (pure SSH
   CLI)      orchestration) backoff)    subprocess)
                              │
                              ▼
  health.py ── periodic checks: process alive + remote ports
```

- `main.py` — typer CLI entry
- `daemon.py` — lifecycle orchestration, service install/uninstall, graceful stop
- `retry.py` — exponential backoff + jitter reconnect state machine
- `core.py` — SSH argument building, subprocess management, port probing
- `health.py` — periodic liveness + remote-port checks
- `notify.py` — ntfy / webhook alerts on repeated failures
- `doctor.py` — one-shot diagnostics used by `ponte doctor`
- `serve.py` — read-only HTTP surface (dashboard / health probe / metrics) over
  the same payload `ponte status --json` emits
- `config.py` — TOML load/validate (built-in `tomllib` on 3.11+)

## ⚙️ Configuration

Run `ponte init`, then edit the file it prints. All paths support `~` and
environment-variable expansion. Resolution order (first existing file wins):

1. `--config PATH`
2. `$PONTE_CONFIG`
3. user config dir — `%APPDATA%\ponte\config.toml` (Windows),
   `~/.config/ponte/config.toml` (Linux),
   `~/Library/Application Support/ponte/config.toml` (macOS)
4. `ponte/config.toml` inside the installed package — **legacy**, honoured only
   so pre-0.3 installs keep working; it is overwritten by `pip install -U`, so
   migrate with `ponte init`.

Sections:

- `[ssh]` — `host` / `port` / `user` / `identity_file` / `known_hosts_file` /
  `options` (any extra key there is passed through verbatim as `-o key=value`)
- `[[profiles]]` — an alternative to the single-tunnel layout: each entry has
  `name`, its own `[profiles.ssh]` and its own `[[profiles.tunnels]]`. Mixed
  with a top-level `[ssh]`/`[[tunnels]]` it is rejected rather than guessed at;
  `retry`/`health`/`daemon`/`service` stay global policy for every profile.
- `[[tunnels]]` — forwarding rules. `kind` picks the flag and what the fields
  mean: `remote` (default, `-R`: the server listens on `remote_port` and
  forwards back to `local_host:local_port`), `local` (`-L`: this machine
  listens on `local_host:local_port` and forwards to `remote_host:remote_port`)
  and `dynamic` (`-D`: a SOCKS5 proxy on `local_host:local_port`). For `-L`/`-D`
  the bind address defaults to `127.0.0.1`, so an omitted field is never a LAN
  exposure.
- `[daemon]` — pid/log paths (default per-platform: `%LOCALAPPDATA%\ponte`,
  `~/.local/state/ponte`, `~/Library/Application Support/ponte`), log rotation
- `[retry]` — `max_retries` (0 = forever), backoff params, `jitter`,
  `stable_after`
- `[health]` — check interval, remote probe toggle/timeout,
  `max_check_interval` (backoff ceiling while unhealthy)
- `[notify]` — `enabled` (default `false`), `on_consecutive_failures`,
  `cooldown` (seconds between two alerts for the same profile), and the
  channels: `ntfy_topic` (plus optional `ntfy_server` / `ntfy_token`) and/or
  `webhook_url`, which receives the alert as JSON
- `[service]` — service name, autostart, POSIX kill grace
- `[windows]` — Windows-only knobs (`task_name`, `ssh_exe`, `pythonw_exe`,
  `run_as`). `run_as` is `user` (default: logon-time, runs as you, can read
  `~/.ssh`) or `system` (boot-time, survives login/reboot, needs elevation
  **and** an identity file SYSTEM can read). `pythonw_exe` pins the windowless
  interpreter the Scheduled Task runs: ponte refuses to install a task that
  would fall back to `python.exe`, because that flashes a console window at
  every logon.

## 🔍 Troubleshooting

| Symptom | Where to look |
|---------|---------------|
| `Permission denied (publickey)` | public key on server `~/.ssh/authorized_keys`; on Windows strip inherited ACLs (`icacls id_rsa /inheritance:r /grant:r <user>:(R)`) |
| Connection rejected after key change | delete `known_hosts`, reconnect (`StrictHostKeyChecking=accept-new` default) |
| Process alive but remote port down | cloud security-group inbound rules; check server with `ss -tlnp` / `lsof -nP -iTCP -sTCP:LISTEN` — the daemon now force-reconnects a "zombie" tunnel after 3 consecutive failed checks |
| Console window flashes at logon, or while stopping | the Scheduled Task must run `pythonw.exe` — check `[windows] pythonw_exe`; `ponte stop` also force-kills through a hidden `taskkill` |
| `ponte serve` exits with "cannot bind" / port busy | another process holds the port — `ponte serve --port 8788`; the refused non-loopback bind is a *token* problem, and the message says so |
| `/healthz` returns `401` | a `[serve].token` is set: pass `?token=...` or `Authorization: Bearer ...` |
| `/healthz` returns `503` while the tunnel looks fine | it reports the *tunnel*, not the process: read `unhealthy` / `errors` in the body, then `ponte check` |
| Logs | `ponte logs -n 100 --follow` |

## 🧪 Development & testing

```bash
pip install -e ".[dev]"
pytest --cov=ponte --cov-report=term-missing   # threshold in pyproject.toml
ruff check .                                   # lint
mypy                                           # type check
python _smoke_test.py                          # zero-dependency quick check
```

CI runs lint + types on Linux, and the test suite across
Windows/Linux/macOS × Python 3.11/3.12, reporting coverage to
[Codecov](https://codecov.io/gh/modusensus/ponte). A `build` job also installs
the built wheel and runs `ponte init`, so a packaging regression cannot ship
again. See [CONTRIBUTING.md](CONTRIBUTING.md).

## 📝 Notes

- **Never commit the private key**: `.gitignore` excludes `id_rsa` /
  `id_rsa.pub`; place your own keys on each machine.
- Runtime files (`ponte.pid` / `ponte.status.json` / `ponte.stop` /
  `ponte.log*`) are generated and not committed.
- **Upgrading from ≤ 0.2.x**: the config file moved out of the package. Run
  `ponte init` (it migrates the in-package file if one exists), then
  `ponte install` again so the service picks up the new `--config` argument.
- Legacy pre-Python scripts (`tunnel.ps1`, `setup.ps1`, `ssh-tunnel.bat`,
  `ssh-tunnel.vbs`, `fix-wsl-tunnel.sh`) live in [`legacy/`](legacy/) and are
  **deprecated** — the CLI replaces them. `setup.ps1` copied your private key
  into the project directory; do not use it.
- Found a security issue? See [SECURITY.md](SECURITY.md) for how to report it
  privately.

---

# 中文

> 让 `ssh -N -R` 在网络抖动与重启后依然存活——断线自动以指数退避 + 抖动重连，
> 并注册系统级开机自启服务，进程崩溃也能被拉活。

## ✦ 特性

- 🧵 **一个守护进程，多条隧道** — `[[profiles]]` 给每个 SSH 端点一个名字、
  一套密钥和一组转发规则；每条 profile 各自重连、各自健康检查、在状态文件里
  各占一段，所以一台服务器连不上不会拖垮其它隧道，`ponte status` / `ponte watch`
  也是每条隧道一行。升级前的单隧道配置会被当作名为 `default` 的 profile 读取。
- 🧭 **三种转发都支持** — `[[tunnels]]` 规则用 `kind` 区分：`remote`（`-R`，默认）、
  `local`（`-L`）、`dynamic`（`-D`，SOCKS5 代理），可以混用且共用一条 SSH 连接。
  重复的监听端口在加载配置时就会被拒绝，而不是让 `ExitOnForwardFailure`
  把一次手误变成无限重连。
- 🔁 **自愈** — 无限重连 + 指数退避 + 全抖动（`max_retries=0` = 永远重试），
  掉线不会变成死隧道。会话稳定运行 ≥ `stable_after` 秒后重试预算归零，
  长跑隧道不会因前期几次抖动被永久放弃。
- 🛟 **崩溃兜底** — `install` 注册系统级开机自启服务：Windows 计划任务（开机或登录） /
  Linux systemd user / macOS launchd。
- 💚 **健康检查** — 周期探测本地进程存活 + 远程端口，异常给出明确诊断。
  SSH 进程假死（活着但端口全掉）时连续 3 次检查失败即强制重连；检查失败
  指数退避，不会高频新开 SSH 触发服务器 `MaxStartups`。
- 🔔 **真断了会主动告诉你** — 连续 `[notify].on_consecutive_failures` 次失败后，
  向 ntfy 主题和/或 JSON webhook 推送一条告警；同一隧道每个 `cooldown` 秒最多
  一条，且只有会话稳定运行 ≥ `stable_after` 秒才重新武装。`ponte notify-test`
  让你在真出事**之前**就验证通道可用。默认关闭：不开启就绝不会外发任何数据。
- 🩺 **`ponte doctor`** — 一条命令逐项体检：配置、密钥及其权限、SSH 连通性、
  监听端口、开机自启状态、通知通道，每行都给出具体修法而不是留个黑箱。
- 📊 **看板与指标接口** — `ponte serve` 把同一份状态摆到 HTTP 上：`/` 是自包含的
  看板（不依赖 CDN、不用 JavaScript），`/healthz` 在隧道真的断时回 `503`，
  `/metrics` 说 Prometheus 格式，`/status.json` 就是 `ponte status --json`。
  默认只监听本机；要对外必须先给令牌。
- 🖥️ **跨平台** — 自动查找 `ssh`、按平台落盘运行时文件、可移植的远程端口探测
  （`socket` → `ss`/`lsof`/`netstat`）。

## 🚀 快速开始

```bash
pipx install ponte-cli  # 或 pip install ponte-cli（pipx 会隔离安装）
ponte init              # 生成配置文件并打印路径
$EDITOR ~/.config/ponte/config.toml   # 填 host/user，identity_file 指向你的密钥
ponte test              # 验证 SSH 连通性
ponte start             # 后台启动守护进程
ponte status            # 查看进程 + 远程端口
ponte install           # 注册开机自启 + 崩溃重启
```

还没有配置文件？`ponte init` 会从内置模板生成一份。配置存放在**包外**，
`pip install -U ponte-cli` 不会覆盖它。PyPI 上的发行名是 `ponte-cli`，
命令与导入包名仍为 `ponte`（从源码目录安装同样可用 `pipx install .`）。

## ⌨️ 命令

| 命令 | 用途 |
|------|------|
| `init [--path P] [--force]` | 从模板生成配置文件（不加 `--force` 不覆盖） |
| `start` / `start --foreground` | 后台启动 / 前台启动（调试） |
| `stop` / `restart` | 优雅停止 / 停旧起新 |
| `status [--json]` | 逐条隧道的健康、端口与统计（`--json` 供脚本消费） |
| `watch [--interval S]` | 实时看板：每条隧道一栏，含会话时长、重连次数与事件流 |
| `logs [-n N] [--follow]` | 查看 / 跟读日志 |
| `test [--profile NAME]` | 快速测 SSH 连通性（默认逐条测试） |
| `check [--profile NAME]` | 检查隧道端口（`-R` 在服务器上，`-L`/`-D` 在本机） |
| `doctor [--offline] [--timeout S]` | 一键体检配置、密钥、连通性、端口、自启与通知，每项给出修法 |
| `notify-test [--profile NAME]` | 通过已配置的 ntfy / webhook 通道发一条测试通知 |
| `serve [--host H] [--port P] [--token T] [--open]` | 本地 HTTP 看板、`/healthz` 探活、Prometheus `/metrics`、`/status.json` 快照 |
| `install` / `uninstall` | 注册 / 移除开机自启服务 |
| `config` | 打印生效配置、来源文件与配置告警 |

全局选项（写在子命令之前）：`--config/-c PATH` 指定配置文件，
`--version/-V` 打印版本。拼错/未知的配置项会由 `ponte config` 报出来，
不再被静默忽略。

## 📊 网页看板与监控接入

`ponte watch` 给坐在机器前的你看，`ponte serve` 给其它一切：浏览器、手机
（同机）、Uptime Kuma、Prometheus。

```bash
ponte serve            # http://127.0.0.1:8787/（默认只监听本机）
ponte serve --open     # 顺手在浏览器里打开
```

| 接口 | 回答什么问题 |
|------|--------------|
| `/` | 看板：逐条隧道的健康、当前会话时长、在线率、端口状态、上次断线原因与事件流 |
| `/healthz` | 隧道正常时 `200`，任一条断开立即 `503` —— 监控就探这个 |
| `/metrics` | Prometheus 文本格式：会话时长、累计在线/离线、在线率、重连次数、端口监听状态 |
| `/status.json` | 与 `ponte status --json` 完全一致的载荷 |

**为什么 `/healthz` 与 `/metrics` 故意不一致。** `/healthz` 会失败，监控才能
报警；`/metrics` 永远回 `200`，把状态当数字报出来——因为采挂掉会盖住
“它为何挂了”，而那正是画图的目的。另外首次健康检查完成前，`/healthz`
报的是 `starting`（`200`），所以重启守护进程不会造成误报。

**安全模型。** 看板会列出你的服务器地址、登录用户与转发端口——这是一张
内网拓扑图，不是一行状态。所以 `ponte serve` 只绑 `127.0.0.1`。绑到局域网或
公网是可以的，但**必须**同时给令牌：ponte 对“对外 + 无令牌”的组合是直接
拒绝，而不是警告一句了事。

```toml
[serve]
host = "0.0.0.0"                  # 显式选择对外
port = 8787
token = "一个足够长的随机串"      # 非回环地址必需
refresh = 5                       # 看板自动刷新秒数
# 也支持 IPv6：host = "::1"
```

客户端用 `?token=...`（脚本/采集器方便）或 `Authorization: Bearer ...`。
四个接口全是只读、每次请求都重新读取守护进程状态，并带
`Cache-Control: no-store`——所以页面不会拿旧的“健康”去骗一个已经挂了的隧道。

```yaml
# prometheus.yml
scrape_configs:
  - job_name: ponte
    static_configs:
      - targets: ["127.0.0.1:8787"]
    # 带令牌时：metrics_path: /metrics?token=一个足够长的随机串
```

最值得拿来报警的一条是 `ponte_profile_port_listening == 0`：进程活着、
转发端口却没了，正是那种悄无声息的典型故障。

## 🛠️ 跨平台服务管理

| 平台 | 机制 | 生成物 |
|------|------|--------|
| Windows | 计划任务（开机或登录） | `Register-ScheduledTask`（`pythonw -m ponte.main --config <文件> start --foreground`） |
| Linux | systemd **user** 单元 | `~/.config/systemd/user/ponte.service` |
| macOS | launchd LaunchAgent | `~/Library/LaunchAgents/com.modusensus.ponte.plist` |

守护进程始终带显式 `--config` 启动：即使服务以其它身份运行（例如 SYSTEM
计划任务），读到的仍是**你这份**配置，而不是静默回退到别处。

## 🏗️ 架构

```
ponte（本地守护进程，Python）
  main.py ──▶ daemon.py ──▶ retry.py ──▶ core.py ──▶ ssh -N（-R/-L/-D）
  (typer     (生命周期     (无限退避     (纯 SSH
   CLI)      编排)         重连)         subprocess)
                              │
                              ▼
  health.py ── 周期检查：进程存活 + 远程端口（-R）+ 本地监听（-L/-D）
```

一个守护进程为每个 `[[profiles]]` 建一个 `ProfileRunner`（各自的 SSH 会话、
重连循环、健康检查），进程级的 pid / 状态文件 / 服务注册由守护进程统一持有；
状态文件按 profile 分区，所以一条隧道挂了不会影响其它隧道。

- `main.py` — typer 命令行入口
- `daemon.py` — 生命周期编排、服务安装/卸载、优雅停止
- `retry.py` — 指数退避 + 抖动重连状态机
- `core.py` — SSH 参数构建、子进程管理、端口探测
- `health.py` — 周期存活 + 远程端口检查
- `notify.py` — 连续失败时的 ntfy / webhook 告警
- `doctor.py` — `ponte doctor` 使用的体检项
- `serve.py` — 只读 HTTP 接口（看板 / 探活 / 指标），渲染的就是
  `ponte status --json` 那份载荷
- `config.py` — TOML 加载/校验（3.11+ 内置 `tomllib`）

## ⚙️ 配置

先运行 `ponte init`，再编辑它打印出的文件。所有路径支持 `~` 与环境变量展开。
查找顺序（先找到的生效）：

1. `--config PATH`
2. 环境变量 `$PONTE_CONFIG`
3. 用户配置目录 —— Windows `%APPDATA%\ponte\config.toml`、
   Linux `~/.config/ponte/config.toml`、
   macOS `~/Library/Application Support/ponte/config.toml`
4. 包内 `ponte/config.toml` —— **旧位置**，仅为兼容 0.3 之前的安装保留；
   它会被 `pip install -U` 覆盖，请用 `ponte init` 迁移。

各段含义：

- `[ssh]` — `host` / `port` / `user` / `identity_file` / `known_hosts_file` /
  `options`（该表内未列出的键会原样透传为 `-o key=value`）
- `[[profiles]]` — 单隧道写法的替代品：每个条目有 `name`、自己的
  `[profiles.ssh]` 与 `[[profiles.tunnels]]`。与顶层 `[ssh]`/`[[tunnels]]`
  混用会被拒绝（而不是猜你的意图）；`retry`/`health`/`daemon`/`service`
  仍是所有 profile 共用的全局策略。
- `[[tunnels]]` — 转发规则。`kind` 决定用哪个转发开关、各字段是什么意思：
  `remote`（默认，`-R`）服务器监听 `remote_port` 并转发回
  `local_host:local_port`；`local`（`-L`）本机监听 `local_host:local_port`
  并转发到服务器侧的 `remote_host:remote_port`；`dynamic`（`-D`）在
  `local_host:local_port` 上开一个 SOCKS5 代理。`-L`/`-D` 的绑定地址默认
  `127.0.0.1`，省略字段不会意外暴露到局域网。
- `[daemon]` — pid/log 路径（平台默认：`%LOCALAPPDATA%\ponte`、
  `~/.local/state/ponte`、`~/Library/Application Support/ponte`）、日志滚动
- `[retry]` — `max_retries`（0 = 无限）、退避参数、`jitter`、`stable_after`
- `[health]` — 检查间隔、远程探测开关/超时、`max_check_interval`
  （不健康期间的间隔退避上限）
- `[notify]` — `enabled`（默认 `false`）、`on_consecutive_failures`、
  `cooldown`（同一 profile 两条告警之间的最小秒数），以及通道：
  `ntfy_topic`（可选 `ntfy_server` / `ntfy_token`）和/或 `webhook_url`
  （以 JSON 形式收到告警）
- `[serve]` — 本地看板：`host`（默认 `127.0.0.1`）、`port`（默认 `8787`）、
  `token`（绑定非回环地址时必填，否则拒绝启动）、`refresh`（看板刷新秒数）
- `[service]` — 服务名、自启、POSIX 强杀等待
- `[windows]` — 仅 Windows 使用（`task_name`、`ssh_exe`、`pythonw_exe`、
  `run_as`）。`run_as` 默认 `user`（登录后以你本人身份运行、能读 `~/.ssh`）或
  `system`（开机即起、重启也能拉起，但需提权，且 `identity_file`
  必须是 SYSTEM 能读到的文件）。`pythonw_exe` 指定计划任务使用的无窗口解释器：
  若只能回退到 `python.exe`，ponte 会拒绝安装——那会导致每次登录弹出黑色窗口。

## 🔍 排障

| 症状 | 排查方向 |
|------|----------|
| 「Permission denied (publickey)」 | 公钥是否加入服务器 `~/.ssh/authorized_keys`；Windows 下私钥去掉继承 ACL（`icacls id_rsa /inheritance:r /grant:r <用户名>:(R)`） |
| 换 key 后连接被拒 | 删除 `known_hosts` 重连（默认 `StrictHostKeyChecking=accept-new`） |
| 进程活着但远程端口不通 | 云安全组入方向规则；服务器上 `ss -tlnp` / `lsof -nP -iTCP -sTCP:LISTEN` 确认监听 —— 守护进程已支持假死检测：连续 3 次检查失败自动强制重连 |
| 登录时（或 `stop` 时）闪出黑色控制台窗口 | 计划任务必须跑 `pythonw.exe`——检查 `[windows] pythonw_exe`；`ponte stop` 的强杀也已隐藏控制台 |
| `ponte serve` 报绑定失败 / 端口占用 | 换端口：`ponte serve --port 8788`；若报的是非回环地址，那是**令牌**问题，报错里写了 |
| `/healthz` 返回 `401` | 配了 `[serve].token`：带上 `?token=...` 或 `Authorization: Bearer ...` |
| 隧道看着正常，`/healthz` 却回 `503` | 它报的是**隧道**不是进程：看响应体里的 `unhealthy` / `errors`，再用 `ponte check` 复核 |
| 排查日志 | `ponte logs -n 100 --follow` |

## 🧪 开发与测试

```bash
pip install -e ".[dev]"
pytest --cov=ponte --cov-report=term-missing   # 阈值见 pyproject.toml
ruff check .                                   # 静态检查
mypy                                           # 类型检查
python _smoke_test.py                          # 零依赖快速自检
```

CI 在 Linux 上跑 lint + 类型检查，在 Windows/Linux/macOS × Python
3.11/3.12 上跑测试，覆盖率上报到
[Codecov](https://codecov.io/gh/modusensus/ponte)。另有一个 `build` 任务会
安装打好的 wheel 并执行 `ponte init`，避免打包问题再次溜进发布。详见
[CONTRIBUTING.md](CONTRIBUTING.md)。

## 📝 注意事项

- **私钥绝不计入仓库**：`.gitignore` 已排除 `id_rsa` / `id_rsa.pub`；
  各机器自行放置密钥。
- 运行时文件（`ponte.pid` / `ponte.status.json` / `ponte.stop` /
  `ponte.log*`）为生成物，不入库。
- **从 ≤ 0.2.x 升级**：配置文件已迁出包目录。请运行 `ponte init`
  （若存在包内旧配置会直接迁移），然后重新 `ponte install`，
  让服务带上新的 `--config` 参数。
- Python 化之前的遗留脚本（`tunnel.ps1`、`setup.ps1`、`ssh-tunnel.bat`、
  `ssh-tunnel.vbs`、`fix-wsl-tunnel.sh`）已移到 [`legacy/`](legacy/) 并标记
  为**废弃**，请改用 CLI。其中 `setup.ps1` 会把你的私钥复制进项目目录，
  不要再使用。
- 发现安全问题？见 [SECURITY.md](SECURITY.md)，请私下报告。
