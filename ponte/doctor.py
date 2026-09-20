"""``ponte doctor``: one pass over the things that actually break tunnels.

Every check answers with a *conclusion* and — when something is off — a fix to
act on. That is the difference between "it does not work" and a support thread.

Doctor is strictly read-only: it never installs, restarts or changes anything,
so it is always safe to run on a machine you are debugging. The exceptions worth
knowing about are the connectivity check, which opens a short-lived SSH
connection exactly like ``ponte test``, and the jump-host probe, which opens one
TCP connection; ``--offline`` skips both.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import shutil
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from ponte.config import NotifyConfig, Profile, TunnelConfig
from ponte.core import ProbeError, TunnelManager, port_is_open

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids importing the world
    from ponte.daemon import DaemonStatus

__all__ = [
    "FAIL",
    "OK",
    "SKIP",
    "WARN",
    "CheckResult",
    "DoctorDaemon",
    "counts",
    "run_checks",
]

logger = logging.getLogger(__name__)

#: A check that passed.
OK = "ok"
#: Something is off but the tunnel can still work (or nothing is running yet).
WARN = "warn"
#: Something is wrong and the tunnel cannot work as configured.
FAIL = "fail"
#: Not applicable, or deliberately not checked (``--offline``).
SKIP = "skip"

#: Hints shown for the two most common connectivity failures.
_SSH_HINT = (
    "确认服务器可达、公钥已加入 authorized_keys、云安全组放行了 SSH 端口；"
    "先单独跑一次 ssh 验证"
)
_DAEMON_HINT = "ponte start 启动；ponte install 注册开机自启与崩溃重启"


class DoctorDaemon(Protocol):
    """The slice of :class:`~ponte.daemon.TunnelDaemon` that doctor uses.

    Spelled out as a protocol so the checks can be driven by a test double
    without casts — and so it stays obvious what doctor actually depends on.

    The notifier is deliberately *not* part of the protocol: doctor only peeks
    at ``last_error`` through ``getattr``, and a mutable protocol attribute
    would make ``TunnelDaemon`` fail structural matching for no benefit.
    """

    def status(self) -> DaemonStatus: ...

    def test_connection(
        self, timeout: int = 10, profile: str | None = None
    ) -> bool: ...

    def check_remote_ports(
        self, timeout: int = 10, profile: str | None = None
    ) -> dict[int, bool]: ...

    def check_local_ports(
        self, timeout: float = 1.0, profile: str | None = None
    ) -> dict[int, bool]: ...

    def service_installed(self) -> bool | None: ...


@dataclasses.dataclass(frozen=True)
class CheckResult:
    """One line of the report: what was checked, and what to do about it."""

    name: str
    status: str
    detail: str = ""
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK


def counts(results: list[CheckResult]) -> dict[str, int]:
    """Tally results by status, for the report's summary line."""
    tally = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0}
    for result in results:
        tally[result.status] = tally.get(result.status, 0) + 1
    return tally


def run_checks(
    config: TunnelConfig,
    daemon: DoctorDaemon | None = None,
    *,
    offline: bool = False,
    timeout: int = 5,
) -> list[CheckResult]:
    """Run every check against *config* and return the results in report order.

    Args:
        config: The validated configuration (doctor never loads it itself, so a
            *broken* config can be reported by the caller instead of crashing).
        daemon: A :class:`~ponte.daemon.TunnelDaemon` for the state-dependent
            checks. ``None`` turns those into ``SKIP`` rows, which keeps the
            function usable without a real daemon.
        offline: Skip the checks that need the network.
        timeout: Timeout for the SSH connectivity test, seconds.
    """
    status = None
    if daemon is not None:
        try:
            status = daemon.status()
        except Exception as exc:  # noqa: BLE001 - doctor must not blow up
            logger.debug("could not read the daemon status: %s", exc)
    running = bool(getattr(status, "running", False))

    results: list[CheckResult] = [_config_check(config)]
    for profile in config.profiles:
        results.extend(
            _profile_checks(
                config,
                profile,
                daemon=daemon,
                running=running,
                offline=offline,
                timeout=timeout,
            )
        )
    results.append(_daemon_check(status))
    results.append(_service_check(config, daemon))
    results.append(_log_check(config))
    results.append(_notify_check(config.notify, daemon))
    return results


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _config_check(config: TunnelConfig) -> CheckResult:
    detail = (
        f"{config.source_path}"
        f"（{len(config.profiles)} 条隧道：{', '.join(config.profile_names)}）"
    )
    if config.warnings:
        return CheckResult(
            "配置文件", WARN, detail, "；".join(config.warnings)
        )
    return CheckResult("配置文件", OK, detail)


