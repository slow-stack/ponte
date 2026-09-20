# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **A failed health probe was reported as a dead port — and could kill a healthy
  tunnel.** The server-side probe is an SSH connection of its own, and when that
  connection failed (a reset, provider-side rate limiting, our own timeout kill)
  every configured port was still reported as *not listening*. Two things
  followed: `ponte status` showed `未监听`, and `_on_health` counted the tick
  towards its three-consecutive-failures threshold, so the monitor force-killed
  a session that was forwarding traffic perfectly well and made the retry loop
  reconnect it — the flakier the path, the more often. Measured on a shared
  uplink, roughly a third of probe ticks failed that way. "The probe never got
  to ask" is now kept apart from "the probe asked and the answer was no":
  `TunnelManager.check_remote_ports()` raises `ProbeError`, and the health
  snapshot marks the result inconclusive (`HealthStatus.conclusive`) with the
  reason instead of inventing port states. An inconclusive tick is not counted
  towards the forced-reconnect threshold (the counter is left untouched, so a
  real zombie is still caught across interleaved probe failures), `ponte status`
  and the dashboard show `未知` with the probe's reason, `ponte doctor` warns
  instead of failing, `ponte check` says `未知` for the affected profile without
  hiding the others, and `/healthz` answers `200 unverified` with an `unknown`
  list (a new `ponte_profiles_unknown` metric) rather than `503 degraded`.
  `healthy: false` with `health_conclusive: false` in `status --json` is the
  machine-readable form of that distinction.
- **The health monitor could freeze forever on Windows — which disabled the one
  recovery that saves a dead tunnel.** The SSH child was spawned with
  `close_fds=False` (Windows has no close-on-exec, so this was meant to keep
  `CREATE_NO_WINDOW` working), which handed that long-lived process a copy of
  every inheritable handle — including the stdout pipe of the *following* health
  probe. When the probe hit its timeout, `subprocess.run` killed it and then
  blocked in `communicate()` waiting for a pipe whose write end the live SSH
  session still held open, so the health thread never came back: `ponte status`
  sat on its last reading ("异常") for hours while the tunnel was fine, and the
  zombie-session force-reconnect — which runs off health ticks — never fired at
  all. The SSH child now closes descriptors as on POSIX, and probes read through
  a helper that bounds the wait twice and reports a wedged probe as failed
  rather than hanging its caller.
- **A broken config could leave you unable to stop the daemon.** `ponte stop`,
  `status`, `logs` and `watch` all built their daemon handle from the validated
  config, so one typo in a tunnel rule made every one of them fail with a
  config error — including `stop`, which is exactly the command you need when
  something is wrong. They now fall back to a minimal handle carrying only the
  `[daemon]` pid/log paths, recovered from the raw TOML when the file still
  parses and from the platform defaults when it does not; the fallback is
  announced on stderr, so `status --json` still emits clean JSON. `start`,
  `restart` and `install` keep requiring a valid config on purpose — and
  `restart` validates *before* stopping anything, so a bad edit can no longer
  leave a tunnel stopped and unrestartable.
- **A console window could still flash on the stop path.** Every external
  control tool (`taskkill`, `systemctl`, `launchctl`) now goes through a single
  helper that applies `creation_flags()`. `taskkill` was the Windows offender:
  it ran bare, so `ponte stop` / `restart` popped a black console box while the
  escalated kill ran — precisely when the user asked for a quiet stop.
- **`ponte install` could silently register a popup-generating task.** When no
  `pythonw.exe` sat next to `sys.executable`, the Scheduled Task quietly pointed
  at `python.exe` and a console window appeared at every logon. Installation now
  refuses with an actionable message instead of installing a task that pops up.
- **A flapping tunnel was reported as 100% available.** `status --json` rounded
  `availability` to one decimal *as a 0..1 ratio*, so 97.9% became `1.0` — and
  that field is what the dashboard, `/status.json` and the Prometheus
  `ponte_profile_availability_ratio` gauge all read, while `ponte status` printed
  the truthful number from the unrounded value. The ratio is now rounded to
  three decimals, which keeps the tenth of a percent those surfaces display.

### Security

