"""Command-line interface for ponte — an SSH reverse tunnel manager.

Typical usage::

    ponte start          # launch the tunnel daemon in the background
    ponte status         # inspect daemon health and remote ports
    ponte logs -f        # follow the daemon log
    ponte install        # register a Windows scheduled task (auto-start)
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import typer
from rich.console import Console, RenderableType
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from ponte import __version__
from ponte.config import (
    ConfigError,
    ServeConfig,
    get_config,
    init_config,
    set_config_path,
)
from ponte.daemon import _format_duration
from ponte.doctor import FAIL, OK, SKIP, WARN, counts, run_checks
from ponte.serve import create_server, serve_url

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, runtime import is lazy
    from ponte.daemon import TunnelDaemon

__all__ = ["app"]


def _configure_utf8_stdio() -> None:
    """Force UTF-8 on stdio so Chinese output renders in UTF-8 terminals.

    On Windows, Python defaults stdout encoding to the ANSI code page (e.g.
    GBK on Chinese systems), which garbles Chinese text in UTF-8 terminals
    such as Windows Terminal, mintty and VS Code.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


_configure_utf8_stdio()


app = typer.Typer(
    no_args_is_help=True,
    invoke_without_command=True,
    help="管理 SSH 反向隧道守护进程的命令行工具",
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


@app.callback()
def main(
    ctx: typer.Context,
    config_file: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        metavar="PATH",
        help="使用的配置文件（默认按 PONTE_CONFIG → 用户配置目录 → 包内旧位置顺序查找）",
    ),
    version: bool = typer.Option(
        False, "--version", "-V", help="显示版本号并退出", is_eager=True
    ),
) -> None:
    """全局选项：``--version`` 与 ``--config``。"""
    if version:
        console.print(f"ponte {__version__}")
        raise typer.Exit()
    if config_file is not None:
        set_config_path(config_file)
        if ctx.invoked_subcommand is None:
            # 只给了全局选项、没给子命令：打印帮助而不是静默退出。
            console.print(ctx.get_help())
            raise typer.Exit()

#: Tokens inside a DaemonStatus.message that indicate a force kill was needed.
_FORCE_KILL_TOKENS = ("kill", "force", "强制", "强杀")

#: Poll interval (seconds) used by --follow when no new log data arrives.
_FOLLOW_POLL_INTERVAL = 0.25


def _daemon() -> TunnelDaemon:
    """Return a daemon instance bound to the effective configuration.

    The ``ponte.daemon`` module is imported lazily so that config-only
    commands (``config``, ``--help``) keep working even when the daemon
    module is unavailable or out of date.
    """
    from ponte.daemon import TunnelDaemon

    return TunnelDaemon(config=get_config())


def _fail(message: str) -> NoReturn:
    """Print a red ``错误：`` message to stderr and exit with status 1."""
    err_console.print(f"[bold red]错误：{escape(message)}[/bold red]")
    raise typer.Exit(code=1)


def _force_kill_message(status: object) -> str:
    """Return ``status.message`` if it mentions a force kill, otherwise ''."""
    message = getattr(status, "message", None) or ""
    lowered = message.lower()
    if any(token in lowered for token in _FORCE_KILL_TOKENS):
        return message
    return ""


# ---------------------------------------------------------------------------
# start / stop / restart
# ---------------------------------------------------------------------------


@app.command()
def start(
    foreground: bool = typer.Option(
        False, "--foreground", "-f", help="前台运行（阻塞，Ctrl+C 停止）"
    ),
) -> None:
    """启动反向隧道守护进程。"""
    try:
        daemon = _daemon()
        status = daemon.status()
        if status.running:
            pid = status.pid if status.pid is not None else "?"
            console.print(f"[yellow]已在运行 (pid {pid})[/yellow]")
            raise typer.Exit(code=0)

        if foreground:
            try:
                code = daemon.run()
            except (KeyboardInterrupt, typer.Abort):
                console.print("\n[yellow]已停止[/yellow]")
                raise typer.Exit(code=0) from None
            raise typer.Exit(code=code or 0)

        pid = daemon.start()
        console.print(f"[green]已启动，pid {pid}[/green]")
        console.print("[dim]可运行 ponte status 查看健康[/dim]")
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


