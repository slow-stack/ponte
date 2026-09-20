"""A local HTTP surface for ponte: dashboard, health probe and metrics.

``ponte watch`` answers "is my tunnel up?" for a human sitting at the machine.
This module answers it for everything else:

* ``/``            a self-contained HTML dashboard — no CDN, no JavaScript, no
                   external assets, so it renders from ``curl``, a phone
                   browser on the same host, or an air-gapped box;
* ``/healthz``     a probe for uptime monitors, whose status code reports
                   whether the *tunnel* works, not whether a process exists;
* ``/metrics``     Prometheus text exposition, for a Prometheus / Grafana /
                   VictoriaMetrics scrape;
* ``/status.json`` byte-for-byte the payload of ``ponte status --json``.

Three rules shape the implementation:

* **Loopback by default, and a token to leave it.** Everything under ``/``
  names your servers, users and forwarded ports. Binding a non-loopback address
  without a token is refused up front by :func:`ponte.config.ensure_bindable`
  rather than served with a warning nobody reads.
* **Standard library only.** ``http.server``, not a web framework: ponte exists
  to be dropped on a machine that has nothing but SSH and Python, and a
  dependency would cost more than this feature is worth.
* **Read-only, and always fresh.** Every request re-reads the daemon status, so
  a page can never show a cached "healthy" for a tunnel that has since died.
  Nothing here can start, stop or reconfigure anything — the worst a leaked
  token buys is a read of your port numbers.

The one thing this module deliberately does *not* do is invent numbers: the
dashboard, the JSON, the health probe and the metrics are all rendered from the
same payload, so the web page can never disagree with ``ponte status``.
"""

from __future__ import annotations

import contextlib
import hmac
import html
import json
import logging
import socket
import time
from collections.abc import Callable, Iterable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from string import Template
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from ponte import __version__
from ponte.config import ensure_bindable
from ponte.daemon import _format_duration

__all__ = [
    "PonteHTTPServer",
    "create_server",
    "dashboard_html",
    "health_response",
    "render_metrics",
    "serve_url",
]

logger = logging.getLogger(__name__)

#: How many recent retry-loop events a dashboard card shows.
_FEED_LIMIT = 8

#: The routes this server answers. Anything else is a 404 — an explicit list
#: rather than a fallback, so a typo never silently serves something.
_ROUTES = ("/", "/healthz", "/metrics", "/status.json")

#: Event type → (glyph, tone). Mirrors ``ponte watch`` so the two views teach
#: the same visual language.
_EVENT_GLYPHS: dict[str, tuple[str, str]] = {
    "connecting": ("→", "dim"),
    "connected": ("●", "ok"),
    "disconnected": ("●", "bad"),
    "retrying": ("↻", "warn"),
    "max_retries_reached": ("✗", "bad"),
}


# ---------------------------------------------------------------------------
# Payload accessors
#
# The payload is the ``ponte status --json`` contract. These helpers are
# tolerant on purpose: the JSON is written by whichever daemon version is
# installed, so a missing or unexpected field degrades to "unknown" instead of
# turning the dashboard into a stack trace while the user is trying to read
# their status.
# ---------------------------------------------------------------------------


