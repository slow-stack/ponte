"""pytest tests for :mod:`ponte.daemon` (offline helpers only).

Daemon lifecycle paths that need a real config / OS service are exercised
through their pure helper functions; nothing here spawns SSH or touches the
scheduled-task / systemd / launchd registries.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
import types

import pytest

import ponte.daemon as daemon
from ponte.config import (
    Profile,
    SSHConfig,
    Tunnel,
    TunnelConfig,
    WindowsConfig,
    load_config,
)
from ponte.core import creation_flags
from ponte.daemon import (
    DaemonStatus,
    ProfileRunner,
    TunnelDaemon,
    _decode_console,
    _derive_reload_marker,
    _derive_status_file,
    _derive_stop_marker,
    _encode_ps,
    _run_tool,
    _windows_pid_alive,
    _windows_process_listed,
)
from ponte.health import HealthStatus
from ponte.retry import RetryEvent


def _section(d, profile: str = "default") -> dict:
    """The status-file section of *profile* (the file is keyed by profile)."""
    return d._store.read_profiles().get(profile, {})


def _write_status_file(d, payload: dict) -> None:
    """Seed a raw status payload; tests write the file the daemon will read."""
    os.makedirs(os.path.dirname(d.status_file), exist_ok=True)
    with open(d.status_file, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def _cfg(tmp_path, *, run_as: str = "system") -> TunnelConfig:
    return TunnelConfig(
        profiles=[
            Profile(
                name="default",
                ssh=SSHConfig(
                    host="example.com",
                    user="testuser",
                    identity_file="/keys/id_rsa",
                    known_hosts_file="/keys/known_hosts",
                ),
                tunnels=[
                    Tunnel(remote_port=23334, local_host="localhost", local_port=2222)
                ],
            )
        ],
        daemon=__import__("ponte.config", fromlist=["DaemonConfig"]).DaemonConfig(
            pid_file=str(tmp_path / "ponte.pid"),
            log_file=str(tmp_path / "ponte.log"),
        ),
        windows=WindowsConfig(run_as=run_as),
    )


def test_derive_status_and_stop_from_pid() -> None:
    pid = r"C:\x\ponte.pid"
    assert _derive_status_file(pid) == r"C:\x\ponte.status.json"
    assert _derive_stop_marker(pid) == r"C:\x\ponte.stop"


def test_encode_ps_roundtrip() -> None:
    script = "Write-Output 'installed'"
    encoded = _encode_ps(script)
    assert isinstance(encoded, str)
    decoded = encoded.encode("ascii")
    import base64
    assert base64.b64decode(decoded).decode("utf-16-le") == script


def test_decode_console_utf8_and_gbk() -> None:
    assert _decode_console(b"") == ""
    assert _decode_console("正常".encode()) == "正常"
    # GBK 字节在 UTF-8 下非法 → 回退 GBK 解码
    assert _decode_console("已注册".encode("gbk")) == "已注册"


def test_daemon_status_uptime() -> None:
    s = DaemonStatus(running=True, uptime_seconds=3661)
    assert s.uptime == "1h 1m 1s"


def test_write_read_pid(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    d.write_pid()
    assert d.read_pid() == os.getpid()


def test_read_pid_missing(tmp_path) -> None:
    d = TunnelDaemon(_cfg(tmp_path))
    assert d.read_pid() is None


# ---------------------------------------------------------------------------
# Windows 进程存活判定 —— 非提权用户看不到 SYSTEM 守护进程
# ---------------------------------------------------------------------------


def test_pid_alive_win32_uses_the_exit_code_when_openable(monkeypatch) -> None:
    """能拿到句柄时以退出码为准：259 是 STILL_ACTIVE，其它就是已退出。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("ponte.daemon._windows_exit_code", lambda _pid: 259)
    assert TunnelDaemon._pid_alive(4321) is True

    monkeypatch.setattr("ponte.daemon._windows_exit_code", lambda _pid: 1)
    assert TunnelDaemon._pid_alive(4321) is False


