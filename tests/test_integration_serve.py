"""端到端：真实的守护进程状态文件经过真实的 HTTP 服务（回环套接字）。

其余测试都是直接喂手工拼的 payload，所以“守护进程写出来的字段名”与“看板读的
字段名”一旦不一致，两边各自的单元测试都照样通过。这里跑完整链路：

    HealthChecker → TunnelDaemon._on_health 落盘 → main._status_payload
    → serve.create_server 真套接字 → 看板 /healthz /status.json /metrics

因此它守的是跨进程的*契约*，而不是任何一个函数的实现细节。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

from ponte.config import load_config
from ponte.core import ProbeError
from ponte.daemon import TunnelDaemon
from ponte.health import HealthChecker
from ponte.main import _status_payload
from ponte.serve import create_server

#: 探针自己没连上时，守护进程实际记录下来的那种原因串。
PROBE_ERROR = "探测连接失败（ssh 退出码 255）：23334 的状态未知"


class _FakeManager:
    """``TunnelManager`` 的最小替身：健康检查只用到这两个入口。"""

    process = None

    def __init__(self, probe: object) -> None:
        self._probe = probe

    def check_remote_ports(self, **_kwargs: object) -> object:
        if isinstance(self._probe, Exception):
            raise self._probe
        return self._probe

    def check_local_ports(self, **_kwargs: object) -> dict[int, bool]:
        return {}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config_path(tmp_path: Path, port: int) -> Path:
    key = tmp_path / "id_rsa"
    key.write_text("x", encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(
        "[ssh]\n"
        'host = "example.com"\n'
        'user = "u"\n'
        f'identity_file = "{key.as_posix()}"\n'
        "\n[[tunnels]]\n"
        "remote_port = 23334\n"
        'local_host = "localhost"\n'
        "local_port = 2222\n"
        "\n[daemon]\n"
        f'pid_file = "{(tmp_path / "ponte.pid").as_posix()}"\n'
        "\n[serve]\n"
        'host = "127.0.0.1"\n'
        f"port = {port}\n",
        encoding="utf-8",
    )
    return path


def _daemon_with_snapshot(tmp_path: Path, probe: object) -> TunnelDaemon:
    """跑一次真实的健康检查，并经守护进程自己的写入路径落盘。"""
    cfg = load_config(str(_config_path(tmp_path, _free_port())))
    daemon = TunnelDaemon(config=cfg)
    # 用测试进程自己的 pid：这样 status() 认为守护进程在跑（不调用 stop()）。
    (tmp_path / "ponte.pid").write_text(str(os.getpid()), encoding="utf-8")
    status = HealthChecker(_FakeManager(probe), cfg.health).check()
    daemon._on_health(status, manager=None, profile="default")
    return daemon


def _start(daemon: TunnelDaemon, token: str = "s3cret"):
    server = create_server(
        lambda: _status_payload(daemon.status()),
        host="127.0.0.1",
        port=_free_port(),
        token=token,
        refresh=5,
    )
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
    )
    thread.start()
    return server, int(server.server_address[1]), token


def _get(port: int, path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # 503 等也要能读到响应体
        return exc.code, exc.read().decode("utf-8")


def _metrics(port: int, token: str) -> list[str]:
    _, body = _get(port, f"/metrics?token={token}")
    return body.splitlines()


def test_unanswered_probe_stays_unknown_all_the_way_to_the_dashboard(tmp_path) -> None:
    """探针没连上 → 一路都是“未知”，绝不是“端口没在听”。"""
    daemon = _daemon_with_snapshot(tmp_path, ProbeError(PROBE_ERROR))
    server, port, token = _start(daemon)
    try:
        code, body = _get(port, f"/healthz?token={token}")
        assert code == 200, "未知不是故障：不该用 503 报警"
        payload = json.loads(body)
        assert payload["status"] == "unverified"
        assert payload["unknown"] == ["default"]
        assert "unhealthy" not in payload

        code, body = _get(port, f"/status.json?token={token}")
        assert code == 200
        section = json.loads(body)["profiles"]["default"]
        assert section["health_conclusive"] is False
        assert "255" in section["probe_error"]
        assert section["remote_ports"] == {}, "没观察到的端口不得写成“未监听”"

        lines = _metrics(port, token)
        assert "ponte_profiles_unknown 1" in lines
        assert not [ln for ln in lines if ln.startswith("ponte_profile_healthy")], (
            "未知时 ponte_profile_healthy 应当是“没有样本”，而不是 0"
        )

        _, page = _get(port, f"/?token={token}")
        assert "未知" in page
        assert "异常" not in page
        assert 'class="card unknown"' in page
    finally:
        server.shutdown()
        server.server_close()


def test_a_conclusive_pass_is_reported_as_healthy(tmp_path) -> None:
    """探针确实跑通了并看到端口在听 → 健康，且真的采到了样本。"""
    daemon = _daemon_with_snapshot(tmp_path, {23334: True})
    server, port, token = _start(daemon)
    try:
        code, body = _get(port, f"/healthz?token={token}")
        assert code == 200
        assert json.loads(body)["status"] == "ok"

        lines = _metrics(port, token)
        assert 'ponte_profile_healthy{profile="default"} 1' in lines
        assert "ponte_profiles_unknown 0" in lines

        _, page = _get(port, f"/?token={token}")
        assert "健康" in page
    finally:
        server.shutdown()
        server.server_close()


def test_a_conclusive_failure_is_degraded_and_names_the_port(tmp_path) -> None:
    """探针连上了、答案是端口没在听 → 这才是判定：503 并点名端口。"""
    daemon = _daemon_with_snapshot(tmp_path, {23334: False})
    server, port, token = _start(daemon)
    try:
        code, body = _get(port, f"/healthz?token={token}")
        assert code == 503
        payload = json.loads(body)
        assert payload["status"] == "degraded"
        assert payload["unhealthy"] == ["default"]
        assert "23334" in payload["errors"]["default"]

        lines = _metrics(port, token)
        assert "ponte_profiles_unhealthy 1" in lines
        assert "ponte_profiles_unknown 0" in lines
    finally:
        server.shutdown()
        server.server_close()
