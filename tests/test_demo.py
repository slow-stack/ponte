"""``ponte serve --demo`` 的测试：内置示例时间轴、payload 契约与命令接线。

演示模式的价值全在"它说的是真的"上：一份随时间推进、覆盖看板会渲染的每种状态的数据，
而且**确定**——同一时刻永远是同一个 payload。所以这些测试钉的是**时刻**，不是运气：
时间通过 ``now`` 注入，测试里没有一处 ``sleep``。
"""

from __future__ import annotations

import os
import types

import pytest
from typer.testing import CliRunner

from ponte import demo
from ponte.config import ServeConfig
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

    # 演示时钟仍然挤在同一个键里（而不是新开一个键），所以这份比对没变松。
    assert set(demo_payload["demo"]) == {
        "at",
        "anchored",
        "next_at",
        "next_profile",
        "next_state",
    }
    assert set(demo_payload["profiles"]) == {"web", "db", "metrics"}


def test_demo_data_is_labelled_in_the_payload_and_on_the_page() -> None:
    """演示数据必须在**三处**都自证：payload、页面头部、页面页脚。

    第一处给脚本和 ``/status.json``，后两处给截图——一张看板截图看起来就像某个人的内网
    拓扑图，所以它自己得说清楚这不是。
    """
    payload = _payload(152.0)
    assert payload["demo"]["anchored"] is False, "没被 ?at= 钉住时要说实话"
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
# 演示时钟：?at= 锚点与“下一处变化”
# ---------------------------------------------------------------------------


def _pinned(seconds: float) -> dict:
    """``?at=`` 钉住的那一刻（页面据此要说“已固定”而不是“实时”）。"""
    return demo.DemoStatus(started_at=_START)({"at": [str(seconds)]})


def _visible(payload: dict) -> tuple:
    """**看板上看得见的东西**：判定 / 结论是否确定 / 进程在不在 / 两列端口。

    这里刻意不调 demo 自己的私有函数——那会把测试变成镜像（两处一起错还互不告白）。这是
    拿渲染给用户看的那一层重算一遍。
    """
    return tuple(
        (
            name,
            section["healthy"],
            section["health_conclusive"],
            section["process_alive"],
            tuple(sorted(section["remote_ports"].items())),
            tuple(sorted(section["local_ports"].items())),
        )
        for name, section in sorted(payload["profiles"].items())
    )


def test_the_clock_can_be_pinned_to_a_moment_by_the_query() -> None:
    """``?at=<秒>`` 把这一刻钉在时间轴上——是**读**，不是控制端点。

    锚点走查询串而不是服务端状态，这条测试把它钉住：同一个 provider 实例、同一个
    ``?at=145``，两次调用给出同一份“断线中”的 payload，而 provider 自己没有变过。
    """
    provider = demo.DemoStatus(started_at=_START)

    pinned = provider({"at": ["145"]})
    assert pinned["uptime_seconds"] == pytest.approx(145.0)
    assert pinned["demo"]["anchored"] is True
    assert pinned["profiles"]["web"]["healthy"] is False
    assert pinned["profiles"]["web"]["last_disconnect_reason"]
    # 同一个锚点再问一次还是那一份：没有可变状态可以跑掉。
    assert provider({"at": ["145"]}) == pinned

    # 没有锚点就是实时，而且 ``anchored`` 如实说是 False。
    live = provider({})
    assert live["demo"]["anchored"] is False
    assert live["uptime_seconds"] != pytest.approx(145.0)


def test_the_anchor_renders_exactly_what_that_moment_renders_without_one() -> None:
    """``?at=T`` 与 ``now=start+T`` 必须是同一份数据。

    否则按钮与地址栏会各说各话：页面上的时刻与它声称的时刻不是一回事，而“一个 URL 就能
    把看板固定在某一刻”也跟着失效。
    """
    provider = demo.DemoStatus(started_at=_START)
    for seconds in (0.0, 25.0, 140.0, 146.0, 152.0, 170.0, 175.0, 210.0, 500.0):
        pinned = provider({"at": [str(seconds)]})
        direct = provider.payload(now=_START + seconds)
        assert pinned["profiles"] == direct["profiles"], seconds
        assert pinned["uptime_seconds"] == pytest.approx(direct["uptime_seconds"])


@pytest.mark.parametrize("raw", ["", "abc", "-1", "nan", "inf", "-inf", "1e400", "0x10", "12s"])
def test_a_bad_anchor_falls_back_to_live_instead_of_failing(raw: str) -> None:
    """地址栏里的锚点可能是手改的、转发的、被别的工具弄坏的，所以坏值一律回退到实时。

    回退而不是报错：这是只读的演示接口，一个参数打错不该让页面白屏；而回退的方向是“显示
    实时数据”——看板仍然完整可用，只是没被钉住。
    """
    payload = demo.DemoStatus(started_at=_START)({"at": [raw]})
    assert payload["demo"]["anchored"] is False
    assert payload["demo"]["at"] == pytest.approx(payload["uptime_seconds"], abs=0.5)


def test_an_absurd_anchor_is_clamped() -> None:
    """``?at=999999999`` 被钳到一天，而不是让时间轴跑到几百万个循环之外。"""
    payload = demo.DemoStatus(started_at=_START)({"at": ["999999999"]})
    assert payload["demo"]["anchored"] is True
    assert payload["uptime_seconds"] == pytest.approx(demo._AT_LIMIT)


