# Contributing

Thanks for your interest in ponte. This document covers how to set up a dev
environment, run tests, and open a clean change.

**Language / 语言：** [English](#english) · [中文](#中文)

---

# English

## Getting started

Requires **Python 3.11+** (uses the built-in `tomllib`).

```bash
git clone git@github.com:modusensus/ponte.git
cd ponte
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Running tests

```bash
pytest                          # full suite (config, core, daemon, health, main, retry)
pytest --cov=ponte --cov-report=term-missing   # with coverage report
ruff check .                    # lint (must be clean)
mypy                            # type check (must be clean)
python _smoke_test.py           # zero-dependency smoke check
```

Coverage has a `fail_under` threshold in `pyproject.toml`; don't let it drop.
CI runs lint + types on Linux, the same test suite on Windows / Linux / macOS ×
Python 3.11 / 3.12, and a `build` job that installs the wheel and runs
`ponte init`. Coverage is uploaded to Codecov.

One more job re-runs the suite with `PONTE_TEST_THREAD_DELAY=0.15`. That variable
makes `tests/conftest.py` inject latency into worker-thread sleeps and waits, so a
test that only passes because the machine is fast fails there **every time**
instead of flaking once in a while. Run it the same way before blaming a runner:

```bash
PONTE_TEST_THREAD_DELAY=0.15 pytest     # 慢机器模拟（Git Bash / POSIX 语法）
```

## Project layout

```
ponte/
  __init__.py           # version
  main.py               # typer CLI
  daemon.py             # lifecycle, service install/uninstall, graceful stop
  retry.py              # reconnect state machine (backoff + jitter)
  core.py               # SSH args / subprocess / port probing
  health.py             # periodic checks
  config.py             # TOML load/validate + config file resolution
  config.example.toml   # template shipped for `ponte init`
tests/                  # pytest suite (+ conftest.py fixtures)
_smoke_test.py          # offline smoke script
legacy/                 # deprecated pre-Python scripts (reference only)
CHANGELOG.md            # notable changes per release
```

## Conventions

- **Follow the existing style**: plain Python with `from __future__ import
  annotations`, detailed docstrings on public methods.
- **Keep it offline-testable**: the test suite must not need a network
  connection or a real SSH server. If you touch `core.py` / `daemon.py`,
  add/adjust tests that mock `subprocess` or use fake configs.
- **Cross-platform awareness**: don't hardcode Windows paths or Linux-only
  commands. Platform-specific branches go behind `sys.platform` checks, and
  runtime files use the per-platform defaults from `config.py`.
- **No secrets in the repo**: never commit keys, tokens, `.env`, or real
  server addresses. `config.example.toml` ships with placeholders on purpose,
  and the real config lives outside the repository.
- **Config handling**: read configuration through `get_config()` (never by
  opening a TOML path directly) so `--config` / `$PONTE_CONFIG` keep working.
  Every new `[section]` key needs a parser entry — a key the parser does not
  read is silently ignored, which is exactly the bug class fixed in 0.3.0.
- **Keep lint and types clean**: `ruff check .` and `mypy` must both pass;
  avoid adding `# noqa` / `# type: ignore` without a reason on the same line.

## Commit messages

Keep them concise and conventional; describe *why*, not just *what*:

```
type(scope): short summary

e.g. fix(daemon): fail loudly when service install is rejected
     feat(core): probe remote ports via python socket
     docs(readme): bilingual quick-start
```

Types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`.

## Releasing

Releases go out **only** through the tag-triggered `publish.yml` workflow. The
PyPI distribution is `ponte-cli`; the console command and the import package
stay `ponte`.

1. Bump `__version__` in `ponte/__init__.py` and add a CHANGELOG entry.
2. Merge that through a PR (`main` is protected).
3. Tag the merged commit and push the tag:
   `git tag -a v0.3.1 -m "v0.3.1: ..." && git push origin v0.3.1`.
4. The workflow builds the sdist/wheel, refuses a tag/version mismatch, installs
   the wheel and runs `ponte init`, then publishes over OIDC. Watch it under
   Actions, then write the GitHub Release from the CHANGELOG highlights.

Credentials live on PyPI, not in this repository: add a *pending* trusted
publisher with project name `ponte-cli`, owner `modusensus`, repository `ponte`,
workflow `publish.yml` and environment `pypi` — and keep the `pypi` environment
name in the workflow in sync with it.

## Before you open a change

1. `pytest` passes locally.
2. `ruff check .` and `mypy` are clean.
3. Coverage stays at/above the `fail_under` threshold.
4. No private keys or real endpoints in the diff.
5. If behaviour changed on a specific OS, say so in the description.
6. Behaviour changes get an entry in [CHANGELOG.md](CHANGELOG.md).

## Code of conduct

Be constructive. This is a small project — small, focused changes are easier
to review and land than large rewrites.

---

# 中文

## 环境准备

需要 **Python 3.11+**（使用内置 `tomllib`）。

```bash
git clone git@github.com:modusensus/ponte.git
cd ponte
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## 运行测试

```bash
pytest                          # 完整测试套件（config/core/daemon/health/main/retry）
pytest --cov=ponte --cov-report=term-missing   # 带覆盖率报告
ruff check .                    # 静态检查（必须干净）
mypy                            # 类型检查（必须干净）
python _smoke_test.py           # 零依赖冒烟检查
```

覆盖率在 `pyproject.toml` 里有 `fail_under` 阈值，请勿让它回落。CI 会在
Linux 上跑 lint + 类型检查，在 Windows / Linux / macOS × Python 3.11 / 3.12
上跑同一套测试，另有 `build` 任务会安装 wheel 并执行 `ponte init`。覆盖率
上报到 Codecov。

还有一个任务会用 `PONTE_TEST_THREAD_DELAY=0.15` 再跑一遍：这个变量让
`tests/conftest.py` 往工作线程的 sleep/wait 里注入延迟，于是“只有机器够快才
通过”的测试会**每次都**在那里失败，而不是偶发地红一次。怀疑是 runner 抽风之前，
先这样在本地跑一遍：

```bash
PONTE_TEST_THREAD_DELAY=0.15 pytest     # 模拟慢机器（Git Bash / POSIX 语法）
```

## 项目结构

```
ponte/
  __init__.py           # 版本号
  main.py               # typer CLI
  daemon.py             # 生命周期、服务安装/卸载、优雅停止
  retry.py              # 重连状态机（退避 + 抖动）
  core.py               # SSH 参数 / 子进程 / 端口探测
  health.py             # 周期检查
  config.py             # TOML 加载/校验 + 配置文件定位
  config.example.toml   # `ponte init` 使用的模板
tests/                  # pytest 测试套件（含 conftest.py fixtures）
_smoke_test.py          # 离线冒烟脚本
legacy/                 # 已废弃的 PowerShell/批处理脚本（仅存档）
CHANGELOG.md            # 各版本变更记录
```

## 约定

- **沿用现有风格**：纯 Python，公开方法带详细 docstring，文件头加
  `from __future__ import annotations`。
- **保持可离线测试**：测试套件不得依赖外网或真实 SSH 服务器。改动
  `core.py` / `daemon.py` 时，请用 mock `subprocess` 或假配置补/改测试。
- **跨平台意识**：不要硬编码 Windows 路径或 Linux 专属命令。平台差异走
  `sys.platform` 分支，运行时文件用 `config.py` 里的平台默认路径。
- **仓库不留密钥**：绝不提交 key、token、`.env` 或真实服务器地址。
  `config.example.toml` 里的占位符是有意保留的，真实配置存放在仓库之外。
- **配置读取统一走 `get_config()`**（不要自己打开某个 TOML 路径），
  否则 `--config` / `$PONTE_CONFIG` 会失效。新增 `[section]` 键必须同时
  补解析逻辑——解析器不读的键会被静默忽略，这正是 0.3.0 修掉的那类 bug。
- **保持 lint 与类型干净**：`ruff check .` 和 `mypy` 都必须通过；
  加 `# noqa` / `# type: ignore` 时请在同一行写明原因。

## 提交信息

简洁、符合常规格式，说明**为什么**而不只是**改了什么**：

```
type(scope): short summary

例如：fix(daemon): fail loudly when service install is rejected
     feat(core): probe remote ports via python socket
     docs(readme): bilingual quick-start
```

类型：`feat` / `fix` / `docs` / `test` / `refactor` / `chore`。

## 发布

发布**只**走 tag 触发的 `publish.yml` 工作流。PyPI 上的发行名是
`ponte-cli`；命令行与导入包名仍是 `ponte`。

1. 提升 `ponte/__init__.py` 里的 `__version__` 并补 CHANGELOG。
2. 通过 PR 合入（`main` 受保护）。
3. 在合并后的提交上打附注 tag 并推送：
   `git tag -a v0.3.1 -m "v0.3.1: ..." && git push origin v0.3.1`。
4. 工作流会构建 sdist/wheel、拒绝 tag 与包版本不一致的情况、装 wheel 跑
   `ponte init`，最后经 OIDC 发布到 PyPI。到 Actions 盯结果，
   再按 CHANGELOG 写 GitHub Release。

发布凭据配置在 PyPI 侧而非仓库里：添加一个 **pending** trusted publisher，
项目名 `ponte-cli`、owner `modusensus`、仓库 `ponte`、工作流 `publish.yml`、
环境 `pypi`，并让工作流里的 `pypi` 环境名与之保持一致。

## 提交前检查

1. `pytest` 本地通过。
2. `ruff check .` 与 `mypy` 干净。
3. 覆盖率不低于 `fail_under` 阈值。
4. diff 里没有私钥或真实端点。
5. 若某个 OS 上行为有变化，请在描述里说明。
6. 行为变更请在 [CHANGELOG.md](CHANGELOG.md) 补一条。

## 行为准则

请保持建设性。这是个小项目——小而聚焦的改动比大重写更容易评审与合入。