def _profiles(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Return the ``name → section`` map of a status payload."""
    raw = payload.get("profiles")
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(name): section
        for name, section in raw.items()
        if isinstance(section, Mapping)
    }


def _as_float(value: Any, default: float | None = None) -> float | None:
    """Coerce a JSON number, returning *default* for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _session_age(section: Mapping[str, Any], now: float) -> float | None:
    """Seconds the current session has been up, ``None`` when disconnected."""
    started = _as_float(section.get("current_session_at"))
    if started is None:
        return None
    return max(0.0, now - started)


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


def health_response(payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Build ``(status_code, body)`` for ``/healthz``.

    The code reports whether the tunnel works, which is the whole reason to
    have this endpoint instead of pinging the PID file:

    * ``503 down``     — the daemon is not running: nothing is being forwarded.
    * ``503 degraded`` — running, but at least one profile is unhealthy.
    * ``200 ok``       — running, and every profile is healthy.
    * ``200 starting`` — running, but no health check has reported yet. A fresh
      ``ponte start`` waits up to ``check_interval`` (60 s by default) for its
      first answer; calling that "down" would fire a false alert on every
      restart, and "no data yet" is not evidence of failure.
    """
    if not payload.get("running"):
        return 503, {"status": "down", "reason": "ponte daemon is not running"}

    profiles = _profiles(payload)
    if not profiles:
        return 200, {"status": "starting", "reason": "no profile has reported yet"}

    unhealthy = sorted(
        name for name, section in profiles.items() if section.get("healthy") is False
    )
    if unhealthy:
        errors = {
            name: profiles[name].get("health_error")
            for name in unhealthy
            if profiles[name].get("health_error")
        }
        return 503, {
            "status": "degraded",
            "profiles": len(profiles),
            "unhealthy": unhealthy,
            "errors": errors,
        }

    if all(section.get("healthy") is True for section in profiles.values()):
        return 200, {"status": "ok", "profiles": len(profiles)}
    return 200, {
        "status": "starting",
        "profiles": len(profiles),
        "reason": "no health check has completed yet",
    }


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------


def _escape_label(value: Any) -> str:
    """Escape a Prometheus label value (backslash, newline, double quote)."""
    return (
        str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
    )


def _metric_value(value: Any) -> str | None:
    """Format *value* as a Prometheus sample value, ``None`` when unknown.

    A missing number is *omitted* rather than exported as NaN: a gap in a graph
    says "no data", while a NaN line invites the reader to guess.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.3f}"
    return None


def _series(name: str, labels: Mapping[str, Any], value: Any) -> str | None:
    """One sample line, or ``None`` when the value is unknown."""
    formatted = _metric_value(value)
    if formatted is None:
        return None
    if not labels:
        return f"{name} {formatted}"
    rendered = ",".join(
        f'{key}="{_escape_label(val)}"' for key, val in labels.items()
    )
    return f"{name}{{{rendered}}} {formatted}"


class _Families:
    """Accumulates metric families so samples come out grouped.

    The text exposition format requires every sample of a metric to be one
    uninterrupted group, which is why this is not simply a list of lines. A
    family whose samples are all unknown is dropped entirely — an empty
    ``# HELP`` header with no samples is noise in every graph it reaches.
    """

    def __init__(self) -> None:
        self._order: list[str] = []
        self._families: dict[str, list[Any]] = {}

    def add(
        self,
        name: str,
        kind: str,
        help_text: str,
        samples: Iterable[str | None],
    ) -> None:
        """Register *samples* under the family *name* (``gauge``/``counter``)."""
        kept = [sample for sample in samples if sample]
        if not kept:
            return
        if name not in self._families:
            self._order.append(name)
            # HELP text stays English: it is read by Prometheus/Grafana
            # tooling, not by the operator's terminal.
            self._families[name] = [kind, help_text, []]
        entry = self._families[name]
        entry[2].extend(kept)

    def render(self) -> str:
        """The complete exposition text, ending with a newline."""
        lines: list[str] = []
        for name in self._order:
            kind, help_text, samples = self._families[name]
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            lines.extend(samples)
        return "\n".join(lines) + "\n"


def render_metrics(payload: Mapping[str, Any], *, now: float | None = None) -> str:
    """Render a status payload as Prometheus text exposition (``0.0.4``).

    Deliberately *not* the same signal as ``/healthz``: that one fails when the
    tunnel is broken, so a monitor can alert. This one always answers ``200``
    and reports the state as numbers, because a scrape failure would hide
    *why* the tunnel went down — exactly the question a graph exists to answer.
    """
    moment = time.time() if now is None else now
    families = _Families()

    running = bool(payload.get("running"))
    families.add(
        "ponte_up",
        "gauge",
        "1 when the ponte daemon is running.",
        [_series("ponte_up", {}, running)],
    )
    families.add(
        "ponte_build_info",
        "gauge",
        "ponte version, always 1 (join on this to graph deploys).",
        [_series("ponte_build_info", {"version": __version__}, True)],
    )
    families.add(
        "ponte_daemon_uptime_seconds",
        "gauge",
        "Seconds since the ponte daemon process started.",
        [_series("ponte_daemon_uptime_seconds", {}, payload.get("uptime_seconds"))],
    )
    if not running:
        return families.render()

    profiles = _profiles(payload)
    families.add(
        "ponte_profiles_configured",
        "gauge",
        "Profiles the daemon supervises.",
        [_series("ponte_profiles_configured", {}, len(profiles))],
    )
    families.add(
        "ponte_profiles_unhealthy",
        "gauge",
        "Profiles that are currently unhealthy.",
        [
            _series(
                "ponte_profiles_unhealthy",
                {},
                sum(
                    1
                    for section in profiles.values()
                    if section.get("healthy") is False
                ),
            )
        ],
    )
    # Identity lives in one info metric instead of being repeated as a label on
    # every series: the samples stay keyed by profile (what a legend wants), and
    # a scrape does not re-encode the same destination a dozen times.
    families.add(
        "ponte_profile_info",
        "gauge",
        "Static identity of a profile: the SSH destination it connects to.",
        [
            _series(
                "ponte_profile_info",
                {"profile": name, "destination": section.get("destination") or ""},
                True,
            )
            for name, section in profiles.items()
        ],
    )

    def add(
        name: str,
        kind: str,
        help_text: str,
        getter: Callable[[Mapping[str, Any]], Any],
    ) -> None:
        """Register one numeric family, one sample per profile."""
        families.add(
            name,
            kind,
            help_text,
            [
                _series(name, {"profile": profile}, getter(section))
                for profile, section in profiles.items()
            ],
        )

    add(
        "ponte_profile_healthy",
        "gauge",
        "1 when every health check of the profile passes; absent until the first check.",
        lambda section: section.get("healthy"),
    )
    add(
        "ponte_profile_process_alive",
        "gauge",
        "1 while the profile's SSH process is alive.",
        lambda section: section.get("process_alive"),
    )
    add(
        "ponte_profile_session_uptime_seconds",
        "gauge",
        "Age of the current SSH session; absent while the tunnel is down.",
        lambda section: _session_age(section, moment),
    )
    add(
        "ponte_profile_availability_ratio",
        "gauge",
        "Completed uptime / observed time for the profile (0..1), the same ratio ponte status prints.",
        lambda section: section.get("availability"),
    )
    add(
        "ponte_profile_connect_attempts_total",
        "counter",
        "SSH launch attempts (cumulative; survives daemon restarts).",
        lambda section: section.get("connect_attempts_total"),
    )
    add(
        "ponte_profile_sessions_total",
        "counter",
        "SSH sessions actually established.",
        lambda section: section.get("sessions_total"),
    )
    add(
        "ponte_profile_reconnects_total",
        "counter",
        "Scheduled reconnects after a drop.",
        lambda section: section.get("reconnects_total"),
    )
    add(
        "ponte_profile_tunnel_uptime_seconds",
        "counter",
        "Cumulative seconds the tunnel has been up.",
        lambda section: section.get("tunnel_uptime_seconds"),
    )
    add(
        "ponte_profile_tunnel_downtime_seconds",
        "counter",
        "Cumulative seconds the tunnel has been down.",
        lambda section: section.get("tunnel_downtime_seconds"),
    )
    add(
        "ponte_profile_last_disconnect_timestamp_seconds",
        "gauge",
        "Unix time of the most recent disconnect.",
        lambda section: section.get("last_disconnect_at"),
    )
    add(
        "ponte_profile_last_notification_timestamp_seconds",
        "gauge",
        "Unix time the most recent failure alert was delivered.",
        lambda section: section.get("last_notification_at"),
    )

    # The strongest signal of the lot: whether the port is *actually* forwarded.
    # A healthy-looking process with a dead port is the classic silent failure.
    port_samples: list[str | None] = []
    for name, section in profiles.items():
        for key, kind in (("remote_ports", "remote"), ("local_ports", "local")):
            ports = section.get(key)
            if not isinstance(ports, Mapping):
                continue
            for port, listening in ports.items():
                port_samples.append(
                    _series(
                        "ponte_profile_port_listening",
                        {"profile": name, "kind": kind, "port": port},
                        listening,
                    )
                )
    families.add(
        "ponte_profile_port_listening",
        "gauge",
        "1 when a forwarded port is listening (remote: on the server, local: on this host).",
        port_samples,
    )
    return families.render()


# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------

#: The dashboard shell. A :class:`string.Template` (``$name`` placeholders)
#: rather than ``str.format`` because the stylesheet is full of braces;
#: ``substitute`` is used on purpose so a stray placeholder fails loudly in
#: tests instead of rendering as literal text.
_PAGE = Template(
    """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="$refresh">
<meta name="color-scheme" content="dark light">
<title>ponte · $title</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ctext y='13' font-size='14'%3E%F0%9F%94%81%3C/text%3E%3C/svg%3E">
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 20px;
  font: 14px/1.6 ui-sans-serif, system-ui, "Segoe UI", "PingFang SC",
        "Microsoft YaHei", sans-serif;
  background: #0e1116; color: #e8eaed;
}
header {
  display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
  margin-bottom: 16px;
}
h1 { font-size: 17px; margin: 0; letter-spacing: .02em; }
h1 span { color: #6b7280; font-weight: 400; }
h2 { font-size: 15px; margin: 0 0 10px; display: flex; align-items: center; gap: 8px; }
h3 {
  font-size: 11px; margin: 14px 0 6px; color: #9aa4b2; font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em;
}
main { display: grid; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); gap: 14px; }
.card {
  background: #161b22; border: 1px solid #262d38; border-left: 4px solid #6b7280;
  border-radius: 10px; padding: 14px 16px;
}
.card.ok { border-left-color: #2ea043; }
.card.bad { border-left-color: #e5534b; }
.card.unknown { border-left-color: #d29922; }
.pill {
  display: inline-block; padding: 1px 8px; border-radius: 999px;
  font-size: 12px; font-weight: 600; white-space: nowrap;
}
.pill.ok { background: #10281a; color: #3fb950; border: 1px solid #2ea043; }
.pill.bad { background: #2d1416; color: #f85149; border: 1px solid #e5534b; }
.pill.unknown { background: #2b2411; color: #d29922; border: 1px solid #bb8009; }
table.kv { width: 100%; border-collapse: collapse; }
table.kv th {
  text-align: left; font-weight: 500; color: #9aa4b2; padding: 2px 10px 2px 0;
  white-space: nowrap; vertical-align: top;
}
table.kv td { padding: 2px 0; width: 100%; word-break: break-word; }
.feed {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px;
  max-height: 190px; overflow-y: auto;
}
.feed .ev { display: flex; gap: 8px; padding: 1px 0; }
.feed .t { color: #6b7280; }
.feed .i { width: 1em; text-align: center; flex: none; }
.i.ok { color: #3fb950; } .i.bad { color: #f85149; }
.i.warn { color: #d29922; } .i.dim { color: #6b7280; }
footer { margin-top: 16px; color: #6b7280; font-size: 12px; }
footer a { color: #58a6ff; text-decoration: none; }
footer a:hover { text-decoration: underline; }
.muted { color: #6b7280; }
</style>
</head>
<body>
<header><h1>ponte <span>v$version</span></h1>$summary</header>
<main>$cards</main>
<footer>数据来自 ponte status 的同一份状态，每 $refresh 秒自动刷新。接口：
<a href="/status.json">status.json</a> ·
<a href="/metrics">metrics</a> ·
<a href="/healthz">healthz</a></footer>
</body>
</html>
"""
)


def _esc(value: Any) -> str:
    """Escape a value for HTML text or attribute context.

    Everything that reaches the page goes through this: profile names come from
    the config, and disconnect reasons come from SSH's stderr — neither is
    trusted input.
    """
    return html.escape(str(value), quote=True)


def _pill(text: str, tone: str) -> str:
    """A coloured status pill (``tone`` is ``ok``/``bad``/``unknown``)."""
    return f'<span class="pill {tone}">{_esc(text)}</span>'


def _health_pill(section: Mapping[str, Any]) -> str:
    """The health pill of one profile."""
    healthy = section.get("healthy")
    if healthy is True:
        return _pill("健康", "ok")
    if healthy is False:
        return _pill("异常", "bad")
    return _pill("未知", "unknown")


def _kv(label: str, value: str) -> str:
    """One table row. *value* must already be escaped or be trusted markup."""
    return f"<tr><th>{_esc(label)}</th><td>{value}</td></tr>"


def _row(label: str, value: Any) -> str:
    """One table row from a plain (untrusted) value."""
    return _kv(label, _esc(value))


def _port_rows(section: Mapping[str, Any]) -> str:
    """Rows for the forwarded ``-R``/``-L``/``-D`` ports of one profile."""
    rows: list[str] = []
    for key, label in (("remote_ports", "远程端口"), ("local_ports", "本地端口")):
        ports = section.get(key)
        if not isinstance(ports, Mapping) or not ports:
            continue
        for port, listening in sorted(ports.items(), key=_port_order):
            rows.append(
                _kv(
                    f"{label} {port}",
                    _pill("监听中", "ok") if listening else _pill("未监听", "bad"),
                )
            )
    return "".join(rows)


def _port_order(item: tuple[Any, Any]) -> int:
    """Sort key putting ports in numeric order (the JSON keys are strings)."""
    try:
        return int(item[0])
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 0


def _feed(section: Mapping[str, Any]) -> str:
    """The recent-event feed of one profile, newest first."""
    events = section.get("recent_events")
    if not isinstance(events, list) or not events:
        return '<p class="muted">暂无事件</p>'
    lines: list[str] = []
    for event in reversed(events[-_FEED_LIMIT:]):
        if not isinstance(event, Mapping):  # pragma: no cover - defensive
            continue
        etype = str(event.get("type", "?"))
        glyph, tone = _EVENT_GLYPHS.get(etype, ("·", "dim"))
        at = _as_float(event.get("at"), 0.0) or 0.0
        stamp = time.strftime("%H:%M:%S", time.localtime(at))
        detail = etype
        if event.get("reason"):
            detail = f"{etype}: {event['reason']}"
        elif event.get("attempt"):
            delay = _as_float(event.get("delay"), 0.0) or 0.0
            detail = f"{etype}: 第 {event['attempt']} 次，{delay:.1f}s 后重试"
        lines.append(
            f'<div class="ev"><span class="t">{_esc(stamp)}</span>'
            f'<span class="i {tone}">{_esc(glyph)}</span>{_esc(detail)}</div>'
        )
    return "".join(lines)


def _profile_card(name: str, section: Mapping[str, Any], *, now: float) -> str:
    """One profile's card: identity, statistics, ports and event feed."""
    healthy = section.get("healthy")
    tone = "ok" if healthy is True else ("bad" if healthy is False else "unknown")
    rows: list[str] = []

    if section.get("destination"):
        rows.append(_row("目标", section["destination"]))
    if section.get("process_alive") is not None:
        rows.append(
            _kv(
                "SSH 进程",
                _pill("运行中", "ok")
                if section.get("process_alive")
                else _pill("已退出", "bad"),
            )
        )

    started = _as_float(section.get("current_session_at"))
    if started is None:
        rows.append(_kv("当前会话", _pill("已断开", "bad")))
    else:
        rows.append(_row("当前会话", _format_duration(now - started)))

    availability = _as_float(section.get("availability"))
    if availability is not None:
        rows.append(_row("在线率", f"{availability * 100:.1f}%"))

    if section.get("sessions_total") is not None:
        rows.append(
            _row(
                "会话统计",
                f"会话 {section.get('sessions_total')} 次 · "
                f"重连 {section.get('reconnects_total')} 次 · "
                f"启动尝试 {section.get('connect_attempts_total')} 次",
            )
        )

    up_total = _as_float(section.get("tunnel_uptime_seconds"))
    if up_total is not None:
        down_total = _as_float(section.get("tunnel_downtime_seconds"), 0.0) or 0.0
        rows.append(
            _row(
                "累计在线 / 离线",
                f"{_format_duration(up_total)} / {_format_duration(down_total)}",
            )
        )

    rows.append(_port_rows(section))

    if section.get("last_disconnect_reason"):
        detail = str(section["last_disconnect_reason"])
        at = _as_float(section.get("last_disconnect_at"))
        if at is not None:
            detail += (
                f"（{_format_duration(now - at)}前，"
                f"{time.strftime('%m-%d %H:%M:%S', time.localtime(at))}）"
            )
        rows.append(_row("上次断线", detail))

    notified = _as_float(section.get("last_notification_at"))
    if notified is not None:
        rows.append(_row("上次通知", f"{_format_duration(now - notified)}前"))

    if section.get("health_error"):
        rows.append(_row("检查错误", section["health_error"]))
    if section.get("error"):
        rows.append(_row("循环错误", section["error"]))

    return (
        f'<section class="card {tone}">'
        f"<h2>{_esc(name)} {_health_pill(section)}</h2>"
        f'<table class="kv">{"".join(rows)}</table>'
        f'<h3>最近事件</h3><div class="feed">{_feed(section)}</div>'
        f"</section>"
    )


def _summary(payload: Mapping[str, Any], profiles: Mapping[str, Any]) -> str:
    """The header line: overall state, pid, daemon uptime and profile count."""
    if not payload.get("running"):
        return _pill("守护进程未运行", "bad") + '<span class="muted">可执行 ponte start 启动</span>'
    unhealthy = [
        name for name, section in profiles.items() if section.get("healthy") is False
    ]
    if unhealthy:
        overall = _pill(f"{len(unhealthy)}/{len(profiles)} 条隧道异常", "bad")
    elif profiles and all(
        section.get("healthy") is True for section in profiles.values()
    ):
        overall = _pill("全部健康", "ok")
    else:
        overall = _pill("等待首次检查", "unknown")
    pieces = [overall]
    if payload.get("pid") is not None:
        pieces.append(f'<span class="muted">pid {_esc(payload["pid"])}</span>')
    uptime = _as_float(payload.get("uptime_seconds"))
    if uptime is not None:
        pieces.append(f'<span class="muted">守护进程运行 {_esc(_format_duration(uptime))}</span>')
    pieces.append(f'<span class="muted">{len(profiles)} 条隧道</span>')
    return "".join(pieces)


def dashboard_html(
    payload: Mapping[str, Any],
    *,
    refresh: int = 5,
    now: float | None = None,
) -> str:
    """Render the whole dashboard page from a ``ponte status`` payload."""
    moment = time.time() if now is None else now
    profiles = _profiles(payload)
    if not payload.get("running"):
        cards: list[str] = [
            '<section class="card bad"><h2>守护进程未运行</h2>'
            '<p class="muted">先执行 <code>ponte start</code> 启动隧道，'
            "本页面会在下一轮自动刷新。</p></section>"
        ]
    else:
        cards = [
            _profile_card(name, section, now=moment)
            for name, section in profiles.items()
        ]
        if not cards:
            cards = [
                '<section class="card unknown"><h2>尚无隧道上报</h2>'
                '<p class="muted">守护进程已启动，等待第一次健康检查。</p></section>'
            ]
    return _PAGE.substitute(
        refresh=max(1, int(refresh)),
        version=_esc(__version__),
        title="隧道看板",
        summary=_summary(payload, profiles),
        cards="".join(cards),
    )


# ---------------------------------------------------------------------------
# The HTTP server
# ---------------------------------------------------------------------------


def serve_url(host: str, port: int, path: str = "/") -> str:
    """The URL to open for a bind *host*/*port* (``""`` means IPv4 loopback)."""
    if not host:
        return f"http://127.0.0.1:{port}{path}"
    if ":" in host and not host.startswith("["):
        return f"http://[{host}]:{port}{path}"
    return f"http://{host}:{port}{path}"


def _address_family(host: str) -> int:
    """Pick the socket family for *host* (IPv6 literals contain a colon)."""
    return socket.AF_INET6 if ":" in host else socket.AF_INET


class PonteHTTPServer(ThreadingHTTPServer):
    """The server ``ponte serve`` runs, carrying its own status provider.

    Subclassing rather than closing over the provider in the handler keeps the
    request handlers plain methods — and lets a test start the server on port
    ``0`` and read back the port the OS assigned.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        provider: Callable[[], dict[str, Any]],
        *,
        token: str = "",
        refresh: int = 5,
    ) -> None:
        # Set before ``super().__init__``: TCPServer creates its socket there,
        # so an IPv6 loopback (``::1``) has to pick its family first.
        self.address_family = _address_family(address[0])
        self.provider = provider
        self.token = token
        self.refresh = refresh
        super().__init__(address, PonteRequestHandler)


class PonteRequestHandler(BaseHTTPRequestHandler):
    """Serves the four read-only endpoints; one instance per connection."""

    server_version = f"ponte/{__version__}"
    sys_version = ""
    # HTTP/1.1 keeps the connection alive for a scraper's next request; every
    # response sends an explicit Content-Length, which is what that requires.
    protocol_version = "HTTP/1.1"

    @property
    def _state(self) -> PonteHTTPServer:
        """The owning server, typed so the handler can read its settings."""
        return cast(PonteHTTPServer, self.server)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Route the stdlib's stderr chatter into the ponte logger."""
        logger.debug("%s %s", self.address_string(), format % args)

    # -- methods -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._respond(head=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._respond(head=True)

    def do_POST(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        """Everything here is read-only, and says so instead of pretending."""
        self._send_json(
            405,
            {"error": "read-only endpoint; use GET"},
            extra={"Allow": "GET, HEAD"},
        )

    # -- routing -----------------------------------------------------------

    def _respond(self, *, head: bool) -> None:
        """Route one request: 404 → 401 → the endpoint."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path not in _ROUTES:
            self._send_json(
                404,
                {"error": "not found", "endpoints": list(_ROUTES)},
                head=head,
            )
            return
        if not self._authorized(parse_qs(parsed.query)):
            self._send_json(
                401,
                {"error": "unauthorized: missing or invalid token"},
                head=head,
                extra={"WWW-Authenticate": 'Bearer realm="ponte"'},
            )
            return

        try:
            payload = self._state.provider()
        except Exception as exc:  # noqa: BLE001 - answer, never drop the client
            logger.exception("serve: status provider failed")
            self._send_json(
                500, {"error": f"status unavailable: {type(exc).__name__}"}, head=head
            )
            return

        if path in ("/", "/index.html"):
            self._send_text(
                200,
                dashboard_html(payload, refresh=self._state.refresh),
                "text/html; charset=utf-8",
                head=head,
            )
        elif path == "/healthz":
            code, body = health_response(payload)
            self._send_json(code, body, head=head)
        elif path == "/metrics":
            self._send_text(
                200,
                render_metrics(payload),
                "text/plain; version=0.0.4; charset=utf-8",
                head=head,
            )
        else:  # "/status.json"
            self._send_json(200, dict(payload), head=head)

    def _authorized(self, query: Mapping[str, list[str]]) -> bool:
        """Check the token in constant time, from the header or ``?token=``.

        The query string exists because Prometheus scrapes a URL and per-target
        ``Authorization`` headers are awkward to configure; the header is what a
        hand-written client would use.
        """
        expected = self._state.token
        if not expected:
            return True
        supplied = ""
        header = self.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            supplied = header[7:].strip()
        if not supplied:
            supplied = (query.get("token") or [""])[0]
        return hmac.compare_digest(
            supplied.encode("utf-8"), expected.encode("utf-8")
        )

    # -- responses ---------------------------------------------------------

    def _send(
        self,
        code: int,
        body: bytes,
        content_type: str,
        *,
        head: bool,
        extra: Mapping[str, str] | None = None,
    ) -> None:
        """Send one complete response (never cached)."""
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Status data must never be cached: a browser or proxy replaying a
        # "healthy" page for a tunnel that has since died is exactly the
        # failure this endpoint exists to prevent.
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if head:
            return
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def _send_json(
        self,
        code: int,
        payload: Mapping[str, Any],
        *,
        head: bool = False,
        extra: Mapping[str, str] | None = None,
    ) -> None:
        """Send a JSON response (compact: this is machine food)."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(
            code,
            body,
            "application/json; charset=utf-8",
            head=head,
            extra=extra,
        )

    def _send_text(
        self,
        code: int,
        text: str,
        content_type: str,
        *,
        head: bool,
    ) -> None:
        """Send a text response (HTML or metrics)."""
        self._send(code, text.encode("utf-8"), content_type, head=head)


def create_server(
    provider: Callable[[], dict[str, Any]],
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    token: str = "",
    refresh: int = 5,
) -> PonteHTTPServer:
    """Build (but do not start) the dashboard server.

    The caller owns the lifecycle; ``ponte serve`` runs ``serve_forever`` on it,
    a test calls ``serve_forever`` on a thread and shuts it down again.

    Raises:
        ConfigValidationError: when *host* is not loopback and *token* is empty.
        OSError: when the address is unusable or the port is already taken.
    """
    ensure_bindable(host, token)
    return PonteHTTPServer((host, port), provider, token=token, refresh=refresh)
