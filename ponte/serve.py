"""A local HTTP surface for ponte: dashboard, health probe and metrics.

``ponte watch`` answers "is my tunnel up?" for a human sitting at the machine.
This module answers it for everything else:

* ``/``            a self-contained HTML dashboard — one file, no CDN, no
                   external assets, so it renders from ``curl``, a phone
                   browser on the same host, or an air-gapped box. It is
                   rendered complete on the server and works with scripting
                   off (a ``<noscript>`` meta refresh reloads it); an inline
                   script only upgrades that reload into an in-place refresh;
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
  dependency would cost more than this feature is worth. The same instinct
  applies to the page: no bundler, no framework, one inline script that does
  one job.
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


def _health_detail(section: Mapping[str, Any]) -> str | None:
    """The reason attached to a profile's health mark, if any.

    ``probe_error`` is listed as well as ``health_error`` because a failed
    probe reports itself separately — that is what makes "we could not ask"
    distinguishable from "we asked and the answer was no".
    """
    return section.get("health_error") or section.get("probe_error") or None


def _failure_reason(section: Mapping[str, Any]) -> str | None:
    """Why a profile is unhealthy, when the daemon recorded no reason of its own.

    A conclusive failure often carries no error string: the probe ran fine and
    simply found a port closed, so the reason has to be read back out of the
    observed port states. Without this, ``/healthz`` answers ``503 degraded``
    and leaves the reader with nothing to act on.
    """
    detail = _health_detail(section)
    if detail:
        return detail
    if section.get("process_alive") is False:
        return "SSH process is not running"
    reasons: list[str] = []
    for key, kind in (("remote_ports", "remote"), ("local_ports", "local")):
        ports = section.get(key)
        if not isinstance(ports, Mapping):
            continue
        for port, listening in sorted(ports.items(), key=lambda item: str(item[0])):
            if listening is False:
                label = "port" if kind == "remote" else "local port"
                reasons.append(f"{label} {port} is not listening")
    return "; ".join(reasons) or None


def _inconclusive(section: Mapping[str, Any]) -> bool:
    """Whether a profile's last check failed to reach a verdict.

    The daemon marks this explicitly (``health_conclusive``): ``healthy`` is
    False either way, but only a *conclusive* failure is evidence that the
    tunnel is broken. A shared/NATed uplink drops the probe's own connection
    often enough that reporting every such tick as degraded turns a monitoring
    endpoint into noise nobody reads.
    """
    return section.get("healthy") is False and section.get("health_conclusive") is False


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

    * ``503 down``       — the daemon is not running: nothing is being forwarded.
    * ``503 degraded``   — running, but at least one profile conclusively failed.
    * ``200 unverified`` — running, but every recent check failed to reach a
      verdict: the probe's own connection could not be made, so the state is
      unknown rather than broken. Kept out of ``degraded`` on purpose — a
      flaky path to the server would otherwise page on every restart.
    * ``200 ok``         — running, and every profile is healthy.
    * ``200 starting``   — running, but no health check has reported yet. A fresh
      ``ponte start`` waits up to ``check_interval`` (60 s by default) for its
      first answer; calling that "down" would fire a false alert on every
      restart, and "no data yet" is not evidence of failure.
    """
    if not payload.get("running"):
        return 503, {"status": "down", "reason": "ponte daemon is not running"}

    profiles = _profiles(payload)
    if not profiles:
        return 200, {"status": "starting", "reason": "no profile has reported yet"}

    unknown = sorted(name for name, s in profiles.items() if _inconclusive(s))
    unhealthy = sorted(
        name
        for name, section in profiles.items()
        if section.get("healthy") is False and not _inconclusive(section)
    )
    if unhealthy:
        errors: dict[str, Any] = {}
        for name in unhealthy:
            detail = _failure_reason(profiles[name])
            if detail:
                errors[name] = detail
        body: dict[str, Any] = {
            "status": "degraded",
            "profiles": len(profiles),
            "unhealthy": unhealthy,
            "errors": errors,
        }
        if unknown:
            body["unknown"] = unknown
        return 503, body

    if unknown:
        return 200, {
            "status": "unverified",
            "profiles": len(profiles),
            "unknown": unknown,
            "reason": "health checks could not be completed; the tunnel state "
            "is unknown, not broken",
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
        "Profiles that conclusively failed their last health check.",
        [
            _series(
                "ponte_profiles_unhealthy",
                {},
                sum(
                    1
                    for section in profiles.values()
                    if section.get("healthy") is False
                    and not _inconclusive(section)
                ),
            )
        ],
    )
    families.add(
        "ponte_profiles_unknown",
        "gauge",
        "Profiles whose last health check could not be completed "
        "(unknown state, not necessarily broken).",
        [
            _series(
                "ponte_profiles_unknown",
                {},
                sum(1 for section in profiles.values() if _inconclusive(section)),
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
        "1 when every health check of the profile passes; absent until the first "
        "check, and absent while a check cannot be completed (see "
        "ponte_profiles_unknown) — an unanswered probe is not a failed tunnel.",
        lambda section: None if _inconclusive(section) else section.get("healthy"),
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
<meta name="color-scheme" content="dark light">
<!-- The reload fallback for a browser that runs no script. It lives in
     <noscript> on purpose: a browser with scripting enabled never creates the
     meta element at all, so it cannot race the in-place refresh below. -->
<noscript><meta http-equiv="refresh" content="$refresh"></noscript>
<title>ponte · $title</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ctext y='13' font-size='14'%3E%F0%9F%94%81%3C/text%3E%3C/svg%3E">
<style>
:root {
  color-scheme: dark;
  --ok: #3fb950; --ok-bg: #0f2a1a; --ok-line: #2ea043;
  --bad: #f85149; --bad-bg: #2d1416; --bad-line: #e5534b;
  --unk: #d29922; --unk-bg: #2b2411; --unk-line: #bb8009;
  --accent: #58a6ff;
  --bg: #0b0f14; --panel: #131922; --row: #18202b; --inset: #0f141b;
  --head: #0f141b; --line: #232c38;
  /* --faint carries 12px secondary text (pid, footer, event timestamps), so it
     is picked for a 4.5:1 contrast on both the page and the panel, not for
     looking quiet. */
  --text: #e6edf3; --dim: #93a1b1; --faint: #7b8794;
  --mono: ui-monospace, SFMono-Regular, Consolas, monospace;
}
/* The page used to declare ``dark light`` and then hard-code a dark palette;
   it keeps its word now. */
@media (prefers-color-scheme: light) {
  :root {
    color-scheme: light;
    --ok: #1a7f37; --ok-bg: #dafbe1; --ok-line: #a2e2b6;
    --bad: #cf222e; --bad-bg: #ffebe9; --bad-line: #ffc9c4;
    --unk: #9a6700; --unk-bg: #fff8c5; --unk-line: #eedb8b;
    --accent: #0969da;
    --bg: #f6f8fa; --panel: #ffffff; --row: #f3f6f9; --inset: #f6f8fa;
    --head: #eef2f6; --line: #d8dee4;
    --text: #1f2328; --dim: #59636e; --faint: #69707a;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 20px 28px;
  font: 14px/1.55 ui-sans-serif, system-ui, "Segoe UI", "PingFang SC",
        "Microsoft YaHei", sans-serif;
  background: var(--bg); color: var(--text);
  -webkit-font-smoothing: antialiased;
}
/* Sticky: the verdict and the counts are the one thing worth keeping in view
   when the tunnel list is longer than the window. */
header {
  position: sticky; top: 0; z-index: 2;
  display: flex; flex-wrap: wrap; align-items: center; gap: 10px 18px;
  padding: 14px 0 12px; margin-bottom: 12px;
  background: var(--bg); border-bottom: 1px solid var(--line);
}
.brand { display: flex; align-items: center; gap: 9px; }
.brand svg { width: 21px; height: 21px; color: var(--accent); flex: none; }
h1 { font-size: 17px; margin: 0; font-weight: 650; letter-spacing: .01em; }
h1 span { margin-left: 3px; color: var(--faint); font-size: 12px; font-weight: 400; }
h2 { font-size: 15px; margin: 0 0 6px; }
h3 {
  font-size: 11px; margin: 0 0 7px; color: var(--dim); font-weight: 600;
  text-transform: uppercase; letter-spacing: .08em;
}
.summary {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px;
  margin-left: auto; font-size: 13px;
}
.tiles { display: flex; flex-wrap: wrap; gap: 6px; }
.tile {
  display: flex; align-items: baseline; gap: 5px; padding: 3px 9px;
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  color: var(--dim); font-size: 12px;
}
.tile b {
  font-size: 15px; font-weight: 650; color: var(--text);
  font-variant-numeric: tabular-nums;
}
.tile.ok b { color: var(--ok); }
.tile.bad b { color: var(--bad); }
.tile.unknown b { color: var(--unk); }
.meta { color: var(--faint); font-size: 12px; }
.meta b { color: var(--dim); font-weight: 600; font-variant-numeric: tabular-nums; }
.pill {
  display: inline-block; padding: 2px 9px; border-radius: 999px;
  font-size: 12px; font-weight: 600; white-space: nowrap;
}
.pill.ok { background: var(--ok-bg); color: var(--ok); border: 1px solid var(--ok-line); }
.pill.bad { background: var(--bad-bg); color: var(--bad); border: 1px solid var(--bad-line); }
.pill.unknown { background: var(--unk-bg); color: var(--unk); border: 1px solid var(--unk-line); }

/* One tunnel per row. The row *is* a <details><summary>, so the compact view
   and the full detail are one element and one click. */
.board {
  background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  overflow: hidden; box-shadow: 0 1px 2px rgba(0, 0, 0, .14);
}
.grid {
  display: grid; align-items: start; gap: 4px 16px;
  grid-template-columns:
    minmax(210px, 1.4fr) minmax(150px, 1fr) minmax(190px, 1.3fr) 1.6em;
}
/* 17px = the rows' 14px padding plus their 3px state bar, so the column
   headings sit exactly above the values they name. */
.head {
  padding: 8px 14px 8px 17px; font-size: 11px; font-weight: 600;
  letter-spacing: .08em; text-transform: uppercase; color: var(--faint);
  background: var(--head); border-bottom: 1px solid var(--line);
}
details.tunnel { border-bottom: 1px solid var(--line); }
details.tunnel:last-child { border-bottom: 0; }
details.tunnel > summary {
  list-style: none; cursor: pointer; padding: 10px 14px;
  border-left: 3px solid var(--faint); transition: background .12s ease;
}
details.tunnel > summary::-webkit-details-marker { display: none; }
details.tunnel > summary:hover { background: var(--row); }
details.tunnel[open] > summary { background: var(--row); }
details.tunnel.ok > summary { border-left-color: var(--ok-line); }
details.tunnel.bad > summary { border-left-color: var(--bad-line); }
details.tunnel.unknown > summary { border-left-color: var(--unk-line); }
/* Painted by the inline script on a row whose verdict just changed, so a tunnel
   that died between two refreshes is noticed instead of being read as "it was
   always like that". */
@keyframes flash { from { background: var(--unk-bg); } to { background: transparent; } }
details.tunnel.changed > summary { animation: flash 2.4s ease-out 1; }
.ident { min-width: 0; }
.title { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.name { font-weight: 600; }
.target, .via {
  font-family: var(--mono); font-size: 12.5px; overflow-wrap: anywhere;
}
.target { color: var(--dim); }
/* Amber on purpose: the bastion is the link that fails first and the one the
   rest of the config cannot even name. */
.via { color: var(--unk); }
.chips { display: flex; flex-wrap: wrap; gap: 5px; }
.chip {
  display: inline-flex; align-items: baseline; gap: 5px; padding: 2px 8px;
  border-radius: 7px; font-size: 12px; font-family: var(--mono);
  border: 1px solid var(--line); background: var(--row); color: var(--dim);
  white-space: nowrap; font-variant-numeric: tabular-nums;
}
.chip .k { opacity: .72; }
.chip.ok { color: var(--ok); border-color: var(--ok-line); background: var(--ok-bg); }
.chip.bad { color: var(--bad); border-color: var(--bad-line); background: var(--bad-bg); }
.chip.unknown { color: var(--unk); border-color: var(--unk-line); background: var(--unk-bg); }
.facts {
  color: var(--dim); font-size: 12.5px; min-width: 0;
  font-variant-numeric: tabular-nums;
}
.facts div { overflow-wrap: anywhere; }
.hi { color: var(--bad); }
.warn { color: var(--unk); }
/* Availability at a glance: half of "99% up" is the shape, not the digits.
   Tabular digits above also keep a number from reflowing as it changes. */
.bar {
  display: inline-block; width: 40px; height: 6px; margin-left: 6px;
  border-radius: 3px; background: var(--line); overflow: hidden;
  vertical-align: middle;
}
.bar i { display: block; height: 100%; background: var(--ok); }
.bar.warn i { background: var(--unk); }
.bar.bad i { background: var(--bad); }
.caret { color: var(--faint); text-align: right; user-select: none; }
/* Literal glyphs, not CSS escapes: this template is an ordinary Python
   string, where a "\25xx" escape is read as an *octal* escape by Python and
   never reaches the browser as a character reference. */
.caret::after { content: "▾"; }
details.tunnel[open] .caret { color: var(--text); }
details.tunnel[open] .caret::after { content: "▴"; }
.panel {
  display: grid; gap: 14px 26px; padding: 12px 14px 16px 17px;
  background: var(--inset); border-left: 3px solid var(--line);
  border-top: 1px solid var(--line);
  grid-template-columns: minmax(250px, 1fr) minmax(250px, 1fr);
}
.panel > div { min-width: 0; }
table.kv { width: 100%; border-collapse: collapse; }
table.kv th {
  text-align: left; font-weight: 500; color: var(--dim); padding: 2px 10px 2px 0;
  white-space: nowrap; vertical-align: top;
}
table.kv td {
  padding: 2px 0; word-break: break-word; font-variant-numeric: tabular-nums;
}
.feed { font-family: var(--mono); font-size: 12px; max-height: 190px; overflow-y: auto; }
.feed .ev { display: flex; gap: 8px; padding: 1px 4px; border-radius: 4px; }
.feed .ev:hover { background: var(--row); }
.feed .t { color: var(--faint); font-variant-numeric: tabular-nums; }
.feed .i { width: 1em; text-align: center; flex: none; }
.i.ok { color: var(--ok); } .i.bad { color: var(--bad); }
.i.warn { color: var(--unk); } .i.dim { color: var(--faint); }
.note { padding: 18px; color: var(--dim); }
code {
  font-family: var(--mono); background: var(--inset);
  border: 1px solid var(--line); padding: 1px 5px; border-radius: 5px;
}
footer {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px;
  margin-top: 14px; color: var(--faint); font-size: 12px;
}
footer .links { margin-left: auto; display: flex; flex-wrap: wrap; gap: 10px; }
footer a { color: var(--accent); text-decoration: none; }
footer a:hover { text-decoration: underline; }
/* The refresh state, told rather than assumed: a page still showing a reading
   from five minutes ago is worse than one that admits it stopped. */
.live { display: inline-flex; align-items: center; gap: 6px; }
.live::before {
  content: ""; width: 7px; height: 7px; border-radius: 50%;
  background: var(--faint);
}
.live.ok::before { background: var(--ok); }
.live.bad { color: var(--bad); }
.live.bad::before { background: var(--bad); animation: pulse 1.4s ease-in-out infinite; }
@keyframes pulse { 50% { opacity: .3; } }
button.pause {
  font: inherit; color: var(--dim); background: var(--panel);
  border: 1px solid var(--line); border-radius: 7px; padding: 2px 9px;
  cursor: pointer;
}
button.pause:hover { color: var(--text); border-color: var(--accent); }
.muted { color: var(--faint); }
/* Narrow screens: stack the row instead of scrolling sideways. */
@media (max-width: 760px) {
  .head { display: none; }
  .grid { grid-template-columns: 1fr; gap: 6px; }
  .caret { text-align: left; }
  .panel { grid-template-columns: 1fr; }
  header { position: static; }
  .summary { margin-left: 0; }
}
@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
</style>
</head>
<body>
<header>
  <div class="brand">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
         stroke-linecap="round" aria-hidden="true">
      <path d="M2 18h20M5 18V9M19 18V9M2 18C2 9.4 6.6 5.5 12 5.5S22 9.4 22 18"/>
      <path d="M5 9.5h14"/>
    </svg>
    <h1>ponte <span>v$version</span></h1>
  </div>
  <div class="summary" id="summary">$summary</div>
</header>
<main id="board">$board</main>
<footer>
  <span class="live" id="live">每 $refresh 秒自动刷新（无脚本时整页刷新）</span>
  <button class="pause" id="pauser" type="button" hidden>暂停</button>$demo_note
  <span class="links">数据来自 ponte status 的同一份状态，点任意一行看明细 ·
  <a href="/status.json">status.json</a> ·
  <a href="/metrics">metrics</a> ·
  <a href="/healthz">healthz</a></span>
</footer>
<script>
/* Progressive enhancement, in one place and one direction: everything above is
   already complete, and this only upgrades the refresh. Without a script the
   <noscript> meta reloads the whole page; with one, the same server-rendered
   markup is fetched and swapped in place, so expanded rows stay expanded and the
   scroll position does not jump.

   Re-using the server's own HTML rather than re-rendering here from the JSON is
   deliberate: a second renderer is exactly how a dashboard starts disagreeing
   with `ponte status`. */
(function () {
  var board = document.getElementById('board');
  var summary = document.getElementById('summary');
  var live = document.getElementById('live');
  var pauser = document.getElementById('pauser');
  if (!board || !summary || !live) { return; }
  var every = $refresh * 1000;
  var timer = null;
  var failures = 0;
  var paused = false;

  function clock() {
    function pad(value) { return (value < 10 ? '0' : '') + value; }
    var now = new Date();
    return pad(now.getHours()) + ':' + pad(now.getMinutes()) + ':' + pad(now.getSeconds());
  }

  function say(tone, text) {
    live.className = 'live' + (tone ? ' ' + tone : '');
    live.textContent = text;
  }

  /* Open rows and their verdicts, read before the swap so both survive it. */
  function snapshot() {
    var open = {}, tones = {}, rows = board.querySelectorAll('details.tunnel');
    for (var i = 0; i < rows.length; i++) {
      var name = rows[i].getAttribute('data-profile');
      if (rows[i].open) { open[name] = 1; }
      tones[name] = rows[i].getAttribute('data-tone');
    }
    return { open: open, tones: tones };
  }

  function restore(before) {
    var rows = board.querySelectorAll('details.tunnel');
    for (var i = 0; i < rows.length; i++) {
      var name = rows[i].getAttribute('data-profile');
      if (before.open[name]) { rows[i].open = true; }
      var tone = rows[i].getAttribute('data-tone');
      if (before.tones[name] && before.tones[name] !== tone) {
        rows[i].className += ' changed';
      }
    }
  }

  function stop() {
    if (timer !== null) { window.clearInterval(timer); timer = null; }
  }

  function halt() {
    paused = true;
    stop();
    if (pauser) { pauser.hidden = false; pauser.textContent = '继续'; }
  }

  function resume() {
    if (timer === null) { timer = window.setInterval(refresh, every); }
  }

  function refresh() {
    fetch(window.location.pathname + window.location.search, {cache: 'no-store'})
      .then(function (response) {
        if (response.ok) { return response.text(); }
        if (response.status === 401 || response.status === 403) { throw 'auth'; }
        throw 'http';
      })
      .then(function (text) {
        var next = new DOMParser().parseFromString(text, 'text/html');
        var nextBoard = next.getElementById('board');
        var nextSummary = next.getElementById('summary');
        if (!nextBoard || !nextSummary) { throw 'shape'; }
        var before = snapshot();
        board.innerHTML = nextBoard.innerHTML;
        summary.innerHTML = nextSummary.innerHTML;
        restore(before);
        failures = 0;
        say('ok', '已更新 ' + clock() + ' · 每 $refresh 秒');
      })
      .catch(function (why) {
        if (why === 'auth') {
          halt();
          say('bad', '缺少令牌，无法自动刷新：在地址里带上 ?token=… 或手动刷新');
          return;
        }
        failures += 1;
        say('bad', '连接中断（第 ' + failures + ' 次），仍在重试');
      });
  }

  if (pauser) {
    pauser.hidden = false;
    pauser.addEventListener('click', function () {
      if (paused) {
        paused = false;
        pauser.textContent = '暂停';
        resume();
        refresh();
      } else {
        halt();
        say('', '已暂停自动刷新，点“继续”恢复');
      }
    });
  }
  /* No point polling a tab nobody is looking at. */
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { stop(); }
    else if (!paused) { refresh(); resume(); }
  });
  resume();
})();
</script>
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


def _tone(section: Mapping[str, Any]) -> str:
    """The one verdict of a profile: ``ok`` / ``bad`` / ``unknown``.

    The pill, the row's left bar and the header count all read it, so they can
    never disagree about which tunnels are in trouble.
    """
    healthy = section.get("healthy")
    if healthy is True:
        return "ok"
    if healthy is False and not _inconclusive(section):
        return "bad"
    return "unknown"


def _health_pill(section: Mapping[str, Any]) -> str:
    """The health pill of one profile."""
    tone = _tone(section)
    return _pill({"ok": "健康", "bad": "异常", "unknown": "未知"}[tone], tone)


def _kv(label: str, value: str) -> str:
    """One table row. *value* must already be escaped or be trusted markup."""
    return f"<tr><th>{_esc(label)}</th><td>{value}</td></tr>"


def _row(label: str, value: Any) -> str:
    """One table row from a plain (untrusted) value."""
    return _kv(label, _esc(value))


#: The port groups a status section reports, and how the row labels them.
#: ``-R`` ports live on the server, ``-L``/``-D`` ports on this machine.
_PORT_GROUPS = (("remote_ports", "远程", "远程端口"), ("local_ports", "本地", "本地端口"))


def _bar(ratio: float) -> str:
    """A small availability bar: the same news as the digits, in one glance.

    Thresholds are about what the number means for a tunnel that is supposed to
    stay up, not about a uniform scale: a single reconnect in a day is worth
    seeing, and half a day down is not the same colour as that.
    """
    width = max(0.0, min(1.0, ratio)) * 100
    tone = "ok" if ratio >= 0.995 else ("warn" if ratio >= 0.95 else "bad")
    return f'<span class="bar {tone}"><i style="width:{width:.1f}%"></i></span>'


def _tile(count: int, label: str, tone: str) -> str:
    """One header count (``tone`` is ``ok``/``unknown``/``bad``/empty)."""
    return f'<div class="tile {tone}"><b>{count}</b><span>{_esc(label)}</span></div>'


def _port_chips(section: Mapping[str, Any]) -> str:
    """The forwarded ports of one tunnel, as chips in the row itself.

    Every chip carries a glyph as well as a colour, because "which of these
    ports is down" is the question the page exists to answer and colour alone
    is not an answer for everyone reading it.

    Unobserved is not the same as closed: when the probe could not run the
    status file holds no port entries at all, so an empty row says
    "未观测" rather than inventing a red "everything is down".
    """
    chips: list[str] = []
    for key, short, _long in _PORT_GROUPS:
        ports = section.get(key)
        if not isinstance(ports, Mapping):
            continue
        for port, listening in sorted(ports.items(), key=_port_order):
            tone, glyph = ("ok", "\u2713") if listening else ("bad", "\u2717")
            chips.append(
                f'<span class="chip {tone}">{glyph}'
                f'<span class="k">{short}</span>{_esc(port)}</span>'
            )
        if key == "remote_ports" and not ports and section.get("probe_error"):
            # Only the server-side group can go unobserved: a failed probe
            # connection hides every ``-R`` port at once, while the ``-L``/``-D``
            # listeners are probed in-process and reported whatever happens.
            chips.append(
                '<span class="chip unknown">?'
                f'<span class="k">{short}</span>未观测</span>'
            )
    if chips:
        return f'<div class="chips">{"".join(chips)}</div>'
    if _inconclusive(section):
        return '<div class="chips"><span class="chip unknown">端口未观测</span></div>'
    return '<span class="muted">—</span>'


def _port_rows(section: Mapping[str, Any]) -> str:
    """The same ports spelled out in words, for the expanded detail."""
    rows: list[str] = []
    for key, _short, label in _PORT_GROUPS:
        ports = section.get(key)
        if not isinstance(ports, Mapping):
            continue
        if not ports:
            if key == "remote_ports" and section.get("probe_error"):
                rows.append(_kv(label, _pill("未观测", "unknown")))
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


def _demo_note(payload: Mapping[str, Any]) -> str:
    """Footer line for ``ponte serve --demo``: says where the data came from, in words."""
    if not payload.get("demo"):
        return ""
    return (
        '\n  <span class="muted">演示模式：内置示例数据（example.com），不是你的隧道；'
        "看自己的状态请用 <code>ponte status</code> / <code>ponte serve</code></span>"
    )


def _identity(name: str, section: Mapping[str, Any]) -> str:
    """Who this tunnel is: its name, verdict, destination and jump chain."""
    parts = [
        f'<div class="title"><span class="name">{_esc(name)}</span>'
        f"{_health_pill(section)}</div>"
    ]
    if section.get("destination"):
        parts.append(f'<div class="target">{_esc(section["destination"])}</div>')
    if section.get("jump"):
        parts.append(
            f'<div class="via" title="ssh -J">\u21b3 经 {_esc(section["jump"])}</div>'
        )
    return f'<div class="ident">{"".join(parts)}</div>'


def _facts(section: Mapping[str, Any], *, now: float) -> str:
    """The at-a-glance column: why it is down first, then session and uptime.

    A reason the operator has to expand a row to discover is a reason they will
    miss, so errors sit on the closed row itself.
    """
    lines: list[str] = []
    # A conclusive failure is red; an unanswered probe is amber, because "we
    # could not ask" is not the same news as "we asked and the answer was no".
    for key, tone, label in (
        ("error", "hi", "循环错误"),
        ("health_error", "hi", "检查错误"),
        ("probe_error", "warn", "探测失败"),
    ):
        if section.get(key):
            lines.append(
                f'<div class="{tone}">{_esc(label)}：{_esc(section[key])}</div>'
            )

    started = _as_float(section.get("current_session_at"))
    session = (
        "会话已断开"
        if started is None
        else f"会话 {_esc(_format_duration(now - started))}"
    )
    pieces = [session]
    availability = _as_float(section.get("availability"))
    if availability is not None:
        pieces.append(f"在线率 {availability * 100:.1f}%{_bar(availability)}")
    lines.append(f'<div>{" · ".join(pieces)}</div>')

    at = _as_float(section.get("last_disconnect_at"))
    if section.get("last_disconnect_reason") and at is not None:
        lines.append(
            f'<div>上次断线 {_esc(_format_duration(now - at))}前</div>'
        )
    return "".join(lines)


def _panel(name: str, section: Mapping[str, Any], *, now: float) -> str:
    """The expanded half of a row: every field the daemon recorded, in full.

    Nothing is dropped from the compact row — it is only deferred, so the
    overview stays one screen tall without becoming a summary of a summary.
    """
    rows: list[str] = []

    if section.get("destination"):
        rows.append(_row("目标", section["destination"]))
    if section.get("jump"):
        rows.append(
            _row("跳板机", f'{section["jump"]}（ssh -J，逐跳由 OpenSSH 自己建立）')
        )
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

    if section.get("probe_error"):
        rows.append(_row("探测失败（端口状态未知）", section["probe_error"]))

    if section.get("health_error"):
        rows.append(_row("检查错误", section["health_error"]))
    if section.get("error"):
        rows.append(_row("循环错误", section["error"]))

    return (
        '<div class="panel">'
        f'<div><h3>{_esc(name)} · 明细</h3>'
        f'<table class="kv">{"".join(rows)}</table></div>'
        f'<div><h3>最近事件</h3><div class="feed">{_feed(section)}</div></div>'
        "</div>"
    )


def _tunnel(name: str, section: Mapping[str, Any], *, now: float) -> str:
    """One tunnel: a closed row that answers the question, and its detail.

    ``<details>`` rather than a link, so a browser that runs nothing still gets
    the whole page. ``data-profile``/``data-tone`` are what the inline refresh
    uses to put the open rows back and to spot a verdict that changed while
    nobody was looking.
    """
    tone = _tone(section)
    return (
        f'<details class="tunnel {tone}" data-profile="{_esc(name)}"'
        f' data-tone="{tone}">'
        f'<summary class="grid">{_identity(name, section)}'
        f'<div>{_port_chips(section)}</div>'
        f'<div class="facts">{_facts(section, now=now)}</div>'
        f'<div class="caret" title="展开明细"></div></summary>'
        f"{_panel(name, section, now=now)}"
        "</details>"
    )


def _summary(payload: Mapping[str, Any], profiles: Mapping[str, Any]) -> str:
    """The header, with a leading marker when this payload is ``ponte serve --demo`` data.

    The marker comes first because it qualifies *everything* after it: a screenshot of
    this page must never be readable as somebody's real infrastructure. It rides on the
    payload (``demo: true``) rather than a server flag, so ``/status.json`` says the same
    thing and the in-place refresh cannot "lose" the marker by re-rendering.
    """
    text = _summary_body(payload, profiles)
    if payload.get("demo"):
        return _pill("演示数据", "unknown") + text
    return text


def _summary_body(payload: Mapping[str, Any], profiles: Mapping[str, Any]) -> str:
    """The verdict, one tile per state that occurs, then daemon facts.

    The counts are tiles rather than a sentence because "two of eleven" is read
    from the page's shape long before any of the words are: a zero-count tile is
    not rendered at all, so what is on screen is what needs attention.
    """
    if not payload.get("running"):
        return _pill("守护进程未运行", "bad") + '<span class="muted">可执行 ponte start 启动</span>'
    ok = [name for name, section in profiles.items() if section.get("healthy") is True]
    broken = [
        name
        for name, section in profiles.items()
        if section.get("healthy") is False and not _inconclusive(section)
    ]
    unknown = [name for name in profiles if name not in ok and name not in broken]
    total = len(profiles)

    if broken:
        verdict = _pill(f"{len(broken)}/{total} 条隧道异常", "bad")
        if unknown:
            verdict += _pill(f"{len(unknown)} 条状态未知", "unknown")
    elif unknown and any(_inconclusive(profiles[name]) for name in unknown):
        verdict = _pill(f"{len(unknown)}/{total} 条隧道状态未知", "unknown")
    elif ok and len(ok) == total:
        verdict = _pill("全部健康", "ok")
    else:
        verdict = _pill("等待首次检查", "unknown")

    tiles = "".join(
        _tile(count, label, tone)
        for count, label, tone in (
            (len(ok), "健康", "ok"),
            (len(unknown), "未知", "unknown"),
            (len(broken), "异常", "bad"),
            (total, "隧道", ""),
        )
        if count
    )
    pieces = [verdict, f'<div class="tiles">{tiles}</div>' if tiles else ""]
    if payload.get("pid") is not None:
        pieces.append(f'<span class="meta">pid <b>{_esc(payload["pid"])}</b></span>')
    uptime = _as_float(payload.get("uptime_seconds"))
    if uptime is not None:
        pieces.append(
            f'<span class="meta">已运行 <b>{_esc(_format_duration(uptime))}</b></span>'
        )
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
        board = (
            '<section class="board"><div class="note"><h2>守护进程未运行</h2>'
            "先执行 <code>ponte start</code> 启动隧道，本页面会在下一轮自动刷新。"
            "</div></section>"
        )
    elif not profiles:
        board = (
            '<section class="board"><div class="note"><h2>尚无隧道上报</h2>'
            "守护进程已启动，等待第一次健康检查。</div></section>"
        )
    else:
        board = (
            '<section class="board">'
            '<div class="grid head"><div>隧道</div><div>端口</div>'
            "<div>状态</div><div></div></div>"
            + "".join(
                _tunnel(name, section, now=moment)
                for name, section in profiles.items()
            )
            + "</section>"
        )
    return _PAGE.substitute(
        refresh=max(1, int(refresh)),
        version=_esc(__version__),
        title="隧道看板",
        summary=_summary(payload, profiles),
        board=board,
        demo_note=_demo_note(payload),
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


#: Longest log line ponte writes for one request. The request line arrives
#: straight off the socket, so its length is the client's choice — http.server
#: will read up to 64 KiB of it, and a log line that long is a flood.
_LOG_TEXT_LIMIT = 500


def _sanitize_log(text: str) -> str:
    """Neutralise characters that could forge a log line, and cap the length.

    ``BaseHTTPRequestHandler`` hands us the request line as it came off the
    socket (decoded as latin-1, so every byte above 0x7f becomes a character),
    and :meth:`PonteRequestHandler.log_message` funnels ``log_error`` here too.
    That text can therefore carry ESC sequences, NUL, DEL, C1 bytes or bidi
    overrides — enough to make a log file claim something it never saw, or a
    terminal render something it never received. ``str.isprintable`` is ``False``
    for exactly those classes (``Cc``/``Cf``/``Zl``/``Zp``) while keeping
    ordinary spaces, so each offender becomes a visible ``?`` instead of an
    invisible instruction. The cap keeps a 64 KiB request line from becoming a
    64 KiB log line.
    """
    cleaned = "".join(char if char.isprintable() else "?" for char in text)
    if len(cleaned) <= _LOG_TEXT_LIMIT:
        return cleaned
    return cleaned[:_LOG_TEXT_LIMIT] + "…"


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
        """Route the stdlib's stderr chatter into the ponte logger.

        Everything logged for a request — including the raw request line, and
        whatever :meth:`log_error` passes on — goes through :func:`_sanitize_log`
        first, because none of it is ours.
        """
        logger.debug("%s %s", self.address_string(), _sanitize_log(format % args))

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