# ---------------------------------------------------------------------------
# Per profile
# ---------------------------------------------------------------------------
def _profile_checks(
    config: TunnelConfig,
    profile: Profile,
    *,
    daemon: DoctorDaemon | None,
    running: bool,
    offline: bool,
    timeout: int,
) -> list[CheckResult]:
    multiple = len(config.profiles) > 1

    def label(what: str) -> str:
        return f"{profile.name} · {what}" if multiple else what

    results = [
        _ssh_client_check(config, profile, label),
        _identity_check(profile, label),
    ]
    if sys.platform != "win32":
        results.append(_key_permission_check(profile, label))
    jump = _jump_check(profile, label, offline, timeout)
    if jump is not None:
        # Reported *before* connectivity on purpose: when the bastion is down
        # the login failure that follows is a consequence, not a second problem.
        results.append(jump)

    # Connectivity is worth testing even when nothing is running: it is the one
    # thing that tells apart "my config is wrong" from "the daemon is down".
    results.append(_connectivity_check(daemon, profile, label, offline, timeout))
    remote = _remote_ports_check(daemon, profile, label, running, offline, timeout)
    if remote is not None:
        results.append(remote)
    local = _local_ports_check(daemon, profile, label, running, offline)
    if local is not None:
        results.append(local)
    return results


def _ssh_client_check(
    config: TunnelConfig, profile: Profile, label: Callable[[str], str]
) -> CheckResult:
    """Resolve the same ``ssh`` the daemon will use, and say where it came from."""
    try:
        exe = TunnelManager(config, profile).ssh_exe
    except Exception as exc:  # noqa: BLE001 - a broken env is exactly the point
        return CheckResult(
            label("SSH 客户端"),
            FAIL,
            f"{type(exc).__name__}: {exc}",
            "安装 OpenSSH 客户端，或在 [windows] ssh_exe 指定绝对路径",
        )
    if exe == "ssh" and shutil.which("ssh") is None:
        return CheckResult(
            label("SSH 客户端"),
            WARN,
            "PATH 里找不到 ssh，将直接执行 'ssh'",
            "安装 OpenSSH 客户端，或在 [windows] ssh_exe 指定绝对路径",
        )
    return CheckResult(label("SSH 客户端"), OK, exe)


def _identity_check(profile: Profile, label: Callable[[str], str]) -> CheckResult:
    path = profile.ssh.identity_file
    if not path:
        # Not an error any more: without ``identity_file`` ponte omits ``-i``
        # and lets ssh resolve the key from ~/.ssh/config, ssh-agent or its
        # default locations. Only warn that this may not survive a rebooted
        # service with no agent.
        return CheckResult(
            label("密钥文件"),
            OK,
            "未配置，交由 ~/.ssh/config / ssh-agent / 默认密钥",
            "后台服务读不到 ssh-agent 时，建议显式设置 [ssh] identity_file",
        )
    if not os.path.isfile(path):
        return CheckResult(
            label("密钥文件"),
            FAIL,
            f"不存在：{path}",
            "路径写错或密钥被移动了；用 ssh-keygen 生成一对，或改用实际路径",
        )
    return CheckResult(label("密钥文件"), OK, path)


def _key_permission_check(profile: Profile, label: Callable[[str], str]) -> CheckResult:
    """POSIX only: a world/group readable private key is refused by OpenSSH."""
    path = profile.ssh.identity_file
    if not path:
        return CheckResult(label("密钥权限"), SKIP, "未显式配置 identity_file")
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        return CheckResult(label("密钥权限"), WARN, f"无法读取权限：{exc}")
    if mode & 0o077:
        return CheckResult(
            label("密钥权限"),
            WARN,
            f"{path} 允许组或其他用户访问（{oct(mode & 0o777)}）",
            f"chmod 600 {path}（ssh 会拒绝权限过宽的私钥）",
        )
    return CheckResult(label("密钥权限"), OK, oct(mode & 0o777))


