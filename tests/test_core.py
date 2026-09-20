"""pytest tests for :mod:`ponte.core` (SSH arg building + port probe parsing).

No real SSH is spawned: ``_run_capture`` (or ``Popen`` underneath it) is patched
and the probe/connect command parsing is exercised directly.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys

import pytest

from _waits import wait_for
from ponte.config import (
    JumpHop,
    Profile,
    SSHConfig,
    SSHOptions,
    Tunnel,
    TunnelConfig,
    WindowsConfig,
)
from ponte.core import (
    ProbeError,
    TunnelManager,
    _find_ssh,
    _run_capture,
    creation_flags,
)

# ``CREATE_NO_WINDOW`` is a Windows-only constant missing from ``subprocess``
# on POSIX. Referencing it directly would make the Windows-flag tests fail at
# import/collection on Linux/macOS, so pin the documented value (0x08000000)
# and compare against that instead.
_CREATE_NO_WINDOW_VALUE = 0x08000000


def _cfg(*, host: str = "example.com", user: str = "testuser", port: int = 22,
         ssh_exe: str = "/usr/bin/ssh",
         tunnels: list[Tunnel] | None = None) -> TunnelConfig:
    return TunnelConfig(
        profiles=[
            Profile(
                name="default",
                ssh=SSHConfig(
                    host=host,
                    user=user,
                    identity_file="/keys/id_rsa",
                    port=port,
                    known_hosts_file="/keys/known_hosts",
                    options=SSHOptions(),
                ),
                tunnels=tunnels if tunnels is not None else [
                    Tunnel(remote_port=23334, local_host="localhost", local_port=2222),
                    Tunnel(remote_port=17897, local_host="localhost", local_port=7897),
                ],
            )
        ],
        windows=WindowsConfig(ssh_exe=ssh_exe),
    )


def _flag_pairs(args: list[str], flag: str) -> list[tuple[str, str]]:
    """Return every ``(flag, spec)`` pair for *flag* in an ssh command line."""
    return [
        (args[i], args[i + 1]) for i in range(len(args) - 1) if args[i] == flag
    ]


def test_build_args_full(monkeypatch) -> None:
    monkeypatch.setattr("ponte.core._find_ssh", lambda _cfg: "/usr/bin/ssh")
    tm = TunnelManager(_cfg())
    args = tm.build_args()
    assert args[0] == "/usr/bin/ssh"
    assert "-i" in args
    assert args[args.index("-i") + 1] == "/keys/id_rsa"
    # 每条 -R 规则独立成对
    pairs = [args[i : i + 2] for i in range(len(args)) if args[i] == "-R"]
    assert ("-R", "23334:localhost:2222") in [tuple(p) for p in pairs]
    assert ("-R", "17897:localhost:7897") in [tuple(p) for p in pairs]
    # 默认 22 端口不加 -p
    assert "-p" not in args
    assert args[-1] == "testuser@example.com"


def test_build_args_custom_port(monkeypatch) -> None:
    monkeypatch.setattr("ponte.core._find_ssh", lambda _cfg: "/usr/bin/ssh")
    tm = TunnelManager(_cfg(port=2222))
    args = tm.build_args()
    assert args[args.index("-p") + 1] == "2222"


def test_find_ssh_uses_python_ssh(monkeypatch) -> None:
    # PATH 命中 ssh 时用 PATH（非 Windows 分支）
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("ponte.core.shutil.which", lambda _name: "/usr/local/bin/ssh")
    assert _find_ssh(_cfg()) == "/usr/local/bin/ssh"


def test_find_ssh_windows_config(monkeypatch) -> None:
    # Windows 且配置了 ssh_exe 且文件存在 → 用配置值
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("ponte.core.shutil.which", lambda _name: "/not/used")
    monkeypatch.setattr("ponte.core.os.path.isfile", lambda p: p == r"D:\Git\usr\bin\ssh.exe")
    cfg = _cfg(ssh_exe=r"D:\Git\usr\bin\ssh.exe")
    assert _find_ssh(cfg) == r"D:\Git\usr\bin\ssh.exe"


def test_test_connection_ok(monkeypatch) -> None:
    monkeypatch.setattr("ponte.core._run_capture", lambda *a, **k: (0, "OK"))
    tm = TunnelManager(_cfg())
    assert tm.test_connection(timeout=5) is True


def test_test_connection_fail(monkeypatch) -> None:
    monkeypatch.setattr("ponte.core._run_capture", lambda *a, **k: (1, ""))
    tm = TunnelManager(_cfg())
    assert tm.test_connection(timeout=5) is False


def test_check_remote_ports_python_probe(monkeypatch) -> None:
    # 服务器端 python3 分支：输出空格分隔的开放端口
    monkeypatch.setattr("ponte.core._run_capture", lambda *a, **k: (0, "23334\n"))
    tm = TunnelManager(_cfg())
    assert tm.check_remote_ports(timeout=5) == {23334: True, 17897: False}


def test_check_remote_ports_tool_fallback(monkeypatch) -> None:
    # 回退分支：ss 风格输出 ``*:23334 `` token
    monkeypatch.setattr(
        "ponte.core._run_capture",
        lambda *a, **k: (0, "tcp LISTEN 0 128 0.0.0.0:23334 users:(())\n"),
    )
    tm = TunnelManager(_cfg())
    assert tm.check_remote_ports(timeout=5) == {23334: True, 17897: False}


def test_check_remote_ports_error_is_unknown_not_down(monkeypatch) -> None:
    """探针自己建不起来 → 状态未知，绝不能报成“端口未监听”。

    这条区别就是“没问到”和“问了、答案是没在听”的区别：把前者报成后者，
    健康监视器连续几次就会把一条完全正常的隧道强行重连。
    """

    def _boom(*_args, **_kwargs):
        raise subprocess.SubprocessError("boom")

    monkeypatch.setattr("ponte.core._run_capture", _boom)
    tm = TunnelManager(_cfg())
    with pytest.raises(ProbeError) as excinfo:
        tm.check_remote_ports(timeout=5)
    assert "23334" in str(excinfo.value) and "未知" in str(excinfo.value)


def test_check_remote_ports_nonzero_exit_is_unknown(monkeypatch) -> None:
    """ssh 以非 0 退出（认证失败/被重置/被限速/被超时 kill）→ 检查命令压根没跑。"""
    monkeypatch.setattr("ponte.core._run_capture", lambda *a, **k: (255, ""))
    with pytest.raises(ProbeError) as excinfo:
        TunnelManager(_cfg()).check_remote_ports(timeout=5)
    assert "255" in str(excinfo.value)


def test_check_remote_ports_wedged_pipe_is_unknown(monkeypatch) -> None:
    """``_run_capture`` 放弃读取时返回 ``(None, "")`` —— 同样只能算未知。"""
    monkeypatch.setattr("ponte.core._run_capture", lambda *a, **k: (None, ""))
    with pytest.raises(ProbeError):
        TunnelManager(_cfg()).check_remote_ports(timeout=5)


def test_check_remote_ports_probe_that_ran_may_still_report_closed(monkeypatch) -> None:
    """探针跑通了、输出里没有这个端口 → 这才是真正的“未监听”（可据以告警）。"""
    monkeypatch.setattr("ponte.core._run_capture", lambda *a, **k: (0, "23334\n"))
    assert TunnelManager(_cfg()).check_remote_ports(timeout=5) == {
        23334: True,
        17897: False,
    }


# ---------------------------------------------------------------------------
# _run_capture —— 探针读取必须永远有界
# ---------------------------------------------------------------------------


def _fake_popen(monkeypatch, *, stdout: str = "", returncode: int = 0,
                timeouts: int = 0, captured: dict | None = None):
    """Patch ``Popen`` with a probe stand-in; *timeouts* is how many
    ``communicate`` calls raise :class:`subprocess.TimeoutExpired` first."""
    state = {"timeouts": timeouts, "killed": False}

    class _Proc:
        def __init__(self) -> None:
            self.returncode = returncode

        def communicate(self, timeout=None):
            if state["timeouts"] > 0:
                state["timeouts"] -= 1
                raise subprocess.TimeoutExpired(cmd="ssh", timeout=timeout)
            return stdout, ""

        def kill(self) -> None:
            state["killed"] = True

    def _popen(args, **kwargs):
        if captured is not None:
            captured["args"] = args
            captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr("ponte.core.subprocess.Popen", _popen)
    return state


def test_run_capture_returns_stdout(monkeypatch) -> None:
    _fake_popen(monkeypatch, stdout="23334\n", returncode=0)
    assert _run_capture(["ssh", "host"], timeout=5) == (0, "23334\n")


def test_run_capture_never_inherits_handles(monkeypatch) -> None:
    """探针不得继承/泄漏句柄：stdin 丢弃、close_fds 打开。

    继承来的管道写端会让 ``communicate()`` 永远等不到 EOF，这正是健康监视器
    卡死数小时的成因。
    """
    captured: dict = {}
    _fake_popen(monkeypatch, captured=captured)
    _run_capture(["ssh", "host"], timeout=5)
    assert captured["close_fds"] is True
    assert captured["stdin"] is subprocess.DEVNULL


def test_run_capture_hangs_up_after_killing_a_wedged_probe(monkeypatch) -> None:
    """超时被 kill 后若管道仍不关闭，必须放弃输出而不是永远阻塞调用方。"""
    state = _fake_popen(monkeypatch, timeouts=2)
    assert _run_capture(["ssh", "host"], timeout=0.1) == (None, "")
    assert state["killed"] is True


def test_run_capture_keeps_output_when_the_killed_probe_still_reports(monkeypatch) -> None:
    """超时后能读到的输出仍然有用（例如 ssh 已经把端口列表写出来了）。"""
    _fake_popen(monkeypatch, stdout="23334", returncode=255, timeouts=1)
    assert _run_capture(["ssh", "host"], timeout=0.1) == (255, "23334")


# ---------------------------------------------------------------------------
# 隧道类型：-R / -L / -D
# ---------------------------------------------------------------------------


def test_build_args_mixed_tunnel_kinds(monkeypatch) -> None:
    """-R / -L / -D 混用时各自生成独立的转发参数，共用一条连接。"""
    monkeypatch.setattr("ponte.core._find_ssh", lambda _cfg: "/usr/bin/ssh")
    tm = TunnelManager(
        _cfg(
            tunnels=[
                Tunnel(remote_port=23334, local_host="localhost", local_port=2222),
                Tunnel(
                    remote_port=5432,
                    local_host="127.0.0.1",
                    local_port=8080,
                    kind="local",
                    remote_host="db.internal",
                ),
                Tunnel(
                    remote_port=None,
                    local_host="127.0.0.1",
                    local_port=1080,
                    kind="dynamic",
                ),
            ]
        )
    )
    args = tm.build_args()
    assert ("-R", "23334:localhost:2222") in _flag_pairs(args, "-R")
    assert ("-L", "127.0.0.1:8080:db.internal:5432") in _flag_pairs(args, "-L")
    assert ("-D", "127.0.0.1:1080") in _flag_pairs(args, "-D")
    assert args[-1] == "testuser@example.com"


def test_build_args_remote_bind_address_opt_in(monkeypatch) -> None:
    """-R 只有显式配置 remote_host 时才带服务器侧绑定地址（默认行为不变）。"""
    monkeypatch.setattr("ponte.core._find_ssh", lambda _cfg: "/usr/bin/ssh")
    plain = TunnelManager(_cfg(tunnels=[Tunnel(23334, "localhost", 2222)]))
    assert _flag_pairs(plain.build_args(), "-R") == [("-R", "23334:localhost:2222")]

    bound = TunnelManager(
        _cfg(
            tunnels=[
                Tunnel(23334, "localhost", 2222, remote_host="0.0.0.0"),
            ]
        )
    )
    assert _flag_pairs(bound.build_args(), "-R") == [
        ("-R", "0.0.0.0:23334:localhost:2222")
    ]


def test_check_remote_ports_skips_non_remote_kinds(monkeypatch) -> None:
    """只有 -R 才有服务器侧端口；没有 -R 时不得多开一条 SSH 连接。"""
    monkeypatch.setattr("ponte.core._find_ssh", lambda _cfg: "/usr/bin/ssh")
    calls: list[tuple] = []
    monkeypatch.setattr(
        "ponte.core._run_capture", lambda args, **k: calls.append(args)
    )
    tm = TunnelManager(
        _cfg(
            tunnels=[
                Tunnel(None, "127.0.0.1", 1080, kind="dynamic"),
                Tunnel(
                    5432, "127.0.0.1", 8080, kind="local", remote_host="db.internal"
                ),
            ]
        )
    )
    assert tm.check_remote_ports(timeout=1) == {}
    assert calls == []


def test_check_local_ports_probes_local_listeners(monkeypatch) -> None:
    """本地探测是 loopback connect：覆盖 -L/-D，跳过 -R，通配绑定按 127.0.0.1 探。"""
    monkeypatch.setattr("ponte.core._find_ssh", lambda _cfg: "/usr/bin/ssh")
    seen: list[tuple[str, int]] = []

    class _Conn:
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def fake_connect(address: tuple[str, int], timeout: float | None = None) -> _Conn:
        seen.append(address)
        if address == ("127.0.0.1", 1080):
            raise OSError("connection refused")
        return _Conn()

    monkeypatch.setattr("ponte.core.socket.create_connection", fake_connect)
    tm = TunnelManager(
        _cfg(
            tunnels=[
                Tunnel(23334, "localhost", 2222),
                Tunnel(
                    5432, "0.0.0.0", 8080, kind="local", remote_host="db.internal"
                ),
                Tunnel(None, "127.0.0.1", 1080, kind="dynamic"),
            ]
        )
    )
    assert tm.check_local_ports() == {8080: True, 1080: False}
    assert seen == [("127.0.0.1", 8080), ("127.0.0.1", 1080)]


def test_stop_no_process() -> None:
    tm = TunnelManager(_cfg())
    tm.process = None
    tm.stop()  # 无进程时安全返回


def test_find_ssh_windows_fallback(monkeypatch) -> None:
    # Windows 未配置 ssh_exe、PATH 未命中 → 回退到 Git 路径
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("ponte.core.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "ponte.core.os.path.isfile",
        lambda p: p == r"C:\Program Files\Git\usr\bin\ssh.exe",
    )
    cfg = _cfg(ssh_exe=None)
    assert _find_ssh(cfg) == r"C:\Program Files\Git\usr\bin\ssh.exe"


def test_find_ssh_windows_config_missing_falls_back(monkeypatch) -> None:
    # Windows 配置了 ssh_exe 但文件不存在 → 回退 PATH
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("ponte.core.shutil.which", lambda _name: r"C:\Windows\ssh.exe")
    monkeypatch.setattr("ponte.core.os.path.isfile", lambda _p: False)
    cfg = _cfg(ssh_exe=r"D:\missing\ssh.exe")
    assert _find_ssh(cfg) == r"C:\Windows\ssh.exe"


def test_find_ssh_no_config_loads_global(monkeypatch, tmp_path) -> None:
    # 不传 config 时内部会调用 get_config，这里只验证会落到 PATH
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("ponte.core.shutil.which", lambda _name: "/usr/bin/ssh")
    # 避免 get_config 读取真实配置文件失败：直接 monkeypatch 掉
    monkeypatch.setattr("ponte.core.get_config", lambda: _cfg())
    assert _find_ssh() == "/usr/bin/ssh"


def test_connect_returns_exit_code(monkeypatch) -> None:
    class _Proc:
        def __init__(self) -> None:
            self.returncode = 42
            self.stderr = iter(())
        def wait(self):
            return self.returncode
        def poll(self):
            return self.returncode

    monkeypatch.setattr("ponte.core.subprocess.Popen", lambda *a, **k: _Proc())
    tm = TunnelManager(_cfg())
    assert tm.connect() == 42


def test_connect_logs_stderr(monkeypatch, caplog) -> None:
    class _Proc:
        def __init__(self) -> None:
            self.returncode = 1
            self.stderr = iter((b"auth failed\n",))
        def wait(self):
            return self.returncode
        def poll(self):
            return self.returncode

    monkeypatch.setattr("ponte.core.subprocess.Popen", lambda *a, **k: _Proc())
    tm = TunnelManager(_cfg())
    with caplog.at_level("WARNING", logger="ponte.core"):
        tm.connect()
        # 这里曾经是 ``time.sleep(0.05)`` "给 drain 线程一点时间"——那是在断言这台机器
        # 够快（慢 runner 上 drain 线程就是排不到，测试随机红）。能等的只有"日志记录
        # 出现"这个事实本身，所以等它，等不到 5 秒才算失败。
        wait_for(
            lambda: any("auth failed" in r.getMessage() for r in caplog.records),
            lambda: f"drain 线程没有记录 stderr："
            f"{[r.getMessage() for r in caplog.records]!r}",
        )
    assert "SSH stderr: auth failed" in caplog.text


def test_drain_stderr_reads_the_process_it_was_handed(caplog) -> None:
    """drain 线程要读的进程必须随线程一起传进去，而不是事后读 ``self.process``。

    ``connect()`` 的 ``finally`` 会把 ``self.process`` 清成 ``None``。线程被调度得
    晚一点（慢 runner 上很常见）就会读到 ``None``，于是整条 stderr 没人读——而
    ``stderr=PIPE`` 的缓冲区填满、把 SSH 别住，正是这条线程存在的理由。

    这里直接钉住那个契约：即使 ``self.process`` 已经是 ``None``，交给这条线程的
    进程照样得被读完。上面那条测试只能靠**调度很晚**才能发现这个竞态（所以它需要
    时序注入），这条不需要。
    """

    class _Proc:
        def __init__(self) -> None:
            self.stderr = iter((b"Connection to host closed by remote host\n",))

    tm = TunnelManager(_cfg())
    tm.process = None  # 就是 connect() 返回之后的状态
    with caplog.at_level("WARNING", logger="ponte.core"):
        tm._drain_stderr(_Proc())
    assert "SSH stderr: Connection to host closed by remote host" in caplog.text


def test_connect_records_last_session_duration(monkeypatch) -> None:
    class _Proc:
        def __init__(self) -> None:
            self.returncode = 0
            self.stderr = iter(())
        def wait(self):
            return self.returncode
        def poll(self):
            return self.returncode

    monkeypatch.setattr("ponte.core.subprocess.Popen", lambda *a, **k: _Proc())
    tm = TunnelManager(_cfg())
    assert tm.last_session_duration is None
    tm.connect()
    assert tm.last_session_duration is not None
    assert tm.last_session_duration >= 0.0


def test_uptime_idle_is_zero() -> None:
    tm = TunnelManager(_cfg())
    assert tm.uptime == 0.0


def test_is_running_reflects_process_state(monkeypatch) -> None:
    tm = TunnelManager(_cfg())
    assert tm.is_running() is False

    class _Alive:
        def poll(self):
            return None

    tm.process = _Alive()
    assert tm.is_running() is True

    class _Dead:
        def poll(self):
            return 0

    tm.process = _Dead()
    assert tm.is_running() is False


def test_creation_flags_windows(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert creation_flags() == _CREATE_NO_WINDOW_VALUE


def test_creation_flags_posix(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert creation_flags() == 0


def test_connect_passes_creationflags(monkeypatch) -> None:
    captured: dict = {}

    class _Proc:
        returncode = 0
        stderr = iter(())

        def wait(self):
            return self.returncode

        def poll(self):
            return self.returncode

    def _popen(*args, **kwargs):
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("ponte.core.subprocess.Popen", _popen)
    TunnelManager(_cfg()).connect()
    assert captured["creationflags"] == _CREATE_NO_WINDOW_VALUE
    # 长命 ssh 绝不能继承兄弟进程的管道：Windows 没有 close-on-exec，
    # 被继承的写端会让持有读端的一方永远等不到 EOF。
    assert captured["close_fds"] is True


def test_test_connection_passes_creationflags(monkeypatch) -> None:
    captured: dict = {}
    _fake_popen(monkeypatch, stdout="OK", returncode=0, captured=captured)

    monkeypatch.setattr(sys, "platform", "win32")
    assert TunnelManager(_cfg()).test_connection() is True
    assert captured["creationflags"] == _CREATE_NO_WINDOW_VALUE


# ---------------------------------------------------------------------------
# [ssh] jump —— -J 必须出现在每一条通往服务器的命令里
# ---------------------------------------------------------------------------

_JUMP_HOPS = (JumpHop(host="bastion.example.com", user="ops"),)


def _jump_cfg() -> TunnelConfig:
    """单一 profile，服务器只能从跳板机那一侧访问。"""
    cfg = _cfg()
    profile = dataclasses.replace(
        cfg.profiles[0],
        ssh=dataclasses.replace(cfg.profiles[0].ssh, jumps=_JUMP_HOPS),
    )
    return dataclasses.replace(cfg, profiles=[profile])


def test_build_args_passes_the_jump_host_to_ssh(monkeypatch) -> None:
    monkeypatch.setattr("ponte.core._find_ssh", lambda _cfg: "/usr/bin/ssh")
    args = TunnelManager(_jump_cfg()).build_args()
    assert _flag_pairs(args, "-J") == [("-J", "ops@bastion.example.com")]
    # 隧道仍然指向真正的服务器，跳板机只用来过路
    assert args[-1] == "testuser@example.com"


def test_the_login_test_goes_through_the_jump_host(monkeypatch) -> None:
    """健康检查/ponte test 必须走同一条链路，否则隧道通了却报“登录失败”。"""
    captured: dict = {}

    def _run(args, **_kwargs):
        captured["args"] = args
        return 0, "OK"

    monkeypatch.setattr("ponte.core._find_ssh", lambda _cfg: "/usr/bin/ssh")
    monkeypatch.setattr("ponte.core._run_capture", _run)
    assert TunnelManager(_jump_cfg()).test_connection(timeout=7) is True
    assert _flag_pairs(captured["args"], "-J") == [("-J", "ops@bastion.example.com")]
    assert captured["args"][-2:] == ["testuser@example.com", "echo OK"]
    assert ("-o", "ConnectTimeout=7") in _flag_pairs(captured["args"], "-o")


def test_the_remote_port_probe_goes_through_the_jump_host(monkeypatch) -> None:
    """服务端端口探测也要过跳板机，否则它连不上服务器、把健康的隧道报成异常。"""
    captured: dict = {}

    def _run(args, **_kwargs):
        captured["args"] = args
        return 0, "23334"

    monkeypatch.setattr("ponte.core._find_ssh", lambda _cfg: "/usr/bin/ssh")
    monkeypatch.setattr("ponte.core._run_capture", _run)
    assert TunnelManager(_jump_cfg()).check_remote_ports(timeout=7)[23334] is True
    assert _flag_pairs(captured["args"], "-J") == [("-J", "ops@bastion.example.com")]


def test_build_args_without_key_or_user_defers_to_ssh(monkeypatch) -> None:
    """没有 identity_file / user 时不传 -i、也不拼 user@，交给 ssh 自己解析。"""
    monkeypatch.setattr("ponte.core._find_ssh", lambda _cfg: "/usr/bin/ssh")
    cfg = _cfg()
    profile = dataclasses.replace(
        cfg.profiles[0],
        ssh=dataclasses.replace(cfg.profiles[0].ssh, identity_file=None, user=""),
    )
    tm = TunnelManager(dataclasses.replace(cfg, profiles=[profile]))
    args = tm.build_args()
    assert "-i" not in args
    assert args[-1] == "example.com"
    assert "-p" not in args