- **A hostname that merely started with `127.` skipped the token requirement.**
  Whether `serve.host` may be bound without a `serve.token` is decided by
  `is_loopback_host`, which accepted any spelling beginning with `127.` — and
  that also matches *names*: `127.corp.example` is a perfectly legal hostname
  that resolves wherever its owner points it, so configuring it exposed the
  dashboard (server addresses, login users, forwarded ports) to a public
  address with no token, which is the one thing `ensure_bindable` exists to
  prevent.  The address is now parsed with `ipaddress` and judged by
  `is_loopback`, with `localhost` the only accepted name — and a v4-mapped
  address (`::ffff:127.0.0.1`) is judged by the IPv4 address it stands for,
  since `IPv6Address.is_loopback` only learned those forms in 3.12 (the CI
  matrix had 3.11 demanding a token for the same spelling). Spellings the OS
  accepts but that parser rejects — the shorthand `127.1`, the absolute form
  `localhost.` — now count as exposed, so the remaining error is "demanded a
  token it did not need" rather than "skipped the token it did need".
- **Request lines reached the log verbatim.** `log_message` — and therefore
  `log_error`, which funnels through it — forwarded the raw request line into
  the log record. `BaseHTTPRequestHandler` decodes that line as latin-1, so a
  client can put ESC, NUL, DEL, C1 bytes or bidi overrides in it: enough to make
  `ponte serve`'s log claim something that never happened, or to make a terminal
  tailing it render output it never received. Untrusted text is now neutralised
  before logging, each non-printable character becoming a visible `?` (kept
  rather than deleted, so an attempt leaves a trace instead of vanishing), and
  the line is capped at 500 characters so a 64 KiB request line cannot become a
  64 KiB log line.

### Changed

- **Every SSH path now builds its connection flags in one place.** The tunnel,
  the login test behind `ponte test` / the health loop / `doctor`, and the
  server-side port probe each assembled their own `-o`/`-i`/`-p` list, so a
  setting could reach the tunnel but not the checks that supervise it — a
  mismatch that would have reported a perfectly healthy tunnel as dead. They now
  share one builder, which is what makes adding `-J` safe.
- **`[ssh]` may now defer to `~/.ssh/config`.** `user` and `identity_file` are
  optional; only `host` is required. When either is omitted ponte no longer
  forces `user@` / `-i` onto the command line, so OpenSSH resolves the user and
  the key itself — from a `Host` alias, `User`, `IdentityFile` or an ssh-agent
  identity. A machine whose plain `ssh myserver` already works no longer has to
  duplicate that into ponte's config, and ponte stops overriding an
  `IdentityFile` set in the SSH config. An explicitly configured
  `identity_file` must still exist (unchanged), so a typo cannot silently fall
  back to a different key.
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
  dashboard labels every row with it.
- **The dashboard is one row per tunnel, not one card per tunnel.** Three
  tunnels already needed a scroll, and the first requirement of a status page is
  "see everything at once". Each row now carries the name and verdict, the
  destination, the jump chain, the forwarded ports as chips, and one line of
  session/availability/disconnect facts; the statistics table and the event feed
  moved into the row's own `<details>` disclosure, so nothing was dropped — it is
  only deferred. Port chips carry a glyph and a side label (`远程`/`本地`) rather
  than relying on colour, and a row that is *unknown* shows `未观测` for the group
  the probe never reached instead of a red `未监听`. The pill, the row's colour
  bar and the header count all read one verdict helper, so they cannot disagree
  about which tunnels are in trouble. The header also became a count of what is
  actually wrong (a `0` tile is not rendered at all), and the page stopped being
  dark-only: it now ships the light palette it had always advertised with
  `<meta name="color-scheme" content="dark light">`, draws availability as a
  small bar next to its digits, and uses tabular numerals so a number changing
  under a refresh does not reflow the row it is in. (While building it: the
  disclosure caret was written as a CSS `\25be` escape inside an ordinary Python
  string, where Python reads `\25` as an *octal* escape — the caret rendered as a
  control character.)