@app.command()
def stop() -> None:
    """停止反向隧道守护进程。"""
    try:
        daemon = _daemon()
        if not daemon.status().running:
            console.print("[grey]未运行[/grey]")
            raise typer.Exit(code=0)
        result = daemon.stop()
        console.print("[green]已停止[/green]")
        kill_msg = _force_kill_message(result)
        if kill_msg:
            console.print(f"[yellow]{escape(kill_msg)}[/yellow]")
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


@app.command()
def restart() -> None:
    """重启反向隧道守护进程（先停止，再以后台模式启动）。"""
    try:
        daemon = _daemon()
        if daemon.status().running:
            result = daemon.stop()
            kill_msg = _force_kill_message(result)
            if kill_msg:
                console.print(f"[yellow]{escape(kill_msg)}[/yellow]")
            else:
                console.print("[grey]已停止旧进程[/grey]")
        pid = daemon.start()
        console.print(f"[green]已重启，pid {pid}[/green]")
        console.print("[dim]可运行 ponte status 查看健康[/dim]")
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


# ---------------------------------------------------------------------------
# status / logs
# ---------------------------------------------------------------------------


@app.command()
def status(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="以 JSON 输出（供脚本 / 监控系统消费）",
    ),
) -> None:
    """查看守护进程与各隧道健康状态（每条隧道一行）。"""
    try:
        s = _daemon().status()
        if not s.running:
            if json_output:
                console.print_json(json.dumps({"running": False}))
            else:
                console.print("[grey]未运行（可 ponte start 启动）[/grey]")
            raise typer.Exit(code=0)

        if json_output:
            console.print_json(json.dumps(_status_payload(s), ensure_ascii=False))
            return

        # 单隧道配置保持原有的一表格布局；多隧道时守护进程信息单独一张表，
        # 每条隧道各一张，避免把两条连接的状态挤进一列。
        if len(s.profiles) <= 1:
            table = _status_table("ponte 状态")
            _add_daemon_rows(table, s)
            if s.profiles:
                _add_profile_rows(table, s.profiles[0])
            if s.message:
                table.add_row("备注", escape(s.message))
            console.print(table)
            return

        header = _status_table("ponte 守护进程")
        _add_daemon_rows(header, s)
        header.add_row("隧道数", str(len(s.profiles)))
        console.print(header)
        for profile in s.profiles:
            table = _status_table(f"隧道 {profile.name}")
            _add_profile_rows(table, profile)
            console.print(table)
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


def _status_table(title: str) -> Table:
    """A two-column key/value table used by ``status``."""
    table = Table(title=title, header_style="bold cyan")
    table.add_column("项目", no_wrap=True, style="cyan")
    table.add_column("值")
    return table


def _markup_health(healthy: bool | None, error: str | None) -> str:
    """Render a health flag, with the probe error when there is one."""
    if healthy is True:
        return "[green]健康[/green]"
    if healthy is None:
        return "[yellow]未知[/yellow]"
    detail = escape(error or "")
    return "[red]异常[/red]" + (f"（{detail}）" if detail else "")


def _markup_port(ok: bool) -> str:
    return "[green]监听中[/green]" if ok else "[red]未监听[/red]"


def _add_daemon_rows(table: Table, s) -> None:  # noqa: ANN001 - DaemonStatus cycle guard
    """Append process-level rows (shared by every profile)."""
    table.add_row("PID", str(s.pid) if s.pid is not None else "—")
    table.add_row("运行时长", s.uptime)