def test_pid_alive_win32_falls_back_when_openprocess_is_denied(monkeypatch) -> None:
    """ACCESS_DENIED（SYSTEM 身份跑着的守护进程）不能当成“没在跑”。

    误报“未运行”的代价很具体：ponte start 会再拉一个守护进程抢同一对服务器端口，
    ponte stop 则拒绝停掉真正在跑的那个。
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("ponte.daemon._windows_exit_code", lambda _pid: None)

    monkeypatch.setattr("ponte.daemon._windows_process_listed", lambda _pid: True)
    assert TunnelDaemon._pid_alive(4321) is True

    # 进程真的不在了：同样拿不到句柄，但进程列表里找不到。
    monkeypatch.setattr("ponte.daemon._windows_process_listed", lambda _pid: False)
    assert TunnelDaemon._pid_alive(4321) is False


def test_pid_alive_win32_assumes_alive_when_it_cannot_tell(monkeypatch) -> None:
    """连进程列表都拿不到时宁可报“活着”：多拦一次 start 好过起两个守护进程。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("ponte.daemon._windows_exit_code", lambda _pid: None)
    monkeypatch.setattr("ponte.daemon._windows_process_listed", lambda _pid: None)
    assert TunnelDaemon._pid_alive(4321) is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_process_listed_sees_a_real_process() -> None:
    """真实调用 Toolhelp32（64 位下快照句柄很容易被截断，这里守住）。"""
    assert _windows_process_listed(os.getpid()) is True
    assert _windows_process_listed(2**31 - 1) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_pid_alive_reports_a_dead_pid_as_dead() -> None:
    """不存在的 pid 必须判定为已退出（否则 ponte start 永远拒绝启动）。"""
    assert _windows_pid_alive(os.getpid()) is True
    assert _windows_pid_alive(2**31 - 1) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_pid_alive_sees_an_account_it_cannot_open() -> None:
    """SYSTEM 跑着的进程，本用户拿不到句柄，也必须判为“活着”。

    这正是 ponte 在 Windows 推荐的部署：守护进程以 SYSTEM 开机即起，
    status / stop 却由登录用户执行——拿不到句柄就报“未运行”的话，
    ponte start 会再起一个守护进程抢同一对端口。
    """
    import subprocess

    import ponte.daemon as daemon_module

    out = subprocess.run(
        ["tasklist", "/FI", "USERNAME eq SYSTEM", "/NH", "/FO", "CSV"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout
    pid = None
    for line in out.splitlines():
        parts = [part.strip('"') for part in line.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            pid = int(parts[1])
            break
    if pid is None:
        pytest.skip("no SYSTEM process to inspect")
    if daemon_module._windows_exit_code(pid) is not None:
        pytest.skip("this session can open SYSTEM processes (elevated?)")
    assert _windows_pid_alive(pid) is True


def test_status_not_running(tmp_path) -> None:
    d = TunnelDaemon(_cfg(tmp_path))
    s = d.status()
    assert s.running is False


def test_status_json_parsing(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    # 伪造一个存活 pid 的 status 文件：用当前进程
    with open(cfg.daemon.pid_file, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    _write_status_file(
        d,
        {
            "started_at": time.time(),
            "profiles": {
                "default": {"healthy": True, "remote_ports": {"23334": True}},
                "offsite": {"healthy": False},
            },
        },
    )
    s = d.status()
    assert s.running is True
    assert [p.name for p in s.profiles] == ["default", "offsite"]
    assert s.profiles[0].healthy is True
    assert s.profiles[0].remote_ports == {23334: True}
    assert s.profiles[1].healthy is False
    assert s.profiles[1].remote_ports == {}
    # 任一 profile 不健康 → 整体不健康。
    assert s.healthy is False
    assert s.get_profile("offsite") is s.profiles[1]
    assert s.get_profile("nowhere") is None


def test_status_reads_pre_profile_status_file(tmp_path) -> None:
    """升级前写的扁平状态文件被当作 default profile 读取（原地升级不丢历史）。"""
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    _live_pid(tmp_path)
    _write_status_file(
        d,
        {"started_at": time.time(), "healthy": True, "remote_ports": {"23334": True}},
    )
    s = d.status()
    assert [p.name for p in s.profiles] == ["default"]
    assert s.profiles[0].healthy is True
    assert s.profiles[0].remote_ports == {23334: True}
    assert s.healthy is True


class _DurationManager:
    """Manager stand-in exposing ``last_session_duration`` from a list.

    ``calls`` is set by the test to the number of completed sessions before
    the ``DISCONNECTED`` event is recorded (mirrors ``TunnelManager``, which
    updates the duration at each ``connect()`` return). An empty list means
    "no session ever completed" (duration ``None``).
    """

    def __init__(self, durations: list[float]) -> None:
        self.durations = durations
        self.calls = 0

    @property
    def last_session_duration(self) -> float | None:
        if self.calls == 0 or not self.durations:
            return None
        return self.durations[min(self.calls - 1, len(self.durations) - 1)]


def test_status_malformed_pid_file(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    with open(cfg.daemon.pid_file, "w", encoding="utf-8") as fh:
        fh.write("not-a-number")
    assert d.read_pid() is None


def test_status_malformed_status_json(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    with open(cfg.daemon.pid_file, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    with open(d.status_file, "w", encoding="utf-8") as fh:
        fh.write("not json")
    s = d.status()
    assert s.running is True
    assert s.healthy is None


def test_safe_remove_missing_file(tmp_path) -> None:
    # 删除不存在的路径不应报错
    TunnelDaemon._safe_remove(str(tmp_path / "missing"))


def test_cleanup_removes_pid_and_marker(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    d.write_pid()
    with open(d.stop_marker, "w", encoding="utf-8") as fh:
        fh.write("x")
    with open(d.reload_marker, "w", encoding="utf-8") as fh:
        fh.write("x")
    d._cleanup()
    assert not os.path.exists(cfg.daemon.pid_file)
    assert not os.path.exists(d.stop_marker)
    assert not os.path.exists(d.reload_marker)


def _windows_path_semantics(monkeypatch, *, executable: str, existing: set[str]) -> None:
    """模拟 Windows 路径语义（POSIX 上 os.path 用 / 会破坏匹配）。

    同样把 ``exists`` 限定成白名单，保证「有/没有 pythonw.exe」两种情形都能
    在任意平台上确定性地复现。
    """
    monkeypatch.setattr(sys, "executable", executable)
    monkeypatch.setattr(
        "ponte.daemon.os.path.basename", lambda _p: executable.split("\\")[-1]
    )
    monkeypatch.setattr(
        "ponte.daemon.os.path.dirname",
        lambda _p: "\\".join(executable.split("\\")[:-1]),
    )
    monkeypatch.setattr(
        "ponte.daemon.os.path.join",
        lambda *parts: "\\".join(str(p).rstrip("\\/") for p in parts),
    )
    known = {p.lower() for p in existing}
    monkeypatch.setattr(
        "ponte.daemon.os.path.exists", lambda p: str(p).lower() in known
    )


def test_pythonw_path_when_executable_is_pythonw(monkeypatch, tmp_path) -> None:
    d = TunnelDaemon(_cfg(tmp_path))
    _windows_path_semantics(
        monkeypatch, executable=r"C:\Python\pythonw.exe", existing=set()
    )
    assert d._pythonw_path() == r"C:\Python\pythonw.exe"


def test_pythonw_path_prefers_sibling_pythonw(monkeypatch, tmp_path) -> None:
    d = TunnelDaemon(_cfg(tmp_path))
    _windows_path_semantics(
        monkeypatch,
        executable=r"C:\Python\python.exe",
        existing={r"C:\Python\pythonw.exe"},
    )
    assert d._pythonw_path() == r"C:\Python\pythonw.exe"


def test_pythonw_path_refuses_to_fall_back_to_console_python(
    monkeypatch, tmp_path
) -> None:
    """没有 pythonw.exe 时必须拒绝安装，而不是静默退回 python.exe 去弹窗。"""
    d = TunnelDaemon(_cfg(tmp_path))
    _windows_path_semantics(
        monkeypatch, executable=r"C:\Python\python.exe", existing=set()
    )
    with pytest.raises(RuntimeError) as excinfo:
        d._pythonw_path()
    message = str(excinfo.value)
    assert "pythonw.exe" in message
    assert "pythonw_exe" in message  # 告诉用户怎么修
    assert r"C:\Python\python.exe" in message


def test_pythonw_path_honours_configured_exe(monkeypatch, tmp_path) -> None:
    """[windows] pythonw_exe 显式指定时优先使用它。"""
    configured = r"D:\Other\pythonw.exe"
    cfg = dataclasses.replace(
        _cfg(tmp_path),
        windows=WindowsConfig(run_as="user", pythonw_exe=configured),
    )
    d = TunnelDaemon(cfg)
    _windows_path_semantics(
        monkeypatch, executable=r"C:\Python\python.exe", existing={configured}
    )
    assert d._pythonw_path() == configured


def test_pythonw_path_rejects_missing_configured_exe(monkeypatch, tmp_path) -> None:
    cfg = dataclasses.replace(
        _cfg(tmp_path),
        windows=WindowsConfig(run_as="user", pythonw_exe=r"D:\Gone\pythonw.exe"),
    )
    d = TunnelDaemon(cfg)
    _windows_path_semantics(
        monkeypatch, executable=r"C:\Python\python.exe", existing=set()
    )
    with pytest.raises(RuntimeError, match="pythonw_exe"):
        d._pythonw_path()


def _capture_install_script(monkeypatch, tmp_path, *, run_as: str) -> str:
    """Mock the PowerShell layer and return the Scheduled-Task script."""
    cfg = _cfg(tmp_path, run_as=run_as)
    daemon = TunnelDaemon(cfg)
    captured: dict[str, str] = {}

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(daemon, "_pythonw_path", lambda: r"C:\Python\pythonw.exe")

    def _run(script: str) -> str:
        captured["script"] = script
        return "installed"

    monkeypatch.setattr(daemon, "_run_powershell", _run)

    assert daemon.install_scheduled_task() == "installed"
    return captured["script"]


def test_install_scheduled_task_run_as_system(monkeypatch, tmp_path) -> None:
    """Default run_as=system keeps a boot-time SYSTEM task (pre-login)."""
    script = _capture_install_script(monkeypatch, tmp_path, run_as="system")
    assert "New-ScheduledTaskTrigger -AtStartup" in script
    assert "-UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest" in script
    assert "-AtLogOn" not in script
    assert "Interactive" not in script


def test_install_scheduled_task_run_as_user(monkeypatch, tmp_path) -> None:
    """run_as=user uses a logon task that can read the installing user's keys."""
    script = _capture_install_script(monkeypatch, tmp_path, run_as="user")
    assert "$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name" in script
    assert "New-ScheduledTaskTrigger -AtLogOn -User $currentUser" in script
    assert "-UserId $currentUser -LogonType Interactive -RunLevel Limited" in script
    assert "-UserId 'SYSTEM'" not in script


def test_setup_logging_idempotent(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    d._setup_logging()
    root = __import__("logging").getLogger("ponte")
    assert getattr(root, "_ponte_setup_ok", False) is True
    # 第二次调用不应重复添加 handler
    before = len(root.handlers)
    d._setup_logging()
    assert len(root.handlers) == before


def test_on_health_writes_status(tmp_path) -> None:
    from ponte.health import HealthStatus

    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    d._on_health(
        HealthStatus(
            process_alive=True,
            remote_ports={23334: True},
            all_healthy=True,
            timestamp=time.time(),
        )
    )
    section = _section(d)
    assert section["process_alive"] is True
    assert section["healthy"] is True
    assert section["remote_ports"] == {"23334": True}
    assert section["local_ports"] == {}


def test_on_health_is_per_profile(tmp_path) -> None:
    """每个 profile 写各自的 section，互不覆盖。"""
    d = TunnelDaemon(_cfg(tmp_path))
    d._on_health(_healthy_status(), profile="default")
    d._on_health(_unhealthy_status(), profile="offsite")
    assert _section(d, "default")["healthy"] is True
    assert _section(d, "offsite")["healthy"] is False
    assert set(d._store.read_profiles()) == {"default", "offsite"}


def test_read_status_missing_returns_empty(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    assert d._store.read_profiles() == {}


def test_read_status_invalid_returns_empty(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    os.makedirs(os.path.dirname(d.status_file), exist_ok=True)
    with open(d.status_file, "w", encoding="utf-8") as fh:
        fh.write("not json")
    assert d._store.read_profiles() == {}


def test_write_status_failure_silent(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)

    def _bad_open(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", _bad_open)
    with d._store.edit("default") as section:  # 不应抛出
        section["x"] = 1


class _FakeManager:
    """A TunnelManager stand-in that only records ``stop()`` calls."""

    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


def _unhealthy_status(process_alive: bool = True) -> HealthStatus:
    return HealthStatus(
        process_alive=process_alive,
        remote_ports={23334: False},
        all_healthy=False,
        timestamp=time.time(),
    )


def _healthy_status() -> HealthStatus:
    return HealthStatus(
        process_alive=True,
        remote_ports={23334: True},
        all_healthy=True,
        timestamp=time.time(),
    )


def _unknown_status() -> HealthStatus:
    """探测连接失败：没观察到端口，因此也没有“未监听”的结论。"""
    return HealthStatus(
        process_alive=True,
        remote_ports={},
        all_healthy=False,
        timestamp=time.time(),
        remote_probe_error="探测连接失败（ssh 退出码 255）：23334 的状态未知",
    )


def test_on_health_forces_reconnect_after_threshold(tmp_path) -> None:
    """连续 N 次 unhealthy 且进程活着 → manager.stop() 被调用，触发后计数归零。"""
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    manager = _FakeManager()

    # 连续两次假死还不够。
    d._on_health(_unhealthy_status(), manager)
    d._on_health(_unhealthy_status(), manager)
    assert manager.stop_calls == 0

    # 第三次达到阈值 → 强制重连一次。
    d._on_health(_unhealthy_status(), manager)
    assert manager.stop_calls == 1

    # 触发后计数归零，需重新累积才能再次触发。
    d._on_health(_unhealthy_status(), manager)
    d._on_health(_unhealthy_status(), manager)
    assert manager.stop_calls == 1
    d._on_health(_unhealthy_status(), manager)
    assert manager.stop_calls == 2


def test_on_health_resets_failures_on_recovery(tmp_path) -> None:
    """健康恢复 → 计数归零，不触发 stop。"""
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    manager = _FakeManager()

    d._on_health(_unhealthy_status(), manager)
    d._on_health(_unhealthy_status(), manager)
    d._on_health(_healthy_status(), manager)  # 恢复 → 计数归零

    # 恢复后重新累计，两次未达阈值 → 不触发。
    d._on_health(_unhealthy_status(), manager)
    d._on_health(_unhealthy_status(), manager)
    assert manager.stop_calls == 0
    # 第三次才触发。
    d._on_health(_unhealthy_status(), manager)
    assert manager.stop_calls == 1


def test_on_health_does_not_force_reconnect_when_process_dead(tmp_path) -> None:
    """进程已死（非假死）不触发强制重连，交给 retry 的 backoff 处理。"""
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    manager = _FakeManager()

    for _ in range(5):
        d._on_health(_unhealthy_status(process_alive=False), manager)
    assert manager.stop_calls == 0


def test_on_health_unknown_never_forces_reconnect(tmp_path) -> None:
    """未知状态无论多少次都不触发强制重连。

    共享/NAT 出口下探测连接失败率很高（实测约 30%），若把它计入阈值，
    监视器会在一条完全健康的隧道上反复开工。“没问到”永远不是证据。
    """
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    manager = _FakeManager()

    for _ in range(20):
        d._on_health(_unknown_status(), manager)
    assert manager.stop_calls == 0
    assert d._health_failures.get("default", 0) == 0


def test_on_health_unknown_does_not_reset_conclusive_failures(tmp_path) -> None:
    """未知不清零计数器：真假死在夹杂探测失败时仍要被抓到。"""
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    manager = _FakeManager()

    d._on_health(_unhealthy_status(), manager)
    d._on_health(_unhealthy_status(), manager)
    d._on_health(_unknown_status(), manager)  # 不计入，也不清零
    assert manager.stop_calls == 0
    assert d._health_failures["default"] == 2

    d._on_health(_unhealthy_status(), manager)  # 第三次“确凿”失败 → 触发
    assert manager.stop_calls == 1


def test_on_health_persists_the_unknown_mark(tmp_path) -> None:
    """显示层靠这两个字段区分“未知”与“异常”，所以它们必须落到状态文件。"""
    from ponte.daemon import _profile_status

    d = TunnelDaemon(_cfg(tmp_path))
    d._on_health(_unknown_status())
    section = _section(d)
    assert section["healthy"] is False
    assert section["health_conclusive"] is False
    assert "255" in section["probe_error"]
    assert section["remote_ports"] == {}, "没观察到的端口不得写成“未监听”"

    # 读回来同样保留这两个字段（"未知"必须能穿过状态文件活到 CLI）。
    status = _profile_status("default", section)
    assert status.healthy is False
    assert status.health_conclusive is False
    assert status.probe_error and "255" in status.probe_error


def test_on_health_persists_conclusive_mark(tmp_path) -> None:
    """确凿失败仍然写成“可判定”，否则重连逻辑就永远不会触发。"""
    d = TunnelDaemon(_cfg(tmp_path))
    d._on_health(_unhealthy_status())
    section = _section(d)
    assert section["health_conclusive"] is True
    assert section["probe_error"] is None


# ---------------------------------------------------------------------------
# 重连统计（tunnel statistics）
# ---------------------------------------------------------------------------


def _live_pid(tmp_path) -> None:
    with open(tmp_path / "ponte.pid", "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))


def test_record_retry_event_full_session(tmp_path) -> None:
    """完整会话周期 → attempts/sessions/uptime/current_session_at 正确累计。"""
    d = TunnelDaemon(_cfg(tmp_path))
    manager = _DurationManager([120.0])

    d._record_retry_event(RetryEvent.connecting(), manager)
    data = _section(d)
    assert data["connect_attempts_total"] == 1
    assert data["sessions_total"] == 0
    assert data["current_session_at"] is not None
    session_at = data["current_session_at"]

    manager.calls = 1  # connect() 返回 → 会话时长变为可读
    d._record_retry_event(RetryEvent.connected(), manager)
    d._record_retry_event(RetryEvent.disconnected(0), manager)
    data = _section(d)
    assert data["sessions_total"] == 1
    assert data["tunnel_uptime_seconds"] == 120.0
    assert data["current_session_at"] is None
    assert data["last_disconnect_at"] is not None
    assert data["last_disconnect_reason"] == "ssh exited with code 0"

    # 会话起点不再保留旧值。
    assert data["current_session_at"] != session_at

    # RETRYING：重连计数 +1，downtime 在下一次 CONNECTING 时闭合。
    d._record_retry_event(RetryEvent.retrying(3.5, 1), manager)
    data = _section(d)
    assert data["reconnects_total"] == 1
    assert data["tunnel_downtime_seconds"] == 0.0  # 尚未闭合

    d._record_retry_event(RetryEvent.connecting(), manager)
    data = _section(d)
    assert data["connect_attempts_total"] == 2
    assert data["tunnel_downtime_seconds"] >= 0.0
    assert data["current_session_at"] is not None

    # 事件流：保留全部 5 条，disconnected 带原因。
    feed = data["recent_events"]
    assert [e["type"] for e in feed] == [
        "connecting", "connected", "disconnected", "retrying", "connecting",
    ]
    assert feed[2]["reason"] == "ssh exited with code 0"
    assert feed[3]["attempt"] == 1
    assert feed[3]["delay"] == 3.5


def test_record_retry_event_launch_failure(tmp_path) -> None:
    """connect() 抛异常（无法启动）→ uptime 不累计，原因来自 error。"""
    d = TunnelDaemon(_cfg(tmp_path))
    manager = _DurationManager([])

    d._record_retry_event(RetryEvent.connecting(), manager)
    d._record_retry_event(
        RetryEvent.disconnected(None, error="OSError: ssh not found"), manager
    )
    data = _section(d)
    assert data["sessions_total"] == 0
    assert data["tunnel_uptime_seconds"] == 0.0
    assert data["last_disconnect_reason"] == "OSError: ssh not found"
    feed = data["recent_events"]
    assert feed[1]["reason"] == "OSError: ssh not found"
    assert "exit_code" not in feed[1]


def test_record_retry_event_feed_is_bounded(tmp_path) -> None:
    """事件流是有界的（最多 _EVENT_FEED_LIMIT 条）。"""
    from ponte.daemon import _EVENT_FEED_LIMIT

    d = TunnelDaemon(_cfg(tmp_path))
    manager = _DurationManager([])  # duration None → uptime 不累计
    for _ in range(_EVENT_FEED_LIMIT + 10):
        d._record_retry_event(RetryEvent.connecting(), manager)
        d._record_retry_event(RetryEvent.disconnected(1), manager)
    data = _section(d)
    assert len(data["recent_events"]) == _EVENT_FEED_LIMIT
    # 最老的事件被淘汰：剩余的最后一条应是最后一轮 disconnected。
    assert data["recent_events"][-1]["type"] == "disconnected"


def test_stats_survive_daemon_restart(tmp_path) -> None:
    """run() 前置合并不清零历史统计（守护进程被服务拉起时保住历史）。

    走真实的 ``_StatusStore.begin()`` 路径：run() 本身要 mock 整个
    retry/health 循环，而这里要验证的正是它的第一步——合并不覆盖。
    """
    d = TunnelDaemon(_cfg(tmp_path))
    _write_status_file(
        d,
        {
            "started_at": 1.0,
            "profiles": {
                "default": {
                    "sessions_total": 7,
                    "connect_attempts_total": 9,
                    "reconnects_total": 2,
                    "tunnel_uptime_seconds": 3600.0,
                    "tunnel_downtime_seconds": 30.0,
                    "recent_events": [{"at": 1.0, "type": "connected"}],
                }
            },
        },
    )
    d._store.begin(["default"], started_at=2.0)

    merged = _section(d)
    assert merged["sessions_total"] == 7
    assert merged["connect_attempts_total"] == 9
    assert merged["reconnects_total"] == 2
    assert merged["tunnel_uptime_seconds"] == 3600.0
    assert merged["tunnel_downtime_seconds"] == 30.0
    assert len(merged["recent_events"]) == 1
    # 只有 started_at（进程运行时长）重置。
    assert d._store.started_at() == 2.0


def test_begin_migrates_and_primes_every_profile(tmp_path) -> None:
    """begin() 把旧扁平文件迁成 default profile，并为新 profile 补零。"""
    d = TunnelDaemon(_cfg(tmp_path))
    _write_status_file(d, {"started_at": 1.0, "sessions_total": 7, "healthy": True})
    d._store.begin(["default", "offsite"], started_at=2.0)
    sections = d._store.read_profiles()
    assert sections["default"]["sessions_total"] == 7
    assert sections["default"]["healthy"] is True
    assert sections["offsite"]["sessions_total"] == 0
    assert sections["offsite"]["tunnel_uptime_seconds"] == 0.0
    assert sections["offsite"]["recent_events"] == []


def test_status_surfaces_statistics(tmp_path) -> None:
    """status() 把统计字段从 JSON 透出到 ProfileStatus。"""
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    _live_pid(tmp_path)
    now = time.time()
    _write_status_file(
        d,
        {
            "started_at": now - 100,
            "profiles": {
                "default": {
                    "healthy": True,
                    "remote_ports": {"23334": True},
                    "connect_attempts_total": 5,
                    "sessions_total": 4,
                    "reconnects_total": 3,
                    "tunnel_uptime_seconds": 400.0,
                    "tunnel_downtime_seconds": 100.0,
                    "current_session_at": now - 50,
                    "last_disconnect_at": now - 60,
                    "last_disconnect_reason": "ssh exited with code 255",
                    "recent_events": [{"at": now, "type": "connecting"}],
                }
            },
        },
    )
    s = d.status()
    profile = s.profiles[0]
    assert profile.name == "default"
    assert profile.connect_attempts_total == 5
    assert profile.sessions_total == 4
    assert profile.reconnects_total == 3
    assert profile.tunnel_uptime_seconds == 400.0
    assert profile.tunnel_downtime_seconds == 100.0
    assert profile.current_session_at is not None
    assert profile.current_session_at <= now
    assert profile.last_disconnect_reason == "ssh exited with code 255"
    assert profile.recent_events == [{"at": now, "type": "connecting"}]
    # 派生属性
    assert profile.session_uptime is not None
    assert profile.availability is not None
    assert abs(profile.availability - 0.8) < 1e-9  # 400 / (400 + 100)
    assert s.uptime_seconds >= 100.0


# ---------------------------------------------------------------------------
# 多 profile（一个守护进程监督多条 SSH 连接）
# ---------------------------------------------------------------------------


def _two_profile_cfg(tmp_path) -> TunnelConfig:
    """A config with two profiles, each with its own endpoint and tunnel."""
    cfg = _cfg(tmp_path)
    source = cfg.profiles[0]
    return dataclasses.replace(
        cfg,
        profiles=[
            Profile(
                name=name,
                ssh=dataclasses.replace(source.ssh, host=f"{name}.example.com"),
                tunnels=source.tunnels,
            )
            for name in ("web", "db")
        ],
    )


class _ProfileManager(_DurationManager):
    """Manager stand-in for ProfileRunner tests: no probes, known duration."""

    def __init__(self, duration: float) -> None:
        super().__init__([duration])
        self.calls = 1  # one completed session → last_session_duration is set
        self.stop_calls = 0

    def is_running(self) -> bool:
        return True

    def check_remote_ports(self, timeout: int = 10) -> dict[int, bool]:
        return {}

    def check_local_ports(self, timeout: float = 1.0) -> dict[int, bool]:
        return {}

    def stop(self) -> None:
        self.stop_calls += 1


class _ScriptedRetry:
    """RetryRunner stand-in that yields a fixed event list and then ends."""

    def __init__(self, events: list[RetryEvent]) -> None:
        self.events = events
        self.stop_calls = 0

    def run(self, _manager):
        yield from self.events

    def stop(self) -> None:
        self.stop_calls += 1


def test_profile_runner_records_events_for_its_own_profile(tmp_path) -> None:
    """一个 profile 的运行时把事件折进自己的 section，并同步停止 SSH。"""
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    manager = _ProfileManager(42.0)
    runner = ProfileRunner(
        cfg.profiles[0],
        cfg,
        d,
        manager=manager,
        retry_runner=_ScriptedRetry(
            [
                RetryEvent.connecting(),
                RetryEvent.connected(),
                RetryEvent.disconnected(1, error="boom"),
            ]
        ),
    )
    runner.start()
    runner.finish()

    section = _section(d)
    assert section["connect_attempts_total"] == 1
    assert section["sessions_total"] == 1
    assert section["tunnel_uptime_seconds"] == 42.0
    assert section["last_disconnect_reason"] == "boom"
    assert manager.stop_calls >= 1, "停止时必须收起 SSH 会话"
    assert runner.error is None


def test_profile_runner_records_crash(tmp_path) -> None:
    """一个 profile 的循环崩掉 → 记入状态文件，而不是静默死掉。"""

    class _BoomRetry:
        def run(self, _manager):
            raise RuntimeError("retry boom")
            yield  # pragma: no cover - unreachable, keeps this a generator

        def stop(self) -> None:
            pass

    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    runner = ProfileRunner(
        cfg.profiles[0], cfg, d, manager=_ProfileManager(1.0), retry_runner=_BoomRetry()
    )
    runner.start()
    runner.finish()

    assert "retry boom" in (runner.error or "")
    assert "retry boom" in (_section(d).get("error") or "")


def test_run_supervises_every_profile(tmp_path, monkeypatch) -> None:
    """run() 为每个 profile 起一个 runner，全部结束后退出并收尾。"""
    import signal as signal_module

    started: list[str] = []
    aborted: list[str] = []

    class _StubRunner:
        def __init__(self, profile, config, daemon, **_kw) -> None:
            self.profile = profile

        def start(self) -> None:
            started.append(self.profile.name)

        def abort(self) -> None:
            aborted.append(self.profile.name)

        def finish(self) -> None:
            pass

        def is_alive(self) -> bool:
            return False  # 立即结束，避免测试挂住

    monkeypatch.setattr("ponte.daemon.ProfileRunner", _StubRunner)
    monkeypatch.setattr(TunnelDaemon, "_setup_logging", lambda self: None)
    monkeypatch.setattr(TunnelDaemon, "write_pid", lambda self: None)
    monkeypatch.setattr(TunnelDaemon, "_cleanup", lambda self: None)
    monkeypatch.setattr(TunnelDaemon, "_watch_stop_marker", lambda self, cb: None)
    monkeypatch.setattr(signal_module, "signal", lambda *_a: None)

    d = TunnelDaemon(_two_profile_cfg(tmp_path))
    assert d.run() == 0
    assert started == ["web", "db"]
    assert aborted == ["web", "db"]
    assert set(d._store.read_profiles()) == {"web", "db"}


def test_status_lists_config_profiles_in_order(tmp_path) -> None:
    """status() 按配置顺序列出每个 profile，未上报的显示未知。"""
    d = TunnelDaemon(_two_profile_cfg(tmp_path))
    _live_pid(tmp_path)
    d._store.begin(["web"], started_at=time.time())
    d._on_health(_unhealthy_status(), profile="web")

    s = d.status()
    assert [profile.name for profile in s.profiles] == ["web", "db"]
    assert s.profiles[0].healthy is False
    assert s.profiles[1].healthy is None
    assert s.healthy is False


def test_profile_names_follow_the_config(tmp_path) -> None:
    assert TunnelDaemon(_two_profile_cfg(tmp_path)).profile_names == ["web", "db"]
    assert TunnelDaemon(_cfg(tmp_path)).profile_names == ["default"]


# ---------------------------------------------------------------------------
# 进程参数 / 工作目录 / 强制终止提示
# ---------------------------------------------------------------------------


def test_daemon_args_pass_config_explicitly(tmp_path) -> None:
    """服务方式启动时必须带上 --config，否则可能解析到另一份配置。"""
    cfg = _cfg(tmp_path)
    cfg = dataclasses.replace(cfg, source_path=str(tmp_path / "config.toml"))
    args = TunnelDaemon(cfg)._daemon_args()
    assert args[:2] == ["-m", "ponte.main"]
    assert "--config" in args
    assert args[args.index("--config") + 1] == str(tmp_path / "config.toml")
    assert args[-2:] == ["start", "--foreground"]


def test_daemon_args_string_quotes_paths_with_spaces(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg = dataclasses.replace(
        cfg, source_path=str(tmp_path / "my config" / "config.toml")
    )
    rendered = TunnelDaemon(cfg)._daemon_args_string()
    assert '"' in rendered
    assert rendered.startswith("-m ponte.main")


def test_work_dir_is_config_directory_not_package_parent(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg = dataclasses.replace(cfg, source_path=str(tmp_path / "config.toml"))
    assert TunnelDaemon(cfg).work_dir == str(tmp_path)


def test_work_dir_leaves_the_package_directory(monkeypatch, tmp_path) -> None:
    """旧布局（配置就放在包目录里）不能拿包目录当工作目录。

    那里 `python -m ponte.main` 根本导入不到包：sys.path 上需要的是包的**父目录**。
    子进程会秒死， ponte start 只报“未写 PID 文件”，ponte install 则注册一个
    永远起不来的任务。
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    monkeypatch.setattr("ponte.daemon.package_dir", lambda: str(pkg))
    cfg = dataclasses.replace(_cfg(tmp_path), source_path=str(pkg / "config.toml"))
    assert TunnelDaemon(cfg).work_dir == str(tmp_path)


def test_spawn_failure_includes_what_the_child_said(tmp_path) -> None:
    """子进程什么也没说时，报错要把它残留的输出带上，而不是只给一个超时。"""
    d = TunnelDaemon(_cfg(tmp_path))
    os.makedirs(os.path.dirname(d.pid_file), exist_ok=True)
    with open(d.pid_file + ".spawn.log", "wb") as handle:
        handle.write(b"ModuleNotFoundError: No module named 'ponte'\n")

    assert "ModuleNotFoundError" in daemon._spawn_tail(d.pid_file + ".spawn.log")
    assert daemon._spawn_tail(str(tmp_path / "missing.log")) == ""


def test_work_dir_falls_back_to_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cfg = _cfg(tmp_path)
    cfg = dataclasses.replace(cfg, source_path="/definitely/missing/config.toml")
    assert TunnelDaemon(cfg).work_dir == os.path.expanduser("~")


def test_stop_reports_force_kill_in_message(monkeypatch, tmp_path) -> None:
    """优雅停止失败时必须告诉用户用了强杀（此前提示永远不会出现）。"""
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    d.write_pid()
    monkeypatch.setattr(d, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(d, "_stop_autostart", lambda: None)
    monkeypatch.setattr(d, "_force_kill", lambda _pid: None)

    status = d.stop(timeout=0.1)
    assert status.running is False
    assert "强制" in status.message


def test_stop_graceful_has_no_kill_message(monkeypatch, tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)
    d.write_pid()
    alive = {"n": 0}

    def _pid_alive(_pid: int) -> bool:
        alive["n"] += 1
        return alive["n"] == 1  # 第一次（status）活着，之后已退出

    monkeypatch.setattr(d, "_pid_alive", _pid_alive)
    monkeypatch.setattr(d, "_stop_autostart", lambda: None)
    status = d.stop(timeout=0.1)
    assert "强制" not in status.message
    assert "kill" not in status.message.lower()


# ---------------------------------------------------------------------------
# 服务安装 / 卸载（subprocess 全部 mock，跨平台可跑）
# ---------------------------------------------------------------------------


def _record_run(record: list[list[str]]):
    def _run(args, **_kwargs):
        record.append(list(args))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    return _run


def test_install_systemd_writes_unit_with_config(monkeypatch, tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg = dataclasses.replace(cfg, source_path=str(tmp_path / "config.toml"))
    d = TunnelDaemon(cfg)
    home = tmp_path / "home"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "ponte.daemon.os.path.expanduser", lambda p: str(home) + p[1:]
    )
    record: list[list[str]] = []
    monkeypatch.setattr("ponte.daemon.subprocess.run", _record_run(record))

    assert d._install_systemd() == "installed"

    unit_path = home / ".config" / "systemd" / "user" / "ponte.service"
    unit = unit_path.read_text(encoding="utf-8")
    assert "Restart=always" in unit
    assert "--config" in unit
    assert f'WorkingDirectory={tmp_path}' in unit
    assert ["systemctl", "--user", "enable", "--now", "ponte.service"] in record


def test_uninstall_systemd_removes_unit(monkeypatch, tmp_path) -> None:
    d = TunnelDaemon(_cfg(tmp_path))
    home = tmp_path / "home"
    unit_path = home / ".config" / "systemd" / "user" / "ponte.service"
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "ponte.daemon.os.path.expanduser", lambda p: str(home) + p[1:]
    )
    monkeypatch.setattr("ponte.daemon.subprocess.run", _record_run([]))

    assert d._uninstall_systemd() == "uninstalled"
    assert not unit_path.exists()


def test_install_launchd_escapes_and_removes(monkeypatch, tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg = dataclasses.replace(cfg, source_path=str(tmp_path / "a&b.toml"))
    d = TunnelDaemon(cfg)
    home = tmp_path / "home"
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "ponte.daemon.os.path.expanduser", lambda p: str(home) + p[1:]
    )
    record: list[list[str]] = []
    monkeypatch.setattr("ponte.daemon.subprocess.run", _record_run(record))

    assert d._install_launchd() == "installed"
    plist_path = home / "Library" / "LaunchAgents" / "com.modusensus.ponte.plist"
    plist = plist_path.read_text(encoding="utf-8")
    assert "KeepAlive" in plist
    assert "a&amp;b.toml" in plist  # XML 转义，避免畸形 plist
    loaded = [cmd for cmd in record if cmd[:3] == ["launchctl", "load", "-w"]]
    assert loaded, record
    assert os.path.basename(loaded[0][3]) == "com.modusensus.ponte.plist"

    assert d._uninstall_launchd() == "uninstalled"
    assert not plist_path.exists()


def test_spawn_background_returns_child_pid(monkeypatch, tmp_path) -> None:
    cfg = _cfg(tmp_path)
    d = TunnelDaemon(cfg)

    class _Popen:
        def __init__(self, cmd, **_kwargs) -> None:
            self.cmd = cmd

    def _popen(cmd, **kwargs):
        proc = _Popen(cmd, **kwargs)
        # 子进程“启动后”写下 PID
        d.write_pid()
        return proc

    monkeypatch.setattr("ponte.daemon.subprocess.Popen", _popen)
    assert d._spawn_background() == os.getpid()


def test_install_service_dispatch_rejects_unknown_platform(monkeypatch, tmp_path) -> None:
    d = TunnelDaemon(_cfg(tmp_path))
    monkeypatch.setattr(sys, "platform", "aix")

    with pytest.raises(RuntimeError):
        d.install_service()
    with pytest.raises(RuntimeError):
        d.uninstall_service()


# ---------------------------------------------------------------------------
# 不得弹窗：外部工具调用必须一律经过 creation_flags()
# ---------------------------------------------------------------------------


def _record_run_with_kwargs() -> tuple[list[tuple[list[str], dict]], object]:
    """记录 (args, kwargs)，用于断言 creationflags 是否被传递。"""
    calls: list[tuple[list[str], dict]] = []

    def _run(args, **kwargs):
        calls.append((list(args), dict(kwargs)))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    return calls, _run


def test_force_kill_hides_console_on_windows(monkeypatch, tmp_path) -> None:
    """taskkill 必须带 creationflags，否则停止时会闪出黑色控制台窗口。"""
    d = TunnelDaemon(_cfg(tmp_path))
    calls, recorder = _record_run_with_kwargs()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("ponte.daemon.subprocess.run", recorder)

    d._force_kill(4242)

    (args, kwargs), = calls
    assert args[:2] == ["taskkill", "/PID"]
    assert "creationflags" in kwargs
    assert kwargs["creationflags"] == creation_flags()


def test_install_scheduled_task_refuses_without_pythonw(monkeypatch, tmp_path) -> None:
    """没有无窗口解释器时 install 必须报错，而不是注册一个每次登录弹窗的任务。"""
    d = TunnelDaemon(_cfg(tmp_path, run_as="user"))
    monkeypatch.setattr(sys, "platform", "win32")
    _windows_path_semantics(
        monkeypatch, executable=r"C:\Python\python.exe", existing=set()
    )
    registered: list[str] = []
    monkeypatch.setattr(
        d, "_run_powershell", lambda script: registered.append(script) or "installed"
    )

    with pytest.raises(RuntimeError, match="pythonw"):
        d.install_scheduled_task()

    assert registered == []  # 计划任务一枚都不能注册出去


def test_service_tool_calls_go_through_run_tool(monkeypatch, tmp_path) -> None:
    """systemctl / launchctl 也必须经 _run_tool（统一带上 creationflags）。"""
    calls, recorder = _record_run_with_kwargs()
    monkeypatch.setattr("ponte.daemon.subprocess.run", recorder)

    _run_tool(["systemctl", "--user", "daemon-reload"])

    (args, kwargs), = calls
    assert args == ["systemctl", "--user", "daemon-reload"]
    assert kwargs["creationflags"] == creation_flags()
    assert kwargs["capture_output"] is True and kwargs["text"] is True


def test_status_names_the_target_of_each_profile(tmp_path) -> None:
    """每条隧道都要能说出自己连的是哪台服务器。

    目标来自配置而不是状态文件（状态文件里就没有这个信息），所以它不能
    因为 daemon 刚重启、还没上报而消失——否则看板上会出现一栏“健康的
    未知服务器”，而多隧道时那正是最需要弄清楚的一件事。
    """
    cfg = _two_profile_cfg(tmp_path)
    d = TunnelDaemon(cfg)
    _live_pid(tmp_path)
    d._store.begin(["web", "db"], started_at=time.time())

    s = d.status()

    assert [profile.destination for profile in s.profiles] == [
        profile.destination for profile in cfg.profiles
    ]
    # 字面断言（而不是子串包含）：既钉死格式，也更严格。
    assert s.profiles[0].destination == "testuser@web.example.com"


def test_status_leaves_the_target_unknown_for_a_dropped_profile(tmp_path) -> None:
    """配置里已删掉的 profile 还会显示（daemon 仍管着它），但没有目标可报。"""
    d = TunnelDaemon(_cfg(tmp_path))
    _live_pid(tmp_path)
    d._store.begin(["default", "ghost"], started_at=time.time())

    ghost = d.status().get_profile("ghost")

    assert ghost is not None
    assert ghost.destination is None
    assert ghost.jump is None


def test_status_carries_the_jump_chain_from_the_config(tmp_path) -> None:
    """跳板机链也要从配置带进 status：否则看板上根本看不到它。

    它是 *配置* 里的事实而不是状态文件里的事实，所以和 destination 一样，
    在 daemon 刚重启、还没上报时就该已经能回答“经哪儿出去”。
    """
    from ponte.config import JumpHop

    cfg = _two_profile_cfg(tmp_path)
    profiles = [
        dataclasses.replace(
            profile,
            ssh=dataclasses.replace(
                profile.ssh,
                jumps=(JumpHop(host="bastion", user="ops", port=2222),)
                if profile.name == "web"
                else (),
            ),
        )
        for profile in cfg.profiles
    ]
    d = TunnelDaemon(dataclasses.replace(cfg, profiles=profiles))
    _live_pid(tmp_path)
    d._store.begin(["web", "db"], started_at=time.time())

    s = d.status()

    assert s.get_profile("web").jump == "ops@bastion:2222"
    assert s.get_profile("db").jump is None, "没有跳板机的 profile 不得凭空多出一条链"


# ---------------------------------------------------------------------------
# 配置热重载：ponte reload / SIGHUP
# ---------------------------------------------------------------------------


class _RecordingRunner:
    """``ProfileRunner`` stand-in for reload tests: records lifecycle, no SSH."""

    instances: list[_RecordingRunner] = []

    def __init__(self, profile, config, daemon, **_kwargs) -> None:
        self.profile = profile
        self.notifier = daemon.notifier
        self.started = False
        self.finished = False
        _RecordingRunner.instances.append(self)

    def start(self) -> None:
        self.started = True

    def abort(self) -> None:
        pass

    def finish(self) -> None:
        self.finished = True

    def is_alive(self) -> bool:
        return self.started and not self.finished


def _write_profiles(
    tmp_path, specs, *, extra: str = ""
) -> TunnelConfig:
    """Write a profiles-layout config (real key file) and return it loaded.

    *specs* is an iterable of ``(name, host)`` pairs, so a test can change just
    one endpoint and prove only that profile is restarted.
    """
    key = tmp_path / "id_rsa"
    key.write_text("x", encoding="utf-8")
    blocks = ""
    for name, host in specs:
        blocks += f"""
[[profiles]]
name = "{name}"
  [profiles.ssh]
  host = "{host}"
  user = "u"
  identity_file = "{key.as_posix()}"
  [[profiles.tunnels]]
  remote_port = 23334
  local_host = "localhost"
  local_port = 2222
"""
    # pid/log must live in tmp_path: the platform default state dir belongs to
    # the real user, and these tests would otherwise read each other's status
    # files (and touch the operator's own).
    daemon_section = (
        "[daemon]\n"
        f'pid_file = "{(tmp_path / "ponte.pid").as_posix()}"\n'
        f'log_file = "{(tmp_path / "ponte.log").as_posix()}"\n'
    )
    path = tmp_path / "config.toml"
    path.write_text(extra + daemon_section + blocks, encoding="utf-8")
    return load_config(str(path))


def _reload_daemon(tmp_path, monkeypatch, specs):
    """A daemon whose runners are recording stubs, one per *specs* entry."""
    _RecordingRunner.instances = []
    monkeypatch.setattr("ponte.daemon.ProfileRunner", _RecordingRunner)
    cfg = _write_profiles(tmp_path, specs)
    d = TunnelDaemon(cfg)
    d._runners = [_RecordingRunner(p, cfg, d) for p in cfg.profiles]
    for runner in d._runners:
        runner.start()
    return d


def test_derive_reload_marker_from_pid() -> None:
    assert _derive_reload_marker(r"C:\x\ponte.pid") == r"C:\x\ponte.reload"


def test_request_reload_writes_marker(tmp_path) -> None:
    """请求重载只写标记文件（跨进程），由守护进程自己消费。"""
    d = TunnelDaemon(_cfg(tmp_path))
    assert not os.path.exists(d.reload_marker)
    d.request_reload()
    assert os.path.exists(d.reload_marker)


def test_reconcile_keeps_unchanged_profiles(tmp_path, monkeypatch) -> None:
    """配置没变的隧道重载后还是同一个 runner——连接不会被打断。"""
    d = _reload_daemon(
        tmp_path, monkeypatch, (("web", "web.example.com"), ("db", "db.example.com"))
    )
    before = list(d._runners)

    summary = d._reconcile()

    assert d._runners == before
    assert all(not runner.finished for runner in before)
    assert "保持 2 条" in summary


def test_reconcile_restarts_only_the_changed_profile(tmp_path, monkeypatch) -> None:
    """改一台服务器只重启那一条，其余（含新增）各归各位。"""
    d = _reload_daemon(
        tmp_path, monkeypatch, (("web", "web.example.com"), ("db", "db.example.com"))
    )
    old = {runner.profile.name: runner for runner in d._runners}

    # web 换了一台机器，db 原样，另加一条 cache。
    _write_profiles(
        tmp_path,
        (
            ("web", "web2.example.com"),
            ("db", "db.example.com"),
            ("cache", "cache.example.com"),
        ),
    )
    summary = d._reconcile()

    current = {runner.profile.name: runner for runner in d._runners}
    assert list(current) == ["web", "db", "cache"]  # 顺序跟随配置
    assert current["db"] is old["db"] and not old["db"].finished
    assert current["web"] is not old["web"] and old["web"].finished
    assert current["cache"].started
    assert "重启/新增" in summary
    assert set(d._store.read_profiles()) == {"web", "db", "cache"}


def test_reconcile_removes_profiles_that_left_the_config(tmp_path, monkeypatch) -> None:
    """配置里删掉的隧道要停掉，状态文件里也不再残留它的旧健康数据。"""
    d = _reload_daemon(
        tmp_path, monkeypatch, (("web", "web.example.com"), ("db", "db.example.com"))
    )
    old = {runner.profile.name: runner for runner in d._runners}

    _write_profiles(tmp_path, (("web", "web.example.com"),))
    summary = d._reconcile()

    assert [runner.profile.name for runner in d._runners] == ["web"]
    assert old["db"].finished
    assert "移除 1 条" in summary
    assert set(d._store.read_profiles()) == {"web"}


def test_reconcile_rebuilds_everything_when_policy_changes(tmp_path, monkeypatch) -> None:
    """retry/health 是写进 runner 的策略，改了就必须整条重建。"""
    d = _reload_daemon(
        tmp_path, monkeypatch, (("web", "web.example.com"), ("db", "db.example.com"))
    )
    old = {runner.profile.name: runner for runner in d._runners}

    _write_profiles(
        tmp_path,
        (("web", "web.example.com"), ("db", "db.example.com")),
        extra="[retry]\nbase_delay = 7\n",
    )
    summary = d._reconcile()

    assert all(old[name].finished for name in old)
    assert all(runner is not old[runner.profile.name] for runner in d._runners)
    assert "retry/health" in summary


def test_reconcile_keeps_running_config_on_a_broken_file(tmp_path, monkeypatch) -> None:
    """配置文件写坏了必须原地保留：一个笔误不能把正在跑的隧道拆掉。"""
    d = _reload_daemon(tmp_path, monkeypatch, (("web", "web.example.com"),))
    before = list(d._runners)

    (tmp_path / "config.toml").write_text("not = toml = =", encoding="utf-8")
    summary = d._reconcile()

    assert "重载失败" in summary
    assert d._runners == before
    assert not before[0].finished