def _jump_check(
    profile: Profile,
    label: Callable[[str], str],
    offline: bool,
    timeout: int,
) -> CheckResult | None:
    """Reachability of the first jump hop (``None`` without a jump chain).

    A tunnel through a bastion fails in one of two places — the hop, or the
    server behind it — and ssh reports both with the same unhelpful login
    failure. A plain TCP connect to the hop tells the two apart in one line.

    Only the *first* hop is probed: every later one is reached through its
    predecessor, so a connect attempt from here would fail on a perfectly
    healthy chain and turn the report into a lie.
    """
    hops = profile.ssh.jumps
    if not hops:
        return None
    chain = " → ".join(hop.destination for hop in hops)
    head = hops[0]
    name = label("跳板机")
    if offline:
        return CheckResult(name, SKIP, f"{chain}（已跳过，--offline）")
    if port_is_open(head.host, head.port, timeout):
        detail = f"{chain}（第一跳 {head.host}:{head.port} 可达）"
        if len(hops) > 1:
            detail += "；后续跳只能经前一跳验证，以 SSH 连通性为准"
        return CheckResult(name, OK, detail)
    return CheckResult(
        name,
        FAIL,
        f"连不上第一跳 {head.host}:{head.port}（完整链路：{chain}）",
        f"先在终端单独执行 ssh {head.destination}，确认地址、端口、密钥与"
        "authorized_keys；堡垒机不可达时，后面的隧道一定起不来",
    )


def _connectivity_hint(profile: Profile) -> str:
    """Hint for a failed login, naming the bastion when there is one.

    "Check that the server is reachable" is misleading advice when the server
    is *supposed* to be unreachable from here — the thing to verify is the hop.
    """
    head = profile.ssh.first_hop
    if head is None:
        return _SSH_HINT
    return (
        f"链路是 {profile.ssh.proxy_jump}，本机只直连第一跳：先单独 ssh "
        f"{head.destination} 验证跳板机，再用 ssh -J … 验证整条链路"
    )


def _connectivity_check(
    daemon: DoctorDaemon | None,
    profile: Profile,
    label: Callable[[str], str],
    offline: bool,
    timeout: int,
) -> CheckResult:
    name = label("SSH 连通性")
    if offline:
        return CheckResult(name, SKIP, "已跳过（--offline）")
    if daemon is None:
        return CheckResult(name, SKIP, "没有可用的守护进程对象")
    hint = _connectivity_hint(profile)
    try:
        reachable = daemon.test_connection(timeout=timeout, profile=profile.name)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, FAIL, f"{type(exc).__name__}: {exc}", hint)
    if reachable:
        detail = f"能在 {timeout}s 内登录 {profile.destination}"
        if profile.ssh.jumps:
            detail += f"（经 {profile.ssh.proxy_jump}）"
        return CheckResult(name, OK, detail)
    return CheckResult(name, FAIL, f"{timeout}s 内未能登录 {profile.destination}", hint)


def _remote_ports_check(
    daemon: DoctorDaemon | None,
    profile: Profile,
    label: Callable[[str], str],
    running: bool,
    offline: bool,
    timeout: int,
) -> CheckResult | None:
    """Server-side ``-R`` ports; meaningless unless the daemon is running."""
    ports = sorted(
        int(t.remote_port) for t in profile.tunnels if t.is_remote and t.remote_port
    )
    if not ports:
        return None
    name = label(f"远程端口 {', '.join(str(p) for p in ports)}")
    if offline:
        return CheckResult(name, SKIP, "已跳过（--offline）")
    if daemon is None or not running:
        return CheckResult(name, SKIP, "守护进程未运行，端口状态没有参考价值")
    try:
        state = daemon.check_remote_ports(timeout=timeout, profile=profile.name)
    except ProbeError as exc:
        # The probe's own connection failed: this is "cannot tell", not "the
        # ports are closed" — and it must not read as a failure to the user.
        return CheckResult(name, WARN, str(exc))
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, WARN, f"探测失败：{type(exc).__name__}: {exc}")
    down = [port for port, is_open in sorted(state.items()) if not is_open]
    if not down:
        return CheckResult(name, OK, "全部在服务器上监听")
    return CheckResult(
        name,
        FAIL,
        f"未监听：{', '.join(str(p) for p in down)}",
        "服务器上 ss -tlnp 确认端口；仍被占用说明 sshd 的转发没建立",
    )


def _local_ports_check(
    daemon: DoctorDaemon | None,
    profile: Profile,
    label: Callable[[str], str],
    running: bool,
    offline: bool,
) -> CheckResult | None:
    """Client-side ``-L``/``-D`` listeners."""
    ports = sorted(t.local_port for t in profile.tunnels if not t.is_remote)
    if not ports:
        return None
    name = label(f"本地端口 {', '.join(str(p) for p in ports)}")
    if offline:
        return CheckResult(name, SKIP, "已跳过（--offline）")
    if daemon is None or not running:
        return CheckResult(name, SKIP, "守护进程未运行，端口状态没有参考价值")
    try:
        state = daemon.check_local_ports(profile=profile.name)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, WARN, f"探测失败：{type(exc).__name__}: {exc}")
    down = [port for port, is_open in sorted(state.items()) if not is_open]
    if not down:
        return CheckResult(name, OK, "本机监听正常")
    return CheckResult(
        name,
        WARN,
        f"未监听：{', '.join(str(p) for p in down)}",
        "本地端口被别的程序占用时 ssh 会报 bind failed，检查 ponte logs",
    )