def _add_profile_rows(table: Table, profile) -> None:  # noqa: ANN001 - cycle guard
    """Append one profile's health, statistics and port states to *table*."""
    # 目标放在第一行：多条隧道时，最先要说清的是“这张表是哪个服务器”。
    if profile.destination:
        table.add_row("目标", escape(profile.destination))
    table.add_row("健康状态", _markup_health(profile.healthy, profile.health_error))

    # 会话时长是区分“守护进程活了多久”与“隧道活了多久”的那一列。
    if profile.current_session_at is not None:
        table.add_row(
            "当前会话时长",
            _format_duration(time.time() - profile.current_session_at),
        )

    if profile.sessions_total is not None:
        availability = profile.availability
        stats = f"会话 {profile.sessions_total} 次 · 重连 {profile.reconnects_total} 次"
        if profile.tunnel_uptime_seconds is not None:
            stats += " · 在线率 " + (
                "—" if availability is None else f"{availability * 100:.1f}%"
            )
        if profile.last_disconnect_reason:
            stats += f" · 上次断线：{profile.last_disconnect_reason}"
        table.add_row("隧道统计", escape(stats))

    for port, ok in sorted(profile.remote_ports.items()):
        table.add_row(f"远程端口 {port}", _markup_port(ok))
    for port, ok in sorted(profile.local_ports.items()):
        table.add_row(f"本地端口 {port}", _markup_port(ok))

    if profile.last_notification_at is not None:
        ago = _format_duration(time.time() - profile.last_notification_at)
        table.add_row("上次通知", f"{ago}前")
    if profile.error:
        table.add_row("错误", f"[red]{escape(profile.error)}[/red]")


def _round1(value: float | None) -> float | None:
    """Round a stat for JSON output, passing ``None`` through."""
    return None if value is None else round(value, 1)


def _profile_payload(profile) -> dict:  # noqa: ANN001 - ProfileStatus cycle guard
    """Machine-readable snapshot of one profile (the ``--json`` contract)."""
    return {
        "destination": profile.destination,
        "healthy": profile.healthy,
        "process_alive": profile.process_alive,
        "health_error": profile.health_error,
        "error": profile.error,
        "remote_ports": {str(p): ok for p, ok in profile.remote_ports.items()},
        "local_ports": {str(p): ok for p, ok in profile.local_ports.items()},
        "connect_attempts_total": profile.connect_attempts_total,
        "sessions_total": profile.sessions_total,
        "reconnects_total": profile.reconnects_total,
        "tunnel_uptime_seconds": _round1(profile.tunnel_uptime_seconds),
        "tunnel_downtime_seconds": _round1(profile.tunnel_downtime_seconds),
        "availability": _round1(profile.availability),
        "current_session_at": profile.current_session_at,
        "last_disconnect_at": profile.last_disconnect_at,
        "last_disconnect_reason": profile.last_disconnect_reason,
        "last_notification_at": profile.last_notification_at,
        "recent_events": profile.recent_events,
    }


def _status_payload(s) -> dict:  # noqa: ANN001 - DaemonStatus cycle guard
    """Whole-daemon snapshot: process facts plus a map of profile → stats."""
    return {
        "running": True,
        "pid": s.pid,
        "started_at": s.started_at,
        "uptime_seconds": round(s.uptime_seconds, 1),
        "healthy": s.healthy,
        "profiles": {
            profile.name: _profile_payload(profile) for profile in s.profiles
        },
    }


def _follow_log(path: str, start_offset: int) -> None:
    """Poll *path* for new content starting at *start_offset* until stopped."""
    offset = start_offset
    while True:
        try:
            size = os.path.getsize(path)
        except OSError:
            console.print("[yellow]日志文件已消失[/yellow]")
            break
        if size < offset:
            # The log was rotated/truncated: tail the new file from the start.
            offset = 0
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
        if data:
            offset += len(data)
            console.print(data.decode("utf-8", errors="replace"), end="", markup=False)
        else:
            time.sleep(_FOLLOW_POLL_INTERVAL)


@app.command()
def logs(
    lines: int = typer.Option(20, "--lines", "-n", min=1, help="显示日志最后 N 行"),
    follow: bool = typer.Option(False, "--follow", "-f", help="持续跟随输出新增日志"),
) -> None:
    """查看守护进程日志（默认只看尾部，-f 跟随）。"""
    try:
        log_file = _daemon().log_file
        if not os.path.isfile(log_file):
            console.print("[yellow]尚无日志（daemon 从未启动？）[/yellow]")
            raise typer.Exit(code=0)

        with open(log_file, "rb") as fh:
            raw = fh.read()
        offset = len(raw)
        content = raw.decode("utf-8", errors="replace")
        text_lines = content.splitlines()
        for line in text_lines[-lines:]:
            console.print(line, markup=False)
        if not follow:
            raise typer.Exit(code=0)

        try:
            _follow_log(log_file, offset)
        except KeyboardInterrupt:
            console.print("\n[yellow]已停止跟随[/yellow]")
            raise typer.Exit(code=0) from None
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


