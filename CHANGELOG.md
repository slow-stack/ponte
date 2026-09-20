# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **A console window could still flash on the stop path.** Every external
  control tool (`taskkill`, `systemctl`, `launchctl`) now goes through a single
  helper that applies `creation_flags()`. `taskkill` was the Windows offender:
  it ran bare, so `ponte stop` / `restart` popped a black console box while the
  escalated kill ran — precisely when the user asked for a quiet stop.
- **`ponte install` could silently register a popup-generating task.** When no
  `pythonw.exe` sat next to `sys.executable`, the Scheduled Task quietly pointed
  at `python.exe` and a console window appeared at every logon. Installation now
  refuses with an actionable message instead of installing a task that pops up.

### Changed

- **`ponte status --json` is now keyed by profile.** That contract was added in
  this same unreleased cycle, so nothing released depends on it: instead of one
  flat object of tunnel statistics the payload is
  `{running, pid, uptime_seconds, healthy, profiles: {<name>: {...}}}`, with
  `availability` spelled out per profile.
- **Duplicate listen ports are now a configuration error.** Every forward is
  established with `ExitOnForwardFailure=yes`, so a repeated `remote_port`, or
  an `-L`/`-D` pair sharing a local port, made OpenSSH drop the *whole*
  connection and put the tunnel into an endless reconnect loop. The config is
  now rejected up front, by a message that names both offending rules.
- **The PyPI distribution is `ponte-cli`.** The bare name `ponte` is already
  taken on PyPI by an unrelated project, so `pyproject.toml` declares
  `name = "ponte-cli"`. The import package (`ponte`), the console command
  (`ponte`) and the repository name are unchanged, so an install from a
  checkout behaves exactly as before.
- **The coverage gate moved from 70% to 80%.** The `[[profiles]]`, notify and
  doctor work pushed the suite past it (~84%), so the threshold in `pyproject.toml`
  now matches the codebase instead of trailing it by ten points.
- **The tunnel target is part of the status now.** Each profile's status
  carries the SSH `destination` it connects to (`ProfileStatus.destination`,
  taken from the config rather than the status file), shown as the first row of
  a `ponte status` table and included in `ponte status --json`. A table of
  numbers is useless if you cannot tell which server the broken one is; the
  dashboard labels every card with it.

### Added

- **Multiple SSH endpoints in one config: `[[profiles]]`.** A profile is a
  named SSH endpoint with its own key and its own `[[profiles.tunnels]]`, and a
  single daemon supervises every one of them concurrently — each with its own
  connection, reconnect budget, health monitor and section of the status file.
  A server that is down now backs off alone instead of taking the other tunnels
  with it, and a profile whose retry loop dies of an unexpected exception is
  recorded in the status file (and shown by `status`/`watch`) rather than dying
  silently. `ponte status` prints one table per profile (unchanged layout for a
  single tunnel), `ponte watch` one panel per profile, and `ponte test` /
  `ponte check` gained `--profile NAME` to target one of them. The pre-profile
  layout is still read as a single profile named `default`, including an
  already-written status file, so an in-place upgrade keeps its statistics.
- **`-L` (local) and `-D` (SOCKS5) forwarding, not just `-R`.** A `[[tunnels]]`
  rule now takes `kind = "remote" | "local" | "dynamic"` — `remote` is the
  default, so existing configs parse and behave exactly as before — and ponte
  emits the matching OpenSSH flag. The kinds can be mixed in one config and
  share a single SSH connection, which turns ponte from a reverse-tunnel
  script into a general forwarding tool. For `-L`/`-D` the bind address
  defaults to `127.0.0.1`, so an omitted field is never a LAN exposure; `-R`
  keeps omitting the server-side bind address unless `remote_host` is set
  explicitly (only meaningful with `GatewayPorts=yes`).
- **Health checks cover the local end too.** The listeners of `-L`/`-D`
  tunnels are probed with an in-process loopback connect on every health tick
  (no SSH connection, so it is free), and their state is persisted, rendered by
  `ponte status` / `ponte watch` and reported by `ponte check` next to the
  remote ports.
