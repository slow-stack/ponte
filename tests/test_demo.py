"""``ponte serve --demo`` 的测试：内置示例时间轴、payload 契约与命令接线。

演示模式的价值全在"它说的是真的"上：一份随时间推进、覆盖看板会渲染的每种状态的数据，
而且**确定**——同一时刻永远是同一个 payload。所以这些测试钉的是**时刻**，不是运气：
时间通过 ``now`` 注入，测试里没有一处 ``sleep``。
"""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from ponte import demo
from ponte.daemon import DaemonStatus, ProfileStatus
from ponte.main import _status_payload, app
from ponte.serve import dashboard_html, health_response, render_metrics

_START = 1_000_000.0


def _payload(seconds: float) -> dict:
    """演示的某一刻（``started_at`` 固定，避免依赖真实时间）。"""
    return demo.DemoStatus(started_at=_START).payload(now=_START + seconds)


def _section(seconds: float, name: str) -> dict:
    return _payload(seconds)["profiles"][name]


# ---------------------------------------------------------------------------
# 时间轴：三种状态都真的会出现，而且会自己走
# ---------------------------------------------------------------------------


def test_timeline_walks_a_tunnel_through_disconnect_and_reconnect() -> None:
    """``web`` 这条演示隧道：健康 → 断线 → 重连退避 → 又连上，会话数跟着加一。

    这是看板存在的理由那件事：状态**会变**，所以页面上的数字、事件流与"上次断线"都该动。
    """
    connected = _section(10.0, "web")
    assert connected["healthy"] is True
    assert connected["process_alive"] is True
    assert connected["remote_ports"] == {"23334": True}
    assert connected["current_session_at"] == _START

    disconnected = _section(145.0, "web")
    assert disconnected["healthy"] is False
    assert disconnected["health_conclusive"] is True
    assert disconnected["process_alive"] is False
    assert disconnected["remote_ports"] == {"23334": False}
    assert disconnected["current_session_at"] is None
    assert "code 255" in disconnected["last_disconnect_reason"]

    retrying = _section(152.0, "web")
    assert retrying["healthy"] is False
    assert retrying["connect_attempts_total"] > disconnected["connect_attempts_total"]

    recovered = _section(170.0, "web")
    assert recovered["healthy"] is True
    assert recovered["sessions_total"] == disconnected["sessions_total"] + 1
    assert recovered["reconnects_total"] == recovered["sessions_total"] - 1
    assert recovered["availability"] < 1.0, "掉过线就不该是 100% 可用"


def test_probe_without_a_verdict_is_not_reported_as_a_dead_port() -> None:
    """``metrics`` 那条演示：探针没问出结论时，端口是**未观测**，不是"未监听"（#25）。

    这条盯的是演示数据会不会把"我们没法问"渲染成"问了，答案是坏"——那正是 #25 修掉的
    误导，一份用来展示前端的示例数据同样不该犯。
    """
    unknown = _section(175.0, "metrics")
    assert unknown["healthy"] is False
    assert unknown["health_conclusive"] is False
    assert unknown["probe_error"]
    assert unknown["remote_ports"] == {}
    assert unknown["local_ports"] == {}

    # db 在"SSH 连上了、服务器那一侧端口没起来"那段里是**确定失败**（取它本轮的前 25 秒，
    # 其余时间是掉线 + 退避）：两种 false 必须能被看板区分开。
    broken = _section(10.0, "db")
    assert broken["healthy"] is False
    assert broken["health_conclusive"] is True
    assert broken["health_error"]
    assert broken["remote_ports"] == {"5432": False}
    assert broken["local_ports"] == {"5432": True}, "本机在听、服务器没听：两列本就不同"