# ---------------------------------------------------------------------------
# watch（实时看板）
# ---------------------------------------------------------------------------


def _profile_border(profile) -> str:  # noqa: ANN001 - ProfileStatus cycle guard
    """Panel border colour for one profile's health flag."""
    if profile.healthy is True:
        return "green"
    if profile.healthy is None:
        return "yellow"
    return "red"


def _render_profile_feed(profile) -> RenderableType:  # noqa: ANN001 - cycle guard
    """The bounded retry-event feed of one profile."""
    feed = Table(
        title="最近事件",
        title_style="dim",
        show_header=False,
        padding=(0, 1),
    )
    feed.add_column(style="dim", no_wrap=True)
    feed.add_column()
    for e in reversed(profile.recent_events[-8:]):
        etype = str(e.get("type", "?"))
        icon = {
            "connecting": "[dim]→[/dim]",
            "connected": "[green]●[/green]",
            "disconnected": "[red]●[/red]",
            "retrying": "[yellow]↻[/yellow]",
            "max_retries_reached": "[red]✗[/red]",
        }.get(etype, "·")
        stamp = time.strftime("%H:%M:%S", time.localtime(e.get("at", 0)))
        text = etype
        if e.get("reason"):
            text = f"{etype}: {e['reason']}"
        elif e.get("attempt"):
            text = f"{etype}: 第 {e['attempt']} 次，{e.get('delay', 0):.1f}s 后重试"
        feed.add_row(f"{stamp}", f"{icon} {escape(text)}")
    return feed


def _render_profile_watch(profile) -> RenderableType:  # noqa: ANN001 - cycle guard
    """One profile's dashboard block: statistics grid + event feed."""
    table = Table.grid(padding=(0, 2))
    table.add_row("健康状态", _markup_health(profile.healthy, profile.health_error))

    if profile.current_session_at is not None:
        table.add_row(
            "当前会话", _format_duration(time.time() - profile.current_session_at)
        )
    else:
        table.add_row("当前会话", "[red]已断开[/red]")

    if profile.sessions_total is not None:
        availability = profile.availability
        avail = "—" if availability is None else f"{availability * 100:.1f}%"
        table.add_row(
            "会话统计",
            f"会话 {profile.sessions_total} · 重连 {profile.reconnects_total} · 在线率 {avail}",
        )

    for port, ok in sorted(profile.remote_ports.items()):
        table.add_row(f"远程端口 {port}", _markup_port(ok))
    for port, ok in sorted(profile.local_ports.items()):
        table.add_row(f"本地端口 {port}", _markup_port(ok))

    if profile.last_disconnect_reason:
        since = ""
        if profile.last_disconnect_at is not None:
            since = f"（{_format_duration(time.time() - profile.last_disconnect_at)}前）"
        table.add_row("上次断线", escape(profile.last_disconnect_reason) + since)

    if profile.last_notification_at is not None:
        table.add_row(
            "上次通知", f"{_format_duration(time.time() - profile.last_notification_at)}前"
        )

    if profile.error:
        table.add_row("错误", f"[red]{escape(profile.error)}[/red]")

    body = Table.grid()
    body.add_row(table)
    body.add_row(_render_profile_feed(profile))
    return body


def _render_watch(s) -> RenderableType:  # noqa: ANN001 - DaemonStatus cycle guard
    """Render one dashboard frame from a :class:`~ponte.daemon.DaemonStatus`.

    A single-profile config keeps the original one-panel layout; with several
    profiles each one gets its own panel, so two tunnels can be compared
    side by side instead of being flattened into one set of numbers.
    """
    if not s.running:
        grid = Table.grid(padding=(0, 2))
        grid.add_row("[red]守护进程未运行[/red]（可 ponte start 启动）")
        return Panel.fit(grid, title="ponte watch", border_style="red")

    body = Table.grid()
    multiple = len(s.profiles) > 1
    for profile in s.profiles:
        block: RenderableType = _render_profile_watch(profile)
        if multiple:
            block = Panel(
                block,
                title=f"隧道 {profile.name}",
                border_style=_profile_border(profile),
            )
        body.add_row(block)

    return Panel(
        body,
        title=f"ponte watch — pid {s.pid}",
        border_style="green" if s.healthy else "yellow",
    )