- **Tunnel statistics that separate "daemon alive" from "tunnel up".** The
  status file now carries cumulative counters — connection attempts,
  established sessions, scheduled reconnects, accumulated tunnel uptime and
  downtime, the current session's start time, and the last disconnect with
  its reason and timestamp. The counters survive daemon restarts (merged,
  not overwritten, when the service manager respawns the process), so a
  tunnel that flaps for hours no longer hides behind a healthy process
  uptime. `ponte status` shows the current session duration, session/reconnect
  counts and the last disconnect reason; `ponte status --json` emits the full
  snapshot for scripts and monitoring.
- **`ponte watch` — a live terminal dashboard.** A `rich.Live` view that
  refreshes in place: process health, current session duration, session
  statistics with availability, remote-port states, the last disconnect and a
  bounded feed of the most recent retry-loop events.
- `.github/workflows/publish.yml` — tag-triggered publishing to PyPI via Trusted
  Publishing (OIDC, no stored token). It refuses a tag that does not match the
  version in `ponte/__init__.py`, and verifies the wheel's distribution name and
  contents (`config.example.toml`) before uploading.
- **Out-of-band alerts when a tunnel really is down (`[notify]`).** A tunnel
  whose reconnect attempts keep failing no longer only writes to a log nobody
  reads: after `on_consecutive_failures` failed attempts in a row the daemon
  pushes to an ntfy topic and/or a JSON webhook. "Recovered" is judged by the
  same `[retry] stable_after` rule the reconnect budget uses, so a flapping
  tunnel re-arms and reports the next outage; `cooldown` rate-limits a single
  outage to one message per window, and at most one alert is attempted per
  outage, so a reconnect loop cannot become a flood of HTTP requests. Delivery
  failures are recorded (never raised) and surfaced by `ponte doctor`, and
  `ponte notify-test` sends a real test message so the channel can be proven
  before the outage that matters.
- **`ponte doctor` — one-command health checkup.** Walks the whole path a user
  actually hits: effective config (and its warnings), the `ssh` client, the
  identity file and its POSIX permissions, SSH reachability per profile, remote
  and local listen ports, daemon/service state and the notify channel. Every
  row carries a verdict plus a concrete fix (the command to run, the file to
  edit), `--offline` skips everything that needs the network, and the exit code
  is non-zero when anything failed, so it is usable from a script.
- `[windows] pythonw_exe` — pin the windowless interpreter for the Scheduled
  Task, for installs where `pythonw.exe` does not sit next to `python.exe`.
- **`ponte serve` — a local HTTP surface: dashboard, health probe and
  Prometheus metrics.** `/` renders a self-contained dashboard (inline CSS, no
  CDN, no JavaScript) with a card per tunnel: health, session age, availability,
  forwarded-port state, the last disconnect and its reason, and the event feed.
  `/healthz` is the endpoint to point a monitor at — it returns `503` when the
  daemon is down *or* a tunnel is broken, and `200` (with `"status":
  "starting"`) until the first health check completes, so a restart does not
  page anyone. `/metrics` speaks the Prometheus text format (session age,
  cumulative up/down time, availability, reconnect and port-listening state)
  and deliberately always answers `200`, because a scrape failure would hide
  *why* a tunnel went down — a graph's whole job. `/status.json` is exactly the
  `ponte status --json` payload, so the page, the probe and the metrics can
  never disagree with the CLI. Everything is read-only and re-read per request
  (`Cache-Control: no-store`), and the whole thing is stdlib `http.server` — no
  new dependency. Binds `127.0.0.1` by default; `[serve] host`/`port`/
  `token`/`refresh` configure it, and a non-loopback bind **without** a token is
  refused at config load and by `ponte serve` alike (the dashboard names your
  servers, users and ports), with clients then passing `?token=` or
  `Authorization: Bearer`.

### Planned

- `ruff format --check` in CI once the tree is formatted.

## [0.3.0] - 2026-09-12

Release focused on **making an installed copy actually usable**, plus the
config/CLI gaps that the README already promised but the code did not deliver.

### Fixed

- **Packaging: the shipped wheel could not work at all.** `config.toml` lived
  inside the package but was never declared as package data, so
  `pip install .` produced an install with no config file and every command
  failed with `ConfigNotFoundError`. The template now ships explicitly
  (`config.example.toml`), and CI installs the built wheel and runs
  `ponte init` to keep the regression from coming back.
- **`[retry] stable_after` and `[health] max_check_interval` were silently
  ignored.** Both are documented (and covered by tests at the dataclass level),
  but the TOML parsers never read them, so setting them had no effect.