def test_nothing_claims_to_have_happened_before_the_process_started() -> None:
    """演示数据不能声称在守护进程**启动之前**断过线，也不能报出未来的时刻。

    页头写着“已运行 1 分 20 秒”，而 web 那行写着“上次断线 2 分钟前”——一条比进程本身还老的
    故障记录。这条测试沿时间轴扫一遍，把“事件／断线时刻必须落在 [启动时刻, 现在] 之间”钉死。
    """
    # 启动后第一个周期里还没断过线时，“上次断线”是空的，不是回头取上一轮。
    early = _section(80.0, "web")
    assert early["healthy"] is True
    assert early["last_disconnect_at"] is None
    assert early["last_disconnect_reason"] is None
    assert _section(10.0, "db")["last_disconnect_at"] is None

    # 真的断过之后就有值，而且就是那一刻（web 在 140s 断线）。
    assert _section(145.0, "web")["last_disconnect_at"] == pytest.approx(_START + 140.0)

    # 全时间轴扫一遍：没有任何一条记录落在启动之前或现在之后。
    for seconds in range(0, 1200, 3):
        now = _START + seconds
        for name in ("web", "db", "metrics"):
            section = _section(float(seconds), name)
            for key in ("last_disconnect_at", "current_session_at", "last_notification_at"):
                moment = section[key]
                assert moment is None or _START <= moment <= now, (seconds, name, key, moment)
            for event in section["recent_events"]:
                assert _START <= event["at"] <= now, (seconds, name, event)


def test_a_tunnel_that_never_disconnects_keeps_one_session() -> None:
    """从不掉线的 ``metrics`` 不该被凭空记出更多会话或更多的可用时间。"""
    # 第 0 秒还没有任何累计值，可用率诚实地是 None（看板显示”—“）而不是编一个 100%。
    assert _section(0.0, "metrics")["availability"] is None
    for seconds in (210.0, 1000.0, 5000.0):
        section = _section(seconds, "metrics")
        assert section["sessions_total"] == 1, seconds
        assert section["reconnects_total"] == 0, seconds
        assert section["current_session_at"] == _START, seconds
        assert section["availability"] == 1.0, seconds


def test_counters_never_go_backwards() -> None:
    """统计只增不减——看板把它们当累计值显示，倒退比数字难看严重得多。"""
    previous: dict[str, float] = {}
    for seconds in range(0, 1200, 7):
        section = _section(float(seconds), "web")
        for key in ("sessions_total", "reconnects_total", "connect_attempts_total",
                    "tunnel_uptime_seconds", "tunnel_downtime_seconds"):
            value = section[key]
            assert value >= previous.get(key, 0.0), f"{key} 在 {seconds}s 退回去了"
            previous[key] = value


def test_the_event_feed_grows_and_stays_ordered() -> None:
    """事件流按时间升序、带上真实时刻，而且会随断线重连多起来（看板的 feed 就靠它动）。"""
    early = _section(30.0, "web")["recent_events"]
    later = _section(400.0, "web")["recent_events"]
    assert len(later) > len(early), "过了几轮之后事件流该更长"
    assert len(later) <= demo._FEED_LIMIT

    stamps = [event["at"] for event in later]
    assert stamps == sorted(stamps), "事件必须按时间升序（看板按这个顺序倒着渲染）"
    assert all(stamp <= _START + 400.0 for stamp in stamps), "不该出现未来的事件"
    assert {"connected", "disconnected"} <= {event["type"] for event in later}


# ---------------------------------------------------------------------------
# payload 契约：演示数据必须长得和真数据一样，否则它就在教看板错误的样子
# ---------------------------------------------------------------------------


def test_payload_matches_the_status_json_contract_exactly() -> None:
    """与真实的 ``ponte status --json``（``_status_payload``）逐键对比。

    这是这份数据唯一真正危险的地方：它是**手写的**。契约以后长出新字段时，演示数据会悄悄
    落后，而它看起来完全一样——所以这里直接问产品自己那份构造器要键集合，而不是抄一份
    清单下来自己维护。
    """
    real = _status_payload(
        DaemonStatus(
            running=True,
            pid=4242,
            started_at=_START,
            uptime_seconds=3600.0,
            profiles=[ProfileStatus(name="web", destination="deploy@edge.example.com:22")],
        )
    )
    demo_payload = _payload(10.0)

    assert set(demo_payload) - set(real) == {"demo"}, "演示 payload 只该多一个 demo 标记"
    assert set(real) - set(demo_payload) == set()
    assert set(demo_payload["profiles"]["web"]) == set(real["profiles"]["web"])

    assert demo_payload["demo"] is True
    assert set(demo_payload["profiles"]) == {"web", "db", "metrics"}