@app.command()
def watch(
    interval: float = typer.Option(
        2.0, "--interval", min=0.5, help="刷新间隔（秒）"
    ),
) -> None:
    """实时看板：在终端里持续刷新隧道健康与会话统计。"""
    daemon = _daemon()
    try:
        with Live(
            _render_watch(daemon.status()),
            console=console,
            refresh_per_second=2,
        ) as live:
            while True:
                time.sleep(interval)
                live.update(_render_watch(daemon.status()))
    except KeyboardInterrupt:
        raise typer.Exit(code=0) from None
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


# ---------------------------------------------------------------------------
# serve（本地 HTTP 看板 / 指标）
# ---------------------------------------------------------------------------


def _serve_config(
    base: ServeConfig,
    *,
    host: str | None,
    port: int | None,
    token: str | None,
    refresh: int | None,
) -> ServeConfig:
    """Apply ``ponte serve``'s command-line overrides to the ``[serve]`` section."""
    return dataclasses.replace(
        base,
        host=base.host if host is None else host,
        port=base.port if port is None else port,
        token=base.token if token is None else token,
        refresh=base.refresh if refresh is None else refresh,
    )


@app.command()
def serve(
    host: str | None = typer.Option(
        None, "--host", help="监听地址（默认取 [serve].host，也就是只监听本机）"
    ),
    port: int | None = typer.Option(
        None, "--port", min=1, max=65535, help="监听端口（默认取 [serve].port）"
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        help="访问令牌；绑定非回环地址时必须提供（写在命令行上会进 shell 历史，"
        "长期使用建议写进 [serve].token）",
    ),
    refresh: int | None = typer.Option(
        None, "--refresh", min=1, help="看板自动刷新间隔（秒）"
    ),
    open_browser: bool = typer.Option(False, "--open", help="启动后在浏览器里打开看板"),
) -> None:
    """启动本地 HTTP 服务：看板 / 、探活 /healthz、指标 /metrics、快照 /status.json。"""
    try:
        daemon = _daemon()
        effective = _serve_config(
            daemon.config.serve, host=host, port=port, token=token, refresh=refresh
        )
        # 与配置文件走同一条校验：绑定非回环地址却没有令牌时直接拒绝，
        # 不提供“先跑起来再说”的选项。
        effective.check_bind()
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))

    try:
        server = create_server(
            lambda: _status_payload(daemon.status()),
            host=effective.host,
            port=effective.port,
            token=effective.token,
            refresh=effective.refresh,
        )
    except OSError as exc:
        _fail(
            f"无法监听 {effective.host}:{effective.port}（{exc}）；"
            "换一个端口：ponte serve --port 8788"
        )

    url = serve_url(effective.host, effective.port)
    console.print(f"[green]ponte 看板已启动：{url}[/green]")
    console.print(
        f"[dim]指标 {url}metrics · 探活 {url}healthz · 快照 {url}status.json[/dim]"
    )
    if not effective.loopback:
        console.print(
            "[yellow]警告：已绑定非回环地址，同网段里拿到令牌的人都能看到你的服务器、"
            "用户与端口；不要暴露到公网[/yellow]"
        )
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        console.print("\n[yellow]已停止看板[/yellow]")
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# test / check
# ---------------------------------------------------------------------------


@app.command()
def test(
    timeout: int = typer.Option(10, "--timeout", help="连接测试超时（秒）"),
    profile: str | None = typer.Option(
        None, "--profile", "-P", help="只测试指定 profile（默认逐条全部测试）"
    ),
) -> None:
    """测试到 SSH 服务器的连接是否正常。"""
    try:
        daemon = _daemon()
        names = [profile] if profile else daemon.profile_names
        failed: list[str] = []
        for name in names:
            label = "" if len(names) == 1 else f"{name}："
            if daemon.test_connection(timeout=timeout, profile=name):
                console.print(f"[green]{escape(label)}连接正常 OK[/green]")
            else:
                console.print(f"[red]{escape(label)}连接失败[/red]")
                failed.append(name)
        if failed:
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


