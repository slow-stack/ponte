"""pytest tests for :mod:`ponte.doctor` (no SSH, no HTTP, no real daemon)."""

from __future__ import annotations

import dataclasses
import sys

from ponte.config import load_config
from ponte.core import ProbeError
from ponte.daemon import DaemonStatus, ProfileStatus
from ponte.doctor import FAIL, OK, SKIP, WARN, counts, run_checks


def _config(tmp_path, extra: str = "", tunnel: str | None = None, jump: str | None = None):
    """A valid single-profile config; *extra* is appended as-is."""
    key = tmp_path / "id_rsa"
    key.write_text("x", encoding="utf-8")
    jump_line = f'jump = "{jump}"\n' if jump else ""
    rules = tunnel if tunnel is not None else """
[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
"""
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[ssh]
host = "example.com"
user = "u"
identity_file = "{key.as_posix()}"
{jump_line}
{rules}
[daemon]
pid_file = "{(tmp_path / 'ponte.pid').as_posix()}"
log_file = "{(tmp_path / 'ponte.log').as_posix()}"
{extra}
""",
        encoding="utf-8",
    )
    return load_config(str(path))


class _FakeDaemon:
    """Stand-in for :class:`~ponte.daemon.TunnelDaemon` (doctor's whole surface)."""

    def __init__(
        self,
        *,
        running: bool = False,
        healthy: bool | None = True,
        reachable: bool = True,
        remote: dict[int, bool] | None = None,
        local: dict[int, bool] | None = None,
        service: bool | None = False,
        notifier: object | None = None,
        conclusive: bool | None = True,
        probe_error: str | None = None,
    ) -> None:
        self._running = running
        self._healthy = healthy
        self._conclusive = conclusive
        self._probe_error = probe_error
        self._reachable = reachable
        self._remote = remote if remote is not None else {23334: True}
        self._local = local if local is not None else {}
        self._service = service
        self.notifier = notifier
        self.calls: list[tuple[str, str | None]] = []

    def status(self) -> DaemonStatus:
        if not self._running:
            return DaemonStatus(running=False, message="daemon is not running")
        return DaemonStatus(
            running=True,
            pid=4242,
            uptime_seconds=3600,
            profiles=[
                ProfileStatus(
                    name="default",
                    healthy=self._healthy,
                    health_conclusive=self._conclusive,
                    remote_ports=self._remote,
                )
            ],
        )

    def test_connection(self, timeout: int = 10, profile: str | None = None) -> bool:
        self.calls.append(("test", profile))
        return self._reachable

    def check_remote_ports(
        self, timeout: int = 10, profile: str | None = None
    ) -> dict[int, bool]:
        self.calls.append(("remote", profile))
        if self._probe_error is not None:
            raise ProbeError(self._probe_error)
        return self._remote

    def check_local_ports(
        self, timeout: float = 1.0, profile: str | None = None
    ) -> dict[int, bool]:
        self.calls.append(("local", profile))
        return self._local

    def service_installed(self) -> bool | None:
        return self._service


def _find(results, needle: str):  # noqa: ANN001, ANN202 - tiny test helper
    matches = [result for result in results if needle in result.name]
    assert matches, f"no check named like {needle!r} in {[r.name for r in results]}"
    return matches[0]


def _make_checks(config, tmp_path, **kwargs):  # noqa: ANN001, ANN202
    """Config whose log directory exists, so unrelated rows stay quiet."""
    return run_checks(config, _FakeDaemon(**kwargs), offline=False, timeout=2)


# ---------------------------------------------------------------------------
# Configuration & profile rows
# ---------------------------------------------------------------------------


def test_config_row_reports_path_and_profiles(tmp_path) -> None:
    config = _config(tmp_path)
    result = _find(run_checks(config, None), "配置文件")
    assert result.status == OK
    assert "default" in result.detail


def test_config_row_surfaces_warnings(tmp_path) -> None:
    config = _config(tmp_path, extra='\n[notify]\nenabled = true\n')
    result = _find(run_checks(config, None), "配置文件")
    assert result.status == WARN
    assert "不会发出任何通知" in result.hint


def test_client_key_and_connectivity_pass(tmp_path) -> None:
    config = _config(tmp_path)
    daemon = _FakeDaemon(reachable=True)
    results = run_checks(config, daemon, timeout=3)

    assert _find(results, "SSH 客户端").status in (OK, WARN)  # WARN 仅当 PATH 无 ssh
    assert _find(results, "密钥文件").status == OK
    connectivity = _find(results, "SSH 连通性")
    assert connectivity.status == OK
    assert daemon.calls[0] == ("test", "default")


def test_connectivity_failure_comes_with_a_fix(tmp_path) -> None:
    results = run_checks(_config(tmp_path), _FakeDaemon(reachable=False))
    connectivity = _find(results, "SSH 连通性")
    assert connectivity.status == FAIL
    assert "authorized_keys" in connectivity.hint


def test_missing_identity_file_is_a_failure(tmp_path) -> None:
    config = _config(tmp_path)
    broken = dataclasses.replace(
        config,
        profiles=[
            dataclasses.replace(config.profiles[0], ssh=dataclasses.replace(
                config.profiles[0].ssh, identity_file=str(tmp_path / "gone")
            ))
        ],
    )
    result = _find(run_checks(broken, None), "密钥文件")
    assert result.status == FAIL
    assert "ssh-keygen" in result.hint


def test_key_permission_row_exists_on_posix(tmp_path) -> None:
    results = run_checks(_config(tmp_path), None)
    names = [result.name for result in results]
    if sys.platform == "win32":
        assert "密钥权限" not in names, "Windows 没有 POSIX 权限位"
    else:
        assert "密钥权限" in names


# ---------------------------------------------------------------------------
# 跳板机 / ProxyJump
# ---------------------------------------------------------------------------


def test_without_a_jump_there_is_no_hop_row(tmp_path) -> None:
    names = [result.name for result in run_checks(_config(tmp_path), None)]
    assert "跳板机" not in names


def test_jump_row_passes_when_the_bastion_answers(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("ponte.doctor.port_is_open", lambda *_args: True)
    result = _find(run_checks(_config(tmp_path, jump="ops@bastion:2222"), None), "跳板机")
    assert result.status == OK
    assert "ops@bastion" in result.detail
    assert "bastion:2222" in result.detail


def test_jump_probes_the_first_hop_only(tmp_path, monkeypatch) -> None:
    """只探测第一跳：后面几跳只能经前一跳到达，本机直接连它们会误报失败。"""
    seen: list[tuple] = []

    def _probe(host, port, timeout):  # noqa: ANN001, ANN202 - 记录探测目标即可
        seen.append((host, port, timeout))
        return True

    monkeypatch.setattr("ponte.doctor.port_is_open", _probe)
    config = _config(tmp_path, jump="ops@hop1:2222, root@hop2")
    result = _find(run_checks(config, None, timeout=3), "跳板机")
    assert seen == [("hop1", 2222, 3)]
    assert result.status == OK
    assert "后续跳" in result.detail


def test_unreachable_bastion_is_a_failure_with_the_command_to_try(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("ponte.doctor.port_is_open", lambda *_args: False)
    result = _find(run_checks(_config(tmp_path, jump="ops@bastion"), None), "跳板机")
    assert result.status == FAIL
    assert "bastion:22" in result.detail
    assert "ssh ops@bastion" in result.hint


def test_jump_row_is_skipped_offline(tmp_path, monkeypatch) -> None:
    def _probe(*_args):  # noqa: ANN202 - 离线时不该被调用
        raise AssertionError("offline 不该发起探测")

    monkeypatch.setattr("ponte.doctor.port_is_open", _probe)
    config = _config(tmp_path, jump="ops@bastion")
    result = _find(run_checks(config, None, offline=True), "跳板机")
    assert result.status == SKIP


def test_connectivity_hint_names_the_bastion(tmp_path, monkeypatch) -> None:
    """登录失败时提示的是跳板机，而不是再次叫人去确认服务器可达。"""
    monkeypatch.setattr("ponte.doctor.port_is_open", lambda *_args: True)
    config = _config(tmp_path, jump="ops@bastion")
    connectivity = _find(run_checks(config, _FakeDaemon(reachable=False)), "SSH 连通性")
    assert connectivity.status == FAIL
    assert "ops@bastion" in connectivity.hint
    assert "authorized_keys" not in connectivity.hint, "有跳板机时别再说去查服务器"


def test_rows_are_prefixed_per_profile_when_there_are_several(tmp_path) -> None:
    """多隧道时每行都要带 profile 名，否则不知道是哪条连接出的问题。"""
    key = (tmp_path / "id_rsa").as_posix()
    entries = "".join(
        f"""
[[profiles]]
name = "{name}"

[profiles.ssh]
host = "{name}.example.com"
user = "u"
identity_file = "{key}"

[[profiles.tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
"""
        for name in ("web", "db")
    )
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    path = tmp_path / "multi.toml"
    path.write_text(
        entries
        + f'\n[daemon]\npid_file = "{(tmp_path / "ponte.pid").as_posix()}"'
        + f'\nlog_file = "{(tmp_path / "ponte.log").as_posix()}"\n',
        encoding="utf-8",
    )
    config = load_config(str(path))
    assert config.profile_names == ["web", "db"]

    names = [result.name for result in run_checks(config, None)]
    assert "web · SSH 客户端" in names
    assert "db · SSH 连通性" in names
    assert "配置文件" in names, "全局行不加前缀"


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


def test_remote_port_down_is_a_failure(tmp_path) -> None:
    daemon = _FakeDaemon(running=True, remote={23334: False})
    result = _find(run_checks(_config(tmp_path), daemon), "远程端口")
    assert result.status == FAIL
    assert "23334" in result.detail
    assert "ss -tlnp" in result.hint


def test_remote_port_probe_failure_is_not_a_failure(tmp_path) -> None:
    """探测连接没建起来 = 无法判定：WARN 并说清原因，不能报成“未监听”。"""
    daemon = _FakeDaemon(
        running=True, probe_error="探测连接失败（ssh 退出码 255）：23334 的状态未知"
    )
    result = _find(run_checks(_config(tmp_path), daemon), "远程端口")
    assert result.status == WARN
    assert "255" in result.detail and "未知" in result.detail
    assert not result.hint, "未知不是故障：不该给出查端口/看日志的修复提示"


def test_local_port_down_is_a_warning(tmp_path) -> None:
    config = _config(
        tmp_path,
        tunnel="""
[[tunnels]]
kind = "dynamic"
local_port = 1080
""",
    )
    daemon = _FakeDaemon(running=True, local={1080: False})
    result = _find(run_checks(config, daemon), "本地端口")
    assert result.status == WARN
    assert "1080" in result.detail


def test_port_checks_need_a_running_daemon(tmp_path) -> None:
    """守护进程没跑时端口状态没有参考价值：跳过而不是报失败。"""
    config = _config(tmp_path)
    daemon = _FakeDaemon(running=False, remote={23334: False})
    results = run_checks(config, daemon)
    assert _find(results, "远程端口").status == SKIP
    assert daemon.calls == [("test", "default")], "不该白跑端口探测"


def test_port_rows_absent_for_kinds_that_have_none(tmp_path) -> None:
    config = _config(
        tmp_path,
        tunnel="""
[[tunnels]]
kind = "dynamic"
local_port = 1080
""",
    )
    results = run_checks(config, _FakeDaemon(running=True, local={1080: True}))
    assert "远程端口" not in " ".join(result.name for result in results)


# ---------------------------------------------------------------------------
# Offline / missing daemon
# ---------------------------------------------------------------------------


def test_offline_skips_network_checks(tmp_path) -> None:
    daemon = _FakeDaemon(running=True)
    results = run_checks(_config(tmp_path), daemon, offline=True)
    assert _find(results, "SSH 连通性").status == SKIP
    assert _find(results, "远程端口").status == SKIP
    assert daemon.calls == [], "--offline 不该发起任何探测"


def test_without_daemon_everything_degrades_to_skip(tmp_path) -> None:
    results = run_checks(_config(tmp_path), None)
    assert _find(results, "SSH 连通性").status == SKIP
    assert _find(results, "守护进程").status == SKIP
    assert _find(results, "开机自启").status == SKIP


# ---------------------------------------------------------------------------
# Daemon / service / log / notify rows
# ---------------------------------------------------------------------------


def test_daemon_row_when_not_running(tmp_path) -> None:
    result = _find(run_checks(_config(tmp_path), _FakeDaemon(running=False)), "守护进程")
    assert result.status == WARN
    assert "ponte start" in result.hint


def test_daemon_row_when_all_healthy(tmp_path) -> None:
    result = _find(run_checks(_config(tmp_path), _FakeDaemon(running=True)), "守护进程")
    assert result.status == OK
    assert "4242" in result.detail


def test_daemon_row_when_a_tunnel_is_broken(tmp_path) -> None:
    result = _find(
        run_checks(_config(tmp_path), _FakeDaemon(running=True, healthy=False)),
        "守护进程",
    )
    assert result.status == FAIL
    assert "default" in result.detail
    assert "ponte watch" in result.hint


def test_daemon_row_when_the_check_could_not_be_completed(tmp_path) -> None:
    """healthy=False 但 health_conclusive=False → 无法判定，不是“异常”。"""
    result = _find(
        run_checks(
            _config(tmp_path),
            _FakeDaemon(running=True, healthy=False, conclusive=False),
        ),
        "守护进程",
    )
    assert result.status == WARN
    assert "无法判定" in result.detail
    assert "default" in result.detail


def test_daemon_row_when_health_is_unknown(tmp_path) -> None:
    result = _find(
        run_checks(_config(tmp_path), _FakeDaemon(running=True, healthy=None)),
        "守护进程",
    )
    assert result.status == WARN
    assert "尚未上报" in result.detail


def test_service_rows(tmp_path) -> None:
    config = _config(tmp_path)
    assert _find(run_checks(config, _FakeDaemon(service=True)), "开机自启").status == OK
    missing = _find(run_checks(config, _FakeDaemon(service=False)), "开机自启")
    assert missing.status == WARN
    assert "ponte install" in missing.hint
    assert _find(run_checks(config, _FakeDaemon(service=None)), "开机自启").status == SKIP


def test_log_rows(tmp_path) -> None:
    config = _config(tmp_path)
    log_path = tmp_path / "ponte.log"

    assert _find(run_checks(config, None), "日志文件").status == WARN, "还没产生日志"

    log_path.write_text("hello", encoding="utf-8")
    assert _find(run_checks(config, None), "日志文件").status == OK

    gone = dataclasses.replace(
        config,
        daemon=dataclasses.replace(config.daemon, log_file=str(tmp_path / "no" / "x.log")),
    )
    assert _find(run_checks(gone, None), "日志文件").status == FAIL


def test_notify_rows(tmp_path) -> None:
    disabled = run_checks(_config(tmp_path), None)
    assert _find(disabled, "断线通知").status == SKIP

    enabled = _config(
        tmp_path, extra='\n[notify]\nenabled = true\nntfy_topic = "t"\n'
    )
    row = _find(run_checks(enabled, None), "断线通知")
    assert row.status == OK
    assert "ntfy" in row.detail

    no_channel = _config(tmp_path, extra="\n[notify]\nenabled = true\n")
    assert _find(run_checks(no_channel, None), "断线通知").status == FAIL


def test_notify_row_surfaces_the_last_delivery_error(tmp_path) -> None:
    class _Notifier:
        last_error = "ntfy: URLError: name or service not known"

    config = _config(
        tmp_path, extra='\n[notify]\nenabled = true\nntfy_topic = "t"\n'
    )
    row = _find(run_checks(config, _FakeDaemon(notifier=_Notifier())), "断线通知")
    assert row.status == WARN
    assert "URLError" in row.hint


def test_counts_tallies_every_status(tmp_path) -> None:
    tally = counts(run_checks(_config(tmp_path), None))
    assert set(tally) == {OK, WARN, FAIL, SKIP}
    assert sum(tally.values()) == len(run_checks(_config(tmp_path), None))
    assert tally[FAIL] == 0
