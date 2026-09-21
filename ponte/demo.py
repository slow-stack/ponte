"""``ponte serve --demo`` 的内置演示数据：一条按真实节奏推进的时间轴。

为什么这件事该在产品里，而不是让每个人自己在外头造一份状态文件：看板是纯函数渲染出来的
HTML，但它只从**真实**的配置与守护进程状态文件取数据。于是"先看一眼前端长什么样"这件事，
在你还没有隧道、或者不想为了看页面去连真服务器时，只能靠手搓状态文件——那份文件会随
payload 契约漂移而悄悄失真，而它长得和真数据一模一样，最容易被当成真的。演示模式把它收进
产品里，并且**在页面、JSON 与命令行三处都标明这是演示数据**。

数据是**确定性的**：每个 profile 是一条循环时间轴，同一时刻的 payload 完全相同，所以测试
可以钉住某个时刻断言，而不是靠 `sleep` 去等一个事件发生。

三个 profile 分别覆盖看板会渲染的三种状态：

===================  ==========================================================
``web``              稳态 + 偶发断线重连：健康 → 异常 → 再连上，会话统计与事件流都在动
``db``               SSH 在、但服务器上的端口不监听：健康是**确定失败**，而可用率并不低
                     —— 这两列本来就不是一回事，演示数据把它显出来
``metrics``          探针偶尔得不出结论：端口是"**未观测**"而不是"未监听"（#25 的语义）
===================  ==========================================================

用法：``ponte serve --demo``（见 ``ponte.main.serve``）。

时间轴可以被**钉在某一刻**：``?at=<秒>``（相对启动）。锚点是**读**而不是写——时间轴本来
就是"某个时刻的纯函数"，所以让那个时刻从请求里来，服务端不必持有任何可变状态；去掉这个
参数就回到实时。于是一个 URL 就能把看板固定在某一刻，截图里自帯"这是哪一刻"，而页面上的
按钮只是会改地址栏里那个数字的便利层。
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: 时间轴上的状态。前三个对应守护进程真实经历的三段，``UNKNOWN`` 对应"探针没问出结论"。
CONNECTED = "connected"
DISCONNECTED = "disconnected"
RETRYING = "retrying"
UNKNOWN = "unknown"

#: 事件流保留多少条（与看板的 ``_FEED_LIMIT`` 对齐；多几条无妨，看板会自己截断）。
_FEED_LIMIT = 8

#: ``?at=`` 的上限：一天。更远的锚点没有意义，也顺手挡住离谱输入。
_AT_LIMIT = 86_400.0

#: 这些状态下 SSH 会话还活着——可用率因此把它们算成"在线"。
_ALIVE_STATES = frozenset({CONNECTED, UNKNOWN})


@dataclass(frozen=True)
class Phase:
    """时间轴的一段：``seconds`` 秒里，看板看到的一切都由 ``state`` 决定。

    ``reason`` 的含义随状态变：``CONNECTED`` 时它是**检查错误**（连上了但端口没通），
    ``DISCONNECTED``/``RETRYING`` 时它是**循环错误/上次断线原因**，``UNKNOWN`` 时它是
    **探测失败的原因**。
    """

    state: str
    seconds: float
    reason: str = ""
    #: ``RETRYING`` 段里每隔多久算一次失败的尝试（也用来算退避等待）。
    attempt_interval: float = 5.0


@dataclass(frozen=True)
class DemoProfile:
    """一条演示隧道：身份、端口，以及它循环经历的时间轴。"""

    name: str
    destination: str
    remote_ports: tuple[int, ...]
    local_ports: tuple[int, ...]
    phases: tuple[Phase, ...]
    jump: str | None = None
    #: 每隔多少个循环发一次"通知"（对应看板明细里的"上次通知"一行）；0 = 从不。
    notify_every: int = 0

    @property
    def cycle_seconds(self) -> float:
        return sum(phase.seconds for phase in self.phases)

    @property
    def reconnect_index(self) -> int:
        """这一轮里"重连成功"之后会话从第几段开始。

        没有 RETRYING 就返回 ``len(phases)``——一个永远到不了的序号，于是"本轮是否已经重连过"
        对所有 profile 都是同一个判断（否则一条从不掉线的隧道会被凭空记出更多会话）。
        """
        retries = [i for i, phase in enumerate(self.phases) if phase.state == RETRYING]
        return retries[-1] + 1 if retries else len(self.phases)

    @property
    def retry_phase(self) -> Phase | None:
        retries = [p for p in self.phases if p.state == RETRYING]
        return retries[-1] if retries else None

    @property
    def disconnects(self) -> bool:
        """这条隧道会不会真的掉线（`metrics` 那条就不会）。"""
        return any(p.state in (DISCONNECTED, RETRYING) for p in self.phases)


#: 演示用域名一律是 reserved 域名：截图里也能一眼看出这不是真基础设施。
_PROFILES: tuple[DemoProfile, ...] = (
    DemoProfile(
        name="web",
        destination="deploy@edge.example.com:22",
        jump="ops@bastion.example.com",
        remote_ports=(23334,),
        local_ports=(1080,),
        phases=(
            Phase(CONNECTED, 140.0),
            Phase(DISCONNECTED, 6.0, "ssh exited with code 255"),
            Phase(RETRYING, 18.0),
            Phase(CONNECTED, 16.0),
        ),
    ),
    DemoProfile(
        name="db",
        destination="ops@db.internal.example.com:22",
        remote_ports=(5432,),
        local_ports=(5432,),
        notify_every=2,
        phases=(
            # 连上了、本地也在听，但服务器那一侧的 5432 没起来：健康是确定失败。
            Phase(CONNECTED, 25.0, "remote port 5432 is not listening"),
            Phase(DISCONNECTED, 5.0, "ssh exited with code 255"),
            Phase(RETRYING, 90.0),
        ),
    ),
    DemoProfile(
        name="metrics",
        destination="ops@metrics.example.com:22",
        remote_ports=(9090,),
        local_ports=(9090,),
        phases=(
            Phase(CONNECTED, 170.0),
            Phase(UNKNOWN, 40.0, "ssh exited with code 255 while probing"),
        ),
    ),
)


def _is_up(state: str) -> bool:
    return state in _ALIVE_STATES


def _parse_at(query: Mapping[str, list[str]] | None) -> float | None:
    """读 ``?at=`` 锚点：一个非负的有限秒数，否则 ``None``（跟随墙上时钟）。

    坏值一律回退到实时而不报错：这是只读的演示接口，打错一个参数不该让页面挂掉（尤其因为
    地址栏里那个参数是按钮写上去的，刷新、手改、转发都可能弄出奇怪的值）。
    """
    values = (query or {}).get("at")
    if not values:
        return None
    try:
        at = float(values[0].strip())
    except (AttributeError, TypeError, ValueError):
        return None
    if not math.isfinite(at) or at < 0:
        return None
    return min(at, _AT_LIMIT)


def _phase_starts(profile: DemoProfile) -> list[float]:
    """每一段在循环内的起点（秒）。"""
    starts: list[float] = []
    cursor = 0.0
    for phase in profile.phases:
        starts.append(cursor)
        cursor += phase.seconds
    return starts


def _locate(profile: DemoProfile, offset: float) -> int:
    """``offset``（循环内秒数）落在第几段。"""
    starts = _phase_starts(profile)
    for index in range(len(profile.phases) - 1, -1, -1):
        if offset >= starts[index]:
            return index
    return 0  # pragma: no cover - offset 非负时上面的循环总会命中


def _up_stretch_start(profile: DemoProfile, index: int) -> float:
    """当前这段"在线"是从这一轮的哪个偏移开始的（用来算会话时长）。"""
    starts = _phase_starts(profile)
    start = 0.0
    for i, phase in enumerate(profile.phases):
        if i == index:
            break
        if phase.state in (DISCONNECTED, RETRYING):
            start = starts[i] + phase.seconds
    return start


def _events(profile: DemoProfile, started: float, now: float) -> list[dict[str, Any]]:
    """最近几次状态转换，按时间升序（与守护进程写进状态文件的顺序一致）。

    转换发生在每一段的起点，所以"最近几条"可以从当前这一轮往回数出来——不需要在内存里
    维护历史，也不需要真的等它发生。
    """
    starts = _phase_starts(profile)
    elapsed = max(0.0, now - started)
    cycle = profile.cycle_seconds
    offset = elapsed % cycle
    events: list[dict[str, Any]] = []
    index = min(_locate(profile, offset), len(profile.phases) - 1)

    for cycle_index in range(int(elapsed // cycle), -1, -1):
        for i in range(index, -1, -1):
            at = started + cycle_index * cycle + starts[i]
            if at > now:
                continue
            events.append(_event_for(profile.phases[i], at))
            if len(events) >= _FEED_LIMIT:
                break
        if len(events) >= _FEED_LIMIT:
            break
        index = len(profile.phases) - 1
    events.reverse()
    return events


def _event_for(phase: Phase, at: float) -> dict[str, Any]:
    """一段的起点对应什么事件。``UNKNOWN`` 不发事件——"探针没问出结论"不是隧道发生的事。"""
    if phase.state == CONNECTED:
        return {"at": at, "type": "connected"}
    if phase.state == DISCONNECTED:
        return {"at": at, "type": "disconnected", "reason": phase.reason}
    if phase.state == RETRYING:
        return {
            "at": at,
            "type": "retrying",
            "attempt": 1,
            "delay": phase.attempt_interval,
        }
    return {}  # UNKNOWN：填位，下面过滤掉


def _profile_payload(profile: DemoProfile, *, started: float, now: float) -> dict[str, Any]:
    """把时间轴上的某一刻换算成一段状态文件里的段落。"""
    cycle = profile.cycle_seconds
    elapsed = max(0.0, now - started)
    cycles, offset = divmod(elapsed, cycle)
    index = _locate(profile, offset)
    phase = profile.phases[index]
    starts = _phase_starts(profile)
    into = offset - starts[index]
    up = _is_up(phase.state)

    # 可用 / 不可用时间的累计：整轮 + 本轮已经走过的部分。
    up_per_cycle = sum(p.seconds for p in profile.phases if _is_up(p.state))
    up_before = sum(p.seconds for p in profile.phases[:index] if _is_up(p.state))
    uptime = cycles * up_per_cycle + up_before + (into if up else 0.0)
    downtime = max(0.0, elapsed - uptime)
    total = uptime + downtime

    # 会话：第 0 秒就有第一条；只有在真的会掉线的 profile 上，每轮"重连回来"那一 段再添一条。
    retry = profile.retry_phase
    per_cycle_reconnects = 1 if retry is not None else 0
    sessions = (
        1
        + int(cycles) * per_cycle_reconnects
        + (1 if index >= profile.reconnect_index else 0)
    )
    reconnects = sessions - 1
    attempts = 1
    if retry is not None:
        per_cycle = max(1, int(math.ceil(retry.seconds / retry.attempt_interval)))
        attempts += int(cycles) * (1 + per_cycle)
        if index >= profile.reconnect_index:
            attempts += per_cycle
        elif phase.state == RETRYING:
            attempts += int(into // retry.attempt_interval)

    # 最近一次断线：本轮里在当前位置之前的那次，否则上一轮的最后一次。
    disconnects = [
        (starts[i], p.reason)
        for i, p in enumerate(profile.phases)
        if p.state == DISCONNECTED
    ]
    last_down_at: float | None = None
    last_down_reason: str | None = None
    if disconnects:
        before = [item for item in disconnects if item[0] <= offset]
        if before:
            at_offset, last_down_reason = before[-1]
            last_down_at = started + cycles * cycle + at_offset
        elif cycles >= 1:
            # 上一轮确实走完了，取那一轮的最后一次断线。
            at_offset, last_down_reason = disconnects[-1]
            last_down_at = started + (cycles - 1) * cycle + at_offset
        # 否则就是"还没断过"：进程启动还不满一个周期、本轮也还没走到断线那一段。回头去取
        # 上一轮是错的——那一轮根本没发生过，页面会写出"已运行 1 分 20 秒"+"上次断线 2 分钟
        # 前"这种自相矛盾的组合，而这恰恰是演示模式最不该犯的错（它的全部用处就是给人看页面）。

    notified: float | None = None
    if profile.notify_every:
        bucket = int(cycles) - (int(cycles) % profile.notify_every)
        candidate = started + bucket * cycle + starts[profile.reconnect_index - 1]
        notified = candidate if candidate <= now else None

    # 会话起点：从不掉线的那条从启动算起（否则每一轮都会"重新开始"，而会话数却没变）；
    # 会掉线的那条，起点就是本轮这条"在线"区间的开头。
    if profile.disconnects:
        session_start = started + cycles * cycle + _up_stretch_start(profile, index)
    else:
        session_start = started

    healthy = phase.state == CONNECTED and not phase.reason
    conclusive = phase.state != UNKNOWN
    return {
        "destination": profile.destination,
        "jump": profile.jump,
        "healthy": healthy,
        "process_alive": up,
        "health_error": phase.reason if phase.state == CONNECTED and phase.reason else None,
        "health_conclusive": conclusive,
        "probe_error": phase.reason if phase.state == UNKNOWN else None,
        "error": phase.reason if phase.state in (DISCONNECTED, RETRYING) and phase.reason else None,
        "remote_ports": {
            str(port): _port_state(phase, remote=True) for port in profile.remote_ports
        },
        "local_ports": {
            str(port): _port_state(phase, remote=False) for port in profile.local_ports
        },
        "connect_attempts_total": attempts,
        "sessions_total": sessions,
        "reconnects_total": reconnects,
        "tunnel_uptime_seconds": round(uptime, 1),
        "tunnel_downtime_seconds": round(downtime, 1),
        "availability": round(uptime / total, 3) if total else None,
        "current_session_at": (session_start if up else None),
        "last_disconnect_at": last_down_at,
        "last_disconnect_reason": last_down_reason,
        "last_notification_at": notified,
        "recent_events": [event for event in _events(profile, started, now) if event],
    }


def _visible_state(profile: DemoProfile, index: int) -> tuple[Any, ...]:
    """看板在一段里渲染出来的"状态"：判定、结论是否确定、进程在不在、两列端口。

    这是"**有没有变化**"的判据——它跟着看板走，而不是跟着阶段走（见 :func:`_next_change`）。
    """
    phase = profile.phases[index]
    return (
        phase.state == CONNECTED and not phase.reason,
        phase.state != UNKNOWN,
        _is_up(phase.state),
        tuple(_port_state(phase, remote=True) for _ in profile.remote_ports),
        tuple(_port_state(phase, remote=False) for _ in profile.local_ports),
    )


def _next_change(profile: DemoProfile, elapsed: float) -> tuple[float, int] | None:
    """``elapsed`` 之后，这条隧道下一次可见的变化：(虚拟秒, 循环内第几段)。

    判据是"看板上看得见的东西变了没有"，而不是"进入下一段"。理由具体：像 ``web`` 的
    "断线 → 重连退避"那两段，判定、两列端口完全一样（页面上的字也几乎一样），中间那个边界
    跳过去等于按钮失灵。所以拿 :func:`_visible_state` 比，跳过那些看不出来的边界。

    周期是有限的，所以变化一定在"当前这一轮剩下的边界 + 下一轮"里，不需要无界搜索。
    """
    cycle = profile.cycle_seconds
    starts = _phase_starts(profile)
    current = _visible_state(profile, _locate(profile, elapsed % cycle))
    base = int(elapsed // cycle)
    for cycle_index in (base, base + 1):
        for index, start in enumerate(starts):
            when = cycle_index * cycle + start
            if when <= elapsed:
                continue
            if _visible_state(profile, index) != current:
                return when, index
    return None  # pragma: no cover - 三个演示 profile 每一轮都会变


def _next_change_at(elapsed: float) -> tuple[float, str, str] | None:
    """全部 profile 里最近的一次可见变化：(虚拟秒, 哪条隧道, 变成什么状态)。"""
    best: tuple[float, str, str] | None = None
    for profile in _PROFILES:
        found = _next_change(profile, elapsed)
        if found is None:  # pragma: no cover - 见 _next_change
            continue
        when, index = found
        if best is None or when < best[0]:
            best = (when, profile.name, profile.phases[index].state)
    return best


def _port_state(phase: Phase, *, remote: bool) -> bool:
    """端口在某一刻的样子。

    ``UNKNOWN`` 返回 ``False`` 只是为了给出一个值——调用方在那种状态下会把整个映射**丢掉**
    （"未观测"必须与"观测到关闭"区分开），见 :func:`_profile_payload`。
    """
    if phase.state != CONNECTED:
        return False
    return not (remote and phase.reason)





class DemoStatus:
    """``ponte serve`` 的状态来源：一个可调用的演示 payload 生成器。

    只要能被调用、且返回 ``ponte status --json`` 那样的字典，就可以当 provider——看板的
    HTTP 层因此不需要为演示模式改动任何东西。
    """

    def __init__(self, *, started_at: float | None = None) -> None:
        self.started_at = time.time() if started_at is None else started_at
        #: 演示的"守护进程"就是提供看板的这个进程，pid 因此是真的。
        self.pid = os.getpid()

    def payload(
        self, *, now: float | None = None, anchored: bool = False
    ) -> dict[str, Any]:
        """某一刻的完整 payload（``now`` 可注入，便于测试钉住时刻）。

        ``anchored`` 只描述"这一刻是怎么来的"（``?at=`` 钉住的，还是墙上时钟），供页面把
        时钟标的诚实（"已固定" / "实时"）；它不影响任何一个数据字段。
        """
        moment = time.time() if now is None else now
        profiles: dict[str, dict[str, Any]] = {}
        for profile in _PROFILES:
            section = _profile_payload(profile, started=self.started_at, now=moment)
            if not section["health_conclusive"]:
                # "未观测"与"观测到关闭"必须区分开（#25）：整张映射丢掉，而不是留一堆 False。
                section["remote_ports"] = {}
                section["local_ports"] = {}
            profiles[profile.name] = section
        healthy = all(section["healthy"] for section in profiles.values())
        elapsed = max(0.0, moment - self.started_at)
        upcoming = _next_change_at(elapsed)
        return {
            "running": True,
            "pid": self.pid,
            "started_at": self.started_at,
            "uptime_seconds": elapsed,
            "healthy": healthy,
            # 一个对象而不是 ``true``：标记与"当前停在哪一刻、下一次变化在哪"是同一件事——
            # 都是演示特有的，而且都必须与看板同时到达客户端（否则按钮会基于过期的时刻跳）。
            # 它仍然是 payload 里唯一多出来的键，契约测试因此没变松。
            "demo": {
                "at": round(elapsed, 1),
                "anchored": anchored,
                "next_at": round(upcoming[0], 1) if upcoming else None,
                "next_profile": upcoming[1] if upcoming else None,
                "next_state": upcoming[2] if upcoming else None,
            },
            "profiles": profiles,
        }

    def __call__(self, query: Mapping[str, list[str]] | None = None) -> dict[str, Any]:
        """``ponte serve`` 每次请求都调这个；``?at=`` 把这一刻钉在时间轴的任意位置。"""
        at = _parse_at(query)
        if at is None:
            return self.payload(anchored=False)
        return self.payload(now=self.started_at + at, anchored=True)