@app.command()
def check(
    timeout: int = typer.Option(10, "--timeout", help="端口检查超时（秒）"),
    profile: str | None = typer.Option(
        None, "--profile", "-P", help="只检查指定 profile（默认逐条检查）"
    ),
) -> None:
    """检查隧道端口：``-R`` 在服务器上、``-L``/``-D`` 在本机。"""
    try:
        daemon = _daemon()
        names = [profile] if profile else daemon.profile_names
        any_port = False
        for name in names:
            label = "" if len(names) == 1 else f"{name} "
            remote = daemon.check_remote_ports(timeout=timeout, profile=name)
            local = daemon.check_local_ports(profile=name)
            for port, ok in sorted(remote.items()):
                any_port = True
                console.print(f"{label}远程端口 {port}: {_markup_port(ok)}")
            for port, ok in sorted(local.items()):
                any_port = True
                console.print(f"{label}本地端口 {port}: {_markup_port(ok)}")
        if not any_port:
            console.print("[yellow]没有任何配置的隧道端口[/yellow]")
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


# ---------------------------------------------------------------------------
# scheduled task / config
# ---------------------------------------------------------------------------


@app.command()
def doctor(
    offline: bool = typer.Option(False, "--offline", help="跳过需要网络/SSH 的检查"),
    timeout: int = typer.Option(5, "--timeout", help="SSH 连通性测试超时（秒）"),
) -> None:
    """一键体检：配置、密钥、连通性、端口、自启与通知，逐项给结论与修法。"""
    try:
        cfg = get_config()
    except ConfigError as exc:
        # 配置本身坏掉时，doctor 的价值就是直接把原因和修法说清楚，
        # 而不是抛异常让用户回去自己猜。
        console.print(Panel(escape(str(exc)), title="ponte doctor", border_style="red"))
        console.print("[dim]修好配置后重新运行 ponte doctor[/dim]")
        raise typer.Exit(code=1) from None

    try:
        daemon = _daemon()
    except Exception:  # noqa: BLE001 - 其余检查仍然有价值
        daemon = None

    checks = run_checks(cfg, daemon, offline=offline, timeout=timeout)
    table = Table(title="ponte doctor", header_style="bold cyan")
    table.add_column("检查", no_wrap=True, style="cyan")
    table.add_column("结论", no_wrap=True)
    table.add_column("详情")
    table.add_column("建议", style="dim")
    marks = {
        OK: "[green]✔ 通过[/green]",
        WARN: "[yellow]! 注意[/yellow]",
        FAIL: "[red]✘ 失败[/red]",
        SKIP: "[dim]– 跳过[/dim]",
    }
    for check in checks:
        table.add_row(
            escape(check.name),
            marks.get(check.status, check.status),
            escape(check.detail),
            escape(check.hint),
        )
    console.print(table)

    tally = counts(checks)
    console.print(
        f"通过 {tally[OK]} · 注意 {tally[WARN]} · "
        f"失败 {tally[FAIL]} · 跳过 {tally[SKIP]}"
    )
    if tally[FAIL]:
        raise typer.Exit(code=1)


@app.command("notify-test")
def notify_test(
    profile: str | None = typer.Option(
        None, "--profile", "-P", help="测试消息里显示的名字"
    ),
) -> None:
    """发一条测试通知，验证 ntfy / webhook 配置真的能收到。"""
    from ponte.notify import Notifier

    try:
        cfg = get_config()
        if not cfg.notify.enabled:
            _fail("[notify] enabled = false：先启用它再测试")
        if not cfg.notify.channels:
            _fail("[notify] 没有配置任何通道（ntfy_topic 或 webhook_url）")

        notifier = Notifier(cfg.notify)
        name = profile or (cfg.profile_names[0] if cfg.profile_names else "test")
        results = notifier.send_test(profile=name, destination="（测试消息）")
        for channel, delivered in results.items():
            mark = "[green]已发送[/green]" if delivered else "[red]发送失败[/red]"
            console.print(f"{channel}：{mark}")
        if notifier.last_error:
            console.print(f"[yellow]{escape(notifier.last_error)}[/yellow]")
        if not all(results.values()):
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