# ---------------------------------------------------------------------------
# Process / service / log / notify
# ---------------------------------------------------------------------------
def _daemon_check(status: DaemonStatus | None) -> CheckResult:
    if status is None:
        return CheckResult("守护进程", SKIP, "无法读取状态")
    if not status.running:
        return CheckResult("守护进程", WARN, "未运行", _DAEMON_HINT)

    profiles = list(status.profiles)
    detail = f"pid {status.pid if status.pid is not None else '?'}，已运行 {status.uptime}"
    # ``healthy is False`` alone is not a verdict: a check the daemon could not
    # complete (``health_conclusive is False``) reports False too, and calling
    # that "异常" here is the false alarm this check exists to avoid.
    broken = [
        p.name for p in profiles if p.healthy is False and p.health_conclusive is not False
    ]
    unverified = [
        p.name for p in profiles if p.healthy is False and p.health_conclusive is False
    ]
    unknown = [p.name for p in profiles if p.healthy is None]
    if broken:
        return CheckResult(
            "守护进程",
            FAIL,
            f"{detail}，异常：{', '.join(broken)}",
            "ponte watch 看是哪一条；ponte logs -n 50 看断线原因",
        )
    if unknown:
        return CheckResult("守护进程", WARN, f"{detail}，尚未上报健康状态：{', '.join(unknown)}")
    if unverified:
        return CheckResult(
            "守护进程",
            WARN,
            f"{detail}，无法判定（探测连接没建起来）：{', '.join(unverified)}",
            "隧道本身可能在正常转发；ponte logs -n 50 看探测失败原因",
        )
    return CheckResult("守护进程", OK, f"{detail}，{len(profiles)} 条隧道全部健康")


def _service_label(config: TunnelConfig) -> str:
    """Human name of the auto-start artifact on this platform."""
    if sys.platform == "win32":
        return f"计划任务 {config.windows.task_name}"
    if sys.platform == "linux":
        return f"systemd user 单元 {config.service.name}.service"
    if sys.platform == "darwin":
        return f"LaunchAgent com.modusensus.{config.service.name}"
    return f"服务 {config.service.name}"


def _service_check(config: TunnelConfig, daemon: DoctorDaemon | None) -> CheckResult:
    installed = None
    if daemon is not None:
        try:
            installed = daemon.service_installed()
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not query the service state: %s", exc)
    if installed is None:
        return CheckResult("开机自启", SKIP, "无法确认（平台不支持或系统工具不可用）")
    if installed:
        return CheckResult("开机自启", OK, f"已注册：{_service_label(config)}")
    return CheckResult(
        "开机自启",
        WARN,
        f"未注册：{_service_label(config)}",
        "ponte install 注册开机自启与崩溃重启（重启后隧道才会自己回来）",
    )


def _log_check(config: TunnelConfig) -> CheckResult:
    path = config.daemon.log_file
    if not path:
        return CheckResult("日志文件", SKIP, "未配置")
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        return CheckResult(
            "日志文件",
            FAIL,
            f"目录不存在：{directory}",
            "先跑一次 ponte start 让它创建，或修正 [daemon] log_file",
        )
    if not os.path.isfile(path):
        return CheckResult(
            "日志文件",
            WARN,
            f"还没有日志：{path}",
            "ponte start 之后再回来看；一直没有说明守护进程没起来",
        )
    return CheckResult("日志文件", OK, f"{path}（{os.path.getsize(path)} 字节）")


def _notify_check(notify: NotifyConfig, daemon: DoctorDaemon | None) -> CheckResult:
    label = "断线通知"
    if not notify.enabled:
        return CheckResult(
            label,
            SKIP,
            "未启用（[notify] enabled = false）",
            "想让 ponte 主动告诉你，就配置 ntfy_topic 或 webhook_url 并设 enabled = true",
        )
    channels = ", ".join(notify.channels)
    if not channels:
        return CheckResult(
            label,
            FAIL,
            "enabled = true 但没有配置任何通道",
            "设置 [notify] ntfy_topic 或 webhook_url",
        )
    detail = (
        f"通道：{channels}；连续失败 {notify.on_consecutive_failures} 次后通知，"
        f"冷却 {notify.cooldown}s"
    )
    notifier = getattr(daemon, "notifier", None) if daemon is not None else None
    last_error = getattr(notifier, "last_error", None)
    if last_error:
        return CheckResult(label, WARN, detail, f"最近一次发送失败：{last_error}")
    return CheckResult(label, OK, detail)