def test_the_next_change_is_the_earliest_one_that_actually_changes_something() -> None:
    """``next_at`` 必须是“看得见的东西变了”的最近一刻。

    两件事一起验，而且都是拿**渲染给看板的那一层**重算的：

    * 在 ``next_at`` 之前，任何时刻的看板都与现在一模一样——否则按钮会跳过一段真的变化；
    * 到 ``next_at`` 那一刻，看板确实不同了——否则按钮看上去像是没反应。

    ``web`` 的“断线 → 重连退避”就是必须被跳过的例子：两段的判定与两列端口完全一样。
    """
    provider = demo.DemoStatus(started_at=_START)
    for seconds in (0.0, 10.0, 30.0, 120.0, 145.0, 146.0, 152.0, 170.0, 175.0, 250.0, 400.0):
        before = _visible(provider({"at": [str(seconds)]}))
        next_at = provider({"at": [str(seconds)]})["demo"]["next_at"]
        assert next_at is not None and next_at > seconds, seconds
        probe = seconds
        while probe + 0.5 < next_at:
            probe += 0.5
            assert _visible(provider({"at": [str(probe)]})) == before, (seconds, probe)
        assert _visible(provider({"at": [str(next_at)]})) != before, seconds


def test_the_next_change_names_the_tunnel_that_changes() -> None:
    """按钮上写着“下一处变化”，就得知道是哪条隧道变了——否则用户不知道往哪儿看。"""
    payload = _pinned(10.0)
    assert payload["demo"]["next_profile"] == "db"
    assert payload["demo"]["next_state"] == demo.DISCONNECTED
    assert payload["demo"]["next_at"] == pytest.approx(25.0)


def test_the_page_offers_the_clock_only_for_demo_data() -> None:
    """控制条只在演示模式出现，而且“下一处变化”是服务端算的（它才知道时间轴）。

    同时钉住一件事：**读数是纯文本**，所以关掉脚本的页面仍然说得出自己停在哪一刻；而按钮
    与它背后的脚本一起出现（一个按下去没反应的控件比没有控件更糟）。
    """
    pinned = dashboard_html(_pinned(145.0), refresh=5, now=_START + 145.0)
    assert 'id="democtl"' in pinned
    assert 'data-at="145.0"' in pinned
    assert 'data-anchored="1"' in pinned
    assert 'data-next-at="164.0"' in pinned
    assert 'data-next-profile="web"' in pinned, "按钮要说清是哪条隧道要变（看板上有三行）"
    assert "web" in pinned.split("下一处变化")[1][:40]
    assert "已固定" in pinned
    assert "0h 2m 25s" in pinned, "读数得是给人看的时长"
    assert 'id="demobuttons" hidden' in pinned, "按钮先藏着，等驱动它们的脚本起来"
    # 锚点也跟着进页脚的链接，否则看板钉在某一刻、status.json 却回答"现在"。
    assert 'href="/status.json?at=145.0"' in pinned

    # 实时（没被钉住）时不该在链接上塞 at，也不该说"已固定"。
    live = dashboard_html(_payload(10.0), refresh=5, now=_START + 10.0)
    assert 'id="democtl"' in live
    assert "实时" in live
    assert 'href="/status.json"' in live
    assert 'href="/status.json?at=' not in live
    assert 'href="/metrics?at=' not in live

    # 真实数据里根本没有这整个东西。
    real = _status_payload(
        DaemonStatus(
            running=True,
            pid=4242,
            started_at=_START,
            uptime_seconds=3600.0,
            profiles=[ProfileStatus(name="web", destination="deploy@edge.example.com:22")],
        )
    )
    real_html = dashboard_html(real, refresh=5, now=_START + 3600.0)
    # 样式表是所有页面共用的，所以查**标记**而不是类名。
    assert 'id="democtl"' not in real_html
    assert "演示时钟" not in real_html
    assert "演示数据" not in real_html


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
    payload = captured["provider"]({})
    assert payload["demo"]["anchored"] is False
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


def test_live_mode_ignores_the_demo_anchor(monkeypatch) -> None:
    """``?at=`` 只属于演示模式：真实状态只有“现在”，非演示模式下它必须什么都不做。

    这条钉的是一个**不该开的后门**。如果实时路径也认这个参数，那么任何能访问到这个端口
    的人（或者一个把地址栏填好的链接）就能让看板显示一个“过去/未来的”状态——而真实状态
    根本没有其它时刻可言，时间轴只存在于演示数据里。
    """

    class _Daemon:
        config = types.SimpleNamespace(serve=ServeConfig())

        def status(self) -> DaemonStatus:
            return DaemonStatus(
                running=True,
                pid=4242,
                started_at=_START,
                uptime_seconds=42.0,
                profiles=[
                    ProfileStatus(name="web", destination="deploy@edge.example.com:22")
                ],
            )

    monkeypatch.setattr("ponte.main._daemon", _Daemon)
    captured: dict = {}

    class _Server:
        def serve_forever(self, poll_interval: float = 0.5) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    def fake_create_server(provider, **kwargs):
        captured["provider"] = provider
        return _Server()

    monkeypatch.setattr("ponte.main.create_server", fake_create_server)
    result = CliRunner().invoke(app, ["serve", "--port", "8792"])
    assert result.exit_code == 0, result.output

    payload = captured["provider"]({"at": ["999"]})
    assert "demo" not in payload
    assert payload["uptime_seconds"] == pytest.approx(42.0), "实时状态不许被锚点挪动"