- **The "force killed" notice was dead code.** `stop()` returned a status whose
  `message` could never contain a kill hint, so a `taskkill /F` escalation was
  reported as a clean stop. The message is now propagated.
- **`_smoke_test.py` had been broken since the health-backoff release** (it
  crashed with `AttributeError: '_RC' object has no attribute 'stable_after'`)
  even though the README advertised it. Its hand-written config stubs are now
  in sync, and it exercises the interval backoff deterministically.
- `ponte stop` / `restart` no longer hide a force kill; `ponte init` no longer
  swallows `[ssh]` as a Rich markup tag.
- `_optional_str` is overloaded, so a caller passing a real default no longer
  has to narrow `str | None` (three latent `arg-type` errors).

### Changed

- **The config file moved out of the package** to the per-user config dir
  (`%APPDATA%\ponte\config.toml`, `~/.config/ponte/config.toml`,
  `~/Library/Application Support/ponte/config.toml`). `pip install -U` no
  longer overwrites your settings. Resolution order: `--config` →
  `$PONTE_CONFIG` → user config dir → the legacy in-package path (still
  honoured, logged as deprecated). The working directory is deliberately not
  searched, since services have arbitrary `cwd`s.
- New `ponte init [--path P] [--force]`: writes the config from the template,
  migrates an existing in-package file, and never overwrites without `--force`.
- New global options `--config/-c PATH` and `--version/-V`.
- Unknown/typo'd config keys are now reported instead of being dropped
  silently: `ponte config` prints them and `load_config()` logs them.
- Service installs now launch the daemon with an explicit `--config <file>`, so
  a SYSTEM Scheduled Task reads your config rather than its own `%APPDATA%`.
- `[windows] run_as` now defaults to `user`. The old `system` default could not
  read `~/.ssh` at all, i.e. the default combination was broken out of the box.
- The daemon's working directory is now the config file's directory instead of
  the package's parent (`site-packages` after a normal install).
- Removed the hardcoded `D:\Git\usr\bin\ssh.exe` fallback in favour of
  environment-derived Git / Windows-OpenSSH locations.
- `ponte config` also shows `stable_after`, `max_check_interval` and
  `windows.run_as`.
- Coverage gate raised from 55% to 70%.

### Added

- `ruff` + `mypy` in CI (and as dev dependencies); both are clean.
- A `build` CI job that builds the sdist/wheel, asserts the example config is
  inside the wheel, installs it and exercises `ponte init`.
- `tests/conftest.py` with an autouse fixture that isolates the config
  override, `$PONTE_CONFIG` and the config cache between tests.
- Tests for the new config resolution, `ponte init`, unknown-key warnings,
  service unit generation (systemd/launchd) and the force-kill message.
- `legacy/` — the pre-Python PowerShell/batch/shell scripts, marked deprecated,
  with a command mapping. `setup.ps1` copied your private key into the project
  directory; that logic has been deleted.
- This changelog, project URLs/metadata, and lower bounds on dependencies.

### Migration

```bash
ponte init       # migrates the in-package config if present
ponte install    # re-register the service so it passes --config
```

## [0.2.1]

Tunnel stability fixes (PR #1): a "zombie" SSH process (alive but ports down)
is force-reconnected after 3 consecutive failed health checks; a session that
stays up ≥ `stable_after` seconds resets the retry budget; health-check
intervals back off exponentially during outages (`max_check_interval`); SSH
stderr is read in real time. New `[retry] stable_after` and
`[health] max_check_interval` options.

## [0.2.0]

- `windows.run_as` — choose the Scheduled Task identity/timing: `user`
  (logon) or `system` (boot).
- The SSH console window is suppressed on Windows.

## [0.1.0]

Initial release of the Python CLI: retry loop with exponential backoff +
jitter, health checks, auto-start services for Windows (Scheduled Task),
Linux (systemd user) and macOS (launchd), cross-platform `ssh` resolution,
and the pytest suite with CI/Codecov.

[Unreleased]: https://github.com/modusensus/ponte/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/modusensus/ponte/releases/tag/v0.3.0
[0.2.1]: https://github.com/modusensus/ponte/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/modusensus/ponte/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/modusensus/ponte/releases/tag/v0.1.0