- **The dashboard refreshes in place instead of reloading itself under you.**
  The auto-refresh was a `<meta http-equiv="refresh">`, so every tick threw away
  the page you were reading: an expanded row snapped shut, the scroll position
  reset, and clicking a row could be undone before you finished reading it (on a
  slow link it was worse than manual refreshing). The page is still rendered
  complete on the server and still runs no script by default — the meta refresh
  now lives inside `<noscript>`, so scripting off keeps exactly the old
  behaviour, and a scripting browser never even creates that element. When a
  script *is* running it fetches the same HTML and swaps the summary and the
  board in place: expanded rows stay expanded (matched by profile name), the
  scroll position does not move, and a row whose verdict changed since the
  previous tick flashes, because a tunnel that dies between two refreshes should
  be noticed rather than read as "it was always like that". It re-uses the
  server's own rendering on purpose — a second renderer written in JavaScript is
  exactly how a dashboard starts disagreeing with `ponte status`. The footer now
  says which state it is in (`已更新 12:34:56 · 每 5 秒`, `已暂停`, or
  `连接中断（第 N 次），仍在重试` instead of quietly showing a stale reading), a
  `暂停` button stops polling while you read, polling stops by itself while the
  tab is hidden, and a `401` says so instead of retrying forever.

### Added

- **The jump chain is part of the status, next to the destination.**
  `ProfileStatus.jump` carries the `ssh -J` value, so `ponte status --json` and
  the dashboard can tell "cannot reach the server" apart from "cannot reach the
  bastion" — the two failures ssh reports with the same message. The dashboard
  prints `↳ 经 ops@bastion:2222` in amber on the row itself, because a bastion is
  the link that fails first and the one nothing else in the config names.
- **Jump hosts are a first-class setting: `[ssh] jump`.** "The server is only
  reachable through the bastion" is the most common real topology this kind of
  tool is pointed at, and it used to be unsupported in any discoverable way:
  hand-writing `ProxyJump` into `[ssh.options]` worked, but nothing validated
  it, `ponte config` did not show it, and `ponte doctor` could not tell "the
  bastion is down" from "your key is wrong". The value keeps OpenSSH's own
  `ProxyJump` syntax — `[user@]host[:port]`, comma separated for a chain — and
  is passed to `ssh -J` verbatim, so there is nothing new to learn and the hop's
  identity comes from `~/.ssh/config` just like the destination's (no second
  `identity_file` to keep in sync). `proxy_jump` is accepted as an alias, hops
  are validated at load time (bad port, empty hop, stray whitespace), and a
  `jump` combined with a `ProxyJump`/`ProxyCommand` in `[ssh.options]` is
  rejected — those describe the same hop, and ssh would silently apply only one.
  `ponte config` shows the chain, `ponte config --ssh-command` shows the `-J`
  it produces, and `ponte doctor` gained a jump-host row that TCP-probes the *first*
  hop (the only one reachable from here — probing later hops would fail on a
  healthy chain) before reporting connectivity, so a dead bastion is named as
  the cause rather than surfacing as a login failure. When a jump is configured,
  the connectivity hint tells you to test `ssh <first hop>` instead of sending
  you to `authorized_keys` on a server you cannot reach anyway.
- **`ponte reload` — apply a new config without dropping healthy tunnels.**
  Re-reads the config file and restarts *only* the profiles whose settings
  actually changed: a new profile is started, a removed one is stopped (and its
  stale status section dropped), an unchanged one is left running. Because
  `[retry]`/`[health]` are baked into a runner at construction time, a change to
  either rebuilds every profile — but a pure tunnel edit no longer costs the
  other connections. The request travels through a reload marker file (the same
  cross-process mechanism as the stop marker), so it works on Windows too, with
  `kill -HUP` as the POSIX equivalent. A config that fails to parse is rejected
  *before* the daemon sees it — `ponte reload` validates it locally and reports
  the error, and the running tunnels keep going.
- **`ponte doctor --json`.** The same checkup as the table, as a stable JSON
  object (`ok`, `counts`, `checks`), with the exit code still non-zero when
  anything failed — so CI can store the report and gate on it in the same run.
- **`ponte config --ssh-command`.** Prints the exact `ssh` argv ponte will
  execute, one shell-quoted line per profile — the fastest way to answer "what
  is it actually running?", especially now that `user`/`identity_file` may be
  left to OpenSSH.
- **Shell completion.** `ponte --install-completion` now installs completion
  for bash / zsh / fish / PowerShell (`add_completion` was off before).
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