def test_demo_data_is_labelled_in_the_payload_and_on_the_page() -> None:
    """演示数据必须在**三处**都自证：payload、页面头部、页面页脚。

    第一处给脚本和 ``/status.json``，后两处给截图——一张看板截图看起来就像某个人的内网
    拓扑图，所以它自己得说清楚这不是。
    """
    payload = _payload(152.0)
    assert payload["demo"] is True
    assert payload["pid"] == os.getpid(), "演示的\"守护进程\"就是提供页面的这个进程"
    assert payload["running"] is True
    assert payload["uptime_seconds"] == pytest.approx(152.0)

    html = dashboard_html(payload, refresh=5, now=_START + 152.0)
    assert "演示数据" in html
    assert "演示模式" in html

    without_flag = {key: value for key, value in payload.items() if key != "demo"}
    assert "演示数据" not in dashboard_html(without_flag, refresh=5, now=_START + 152.0)


def test_the_endpoints_consume_demo_data_at_every_moment() -> None:
    """整条时间轴上，三个端点都渲染得出来——包括刚启动的第 0 秒。

    首次加载恰好发生在 t≈0（那时可用率还没有累计值，是 ``None``），所以这里从头到尾走一遍，
    而不是只挑几个"数据好看"的时刻。
    """
    for seconds in (0.0, 1.0, 30.0, 145.0, 152.0, 170.0, 175.0, 210.0, 1000.0):
        payload = _payload(seconds)
        html = dashboard_html(payload, refresh=5, now=_START + seconds)
        assert html.startswith("<!DOCTYPE html>"), seconds
        code, body = health_response(payload)
        assert code in (200, 503), seconds
        assert body["profiles"] == 3, seconds
        assert "ponte_" in render_metrics(payload, now=_START + seconds), seconds


def test_same_moment_always_renders_the_same_payload() -> None:
    """确定性：同一时刻两次取到的 payload 完全相同（否则测试只能靠等，看板也只能靠猜）。"""
    provider = demo.DemoStatus(started_at=_START)
    assert provider.payload(now=_START + 152.0) == provider.payload(now=_START + 152.0)
    assert provider.payload(now=_START + 1.0) != provider.payload(now=_START + 152.0)


# ---------------------------------------------------------------------------
# 命令接线：--demo 不读配置、不碰守护进程，但绑定校验照旧
# ---------------------------------------------------------------------------


def test_demo_mode_needs_no_config_and_never_touches_the_daemon(monkeypatch) -> None:
    """``serve --demo`` 在没有配置文件、没有守护进程时也要能起来。

    "不碰守护进程"是这条测试的重点：一旦它去读配置，演示模式就又有了一道"先有隧道才行"
    的门槛，而这个模式存在的全部理由就是没有隧道也能看见页面。
    """

    def explode(*args, **kwargs):  # pragma: no cover - 被调用即测试失败
        raise AssertionError("演示模式不该碰守护进程或配置加载")

    monkeypatch.setattr("ponte.main._daemon", explode)
    monkeypatch.setattr("ponte.main.get_config", explode)

    captured: dict = {}

    class _Server:
        def serve_forever(self, poll_interval: float = 0.5) -> None:
            raise KeyboardInterrupt  # 让命令干净地退出，而不是在测试里真的服务

        def server_close(self) -> None:
            captured["closed"] = True

    def fake_create_server(provider, **kwargs):
        captured["provider"] = provider
        captured["options"] = kwargs
        return _Server()

    monkeypatch.setattr("ponte.main.create_server", fake_create_server)

    result = CliRunner().invoke(app, ["serve", "--demo", "--port", "8791"])

    assert result.exit_code == 0, result.output
    assert "演示模式" in result.output
    assert captured["closed"] is True
    assert captured["options"] == {
        "host": "127.0.0.1",
        "port": 8791,
        "token": "",
        "refresh": 5,
    }
    payload = captured["provider"]()
    assert payload["demo"] is True
    assert set(payload["profiles"]) == {"web", "db", "metrics"}


def test_demo_mode_still_refuses_to_expose_the_dashboard_without_a_token(monkeypatch) -> None:
    """演示数据看起来仍然像一张内网拓扑图，所以绑定规则不能因为是演示而放松。"""
    monkeypatch.setattr(
        "ponte.main.create_server",
        lambda *a, **k: pytest.fail("校验不该走到建服务器这一步"),
    )
    result = CliRunner().invoke(app, ["serve", "--demo", "--host", "0.0.0.0"])
    assert result.exit_code != 0
    assert "令牌" in result.output