@app.command()
def install() -> None:
    """注册开机自启 + 崩溃重启（按平台：计划任务 / systemd / launchd）。"""
    try:
        daemon = _daemon()
        message = daemon.install_service()
        console.print("[green]已注册开机自启服务（崩溃自动重启）[/green]")
        if message:
            console.print(f"[dim]{escape(message)}[/dim]")
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


@app.command()
def uninstall() -> None:
    """移除已注册的开机自启服务。"""
    try:
        daemon = _daemon()
        message = daemon.uninstall_service()
        console.print("[green]已移除开机自启服务[/green]")
        if message:
            console.print(f"[dim]{escape(message)}[/dim]")
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


@app.command()
def config() -> None:
    """打印当前生效的配置关键项。"""
    try:
        cfg = get_config()
        retry = cfg.retry
        health = cfg.health

        table = Table(title="生效配置", header_style="bold cyan")
        table.add_column("键", no_wrap=True, style="cyan")
        table.add_column("值")

        multiple = len(cfg.profiles) > 1
        for profile in cfg.profiles:
            prefix = f"{profile.name} · " if multiple else ""
            table.add_row(
                f"{prefix}服务器", f"{profile.ssh.user}@{profile.ssh.host}"
            )
            table.add_row(f"{prefix}SSH 端口", str(profile.ssh.port))
            tunnel_lines = [
                t.summary + (f"  ({escape(t.description)})" if t.description else "")
                for t in profile.tunnels
            ]
            table.add_row(f"{prefix}隧道", "\n".join(tunnel_lines) or "（无）")
        table.add_row(
            "retry",
            f"max_retries={retry.max_retries}, base_delay={retry.base_delay}s, "
            f"max_delay={retry.max_delay}s, backoff_factor={retry.backoff_factor}, "
            f"jitter={'on' if retry.jitter else 'off'}, "
            f"stable_after={retry.stable_after}s",
        )
        table.add_row(
            "health",
            f"check_interval={health.check_interval}s, "
            f"remote_check={'on' if health.remote_check_enabled else 'off'}, "
            f"remote_check_timeout={health.remote_check_timeout}s, "
            f"max_check_interval={health.max_check_interval}s",
        )
        notify = cfg.notify
        table.add_row(
            "notify",
            f"enabled={'on' if notify.enabled else 'off'}, "
            f"channels={', '.join(notify.channels) or '（无）'}, "
            f"on_consecutive_failures={notify.on_consecutive_failures}, "
            f"cooldown={notify.cooldown}s",
        )
        # 令牌只报“有没有”，绝不回显：ponte config 的输出经常被粘进 issue。
        serve = cfg.serve
        table.add_row(
            "serve",
            f"host={serve.host}, port={serve.port}, "
            f"token={'已设置' if serve.token else '（无）'}, "
            f"refresh={serve.refresh}s",
        )
        table.add_row("pid_file", cfg.daemon.pid_file or "（默认）")
        table.add_row("log_file", cfg.daemon.log_file or "（默认）")
        table.add_row("ssh_exe", cfg.windows.ssh_exe or "ssh（PATH）")
        table.add_row("windows.run_as", cfg.windows.run_as)
        table.add_row("配置文件", cfg.source_path)

        console.print(table)

        for warning in cfg.warnings:
            console.print(f"[yellow]警告：{escape(warning)}[/yellow]")
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(str(exc))


@app.command()
def init(
    path: Path | None = typer.Option(
        None, "--path", help="写入路径（默认写入用户配置目录）"
    ),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的配置文件"),
) -> None:
    """生成配置文件（默认复制到用户配置目录，不覆盖已有文件）。"""
    try:
        target = init_config(path, force=force)
    except (ConfigError, OSError) as exc:
        _fail(str(exc))

    console.print(f"[green]已写入配置：{escape(target)}[/green]")
    console.print(
        "[dim]请填写 "
        + escape("[ssh]")
        + " 的 host / user / identity_file，然后运行 ponte test[/dim]"
    )
    console.print(
        "[dim]换其它配置文件：ponte --config <path> … 或设置 PONTE_CONFIG[/dim]"
    )


if __name__ == "__main__":
    app()
