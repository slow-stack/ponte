"""Tests for ``ponte serve`` — the HTTP dashboard, health probe and metrics.

The renderers are pure functions over a ``ponte status --json`` payload, so most
of this file needs no sockets. The end-to-end section starts a *real* server on
an OS-assigned port and speaks real HTTP to it, because the parts worth being
sure about — routing, status codes, the token gate, the absence of caching —
only exist once bytes go over a socket.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from ponte import __version__
from ponte.config import ConfigValidationError, ensure_bindable
from ponte.serve import (
    create_server,
    dashboard_html,
    health_response,
    render_metrics,
    serve_url,
)

_NOW = 1_700_000_000.0


def _profile(**overrides) -> dict:
    """One profile section, shaped exactly like ``ponte status --json``."""
    section = {
        "destination": "deploy@example.com:22",
        "healthy": True,
        "process_alive": True,
        "health_error": None,
        "error": None,
        "remote_ports": {"23334": True},
        "local_ports": {"1080": False},
        "connect_attempts_total": 4,
        "sessions_total": 3,
        "reconnects_total": 1,
        "tunnel_uptime_seconds": 3600.0,
        "tunnel_downtime_seconds": 60.0,
        "availability": 0.9,
        "current_session_at": _NOW - 120.0,
        "last_disconnect_at": _NOW - 600.0,
        "last_disconnect_reason": "ssh exited with code 255",
        "last_notification_at": None,
        "recent_events": [
            {"at": _NOW - 700.0, "type": "disconnected", "reason": "ssh exited with code 255"},
            {"at": _NOW - 690.0, "type": "retrying", "attempt": 1, "delay": 5.0},
            {"at": _NOW - 120.0, "type": "connected"},
        ],
    }
    section.update(overrides)
    return section


def _payload(**overrides) -> dict:
    """A whole-daemon payload."""
    base = {
        "running": True,
        "pid": 4242,
        "started_at": _NOW - 3600.0,
        "uptime_seconds": 3600.0,
        "healthy": True,
        "profiles": {"web": _profile()},
    }
    base.update(overrides)
    return base


def _parse_families(text: str) -> dict[str, dict]:
    """Parse exposition text, asserting the format's grouping rules holds.

    Returns ``{name: {"type": str, "samples": [line, ...]}}``. The assertions
    inside are the real test: a metric's samples must form one uninterrupted
    group, every family needs exactly one ``# HELP``/``# TYPE``, and a sample
    may never appear before its own header.
    """
    families: dict[str, dict] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("# HELP "):
            name, _, help_text = line[len("# HELP ") :].partition(" ")
            assert name not in families, f"duplicate HELP for {name}"
            assert help_text.strip(), f"empty HELP for {name}"
            families[name] = {"type": None, "samples": [], "help": help_text}
            current = name
        elif line.startswith("# TYPE "):
            name, _, kind = line[len("# TYPE ") :].partition(" ")
            assert families[name]["type"] is None, f"duplicate TYPE for {name}"
            families[name]["type"] = kind
            current = name
        elif line.strip():
            name = line.split("{")[0].split(" ")[0]
            assert name == current, f"sample {name} is not grouped under {current}"
            families[current]["samples"].append(line)
    for name, family in families.items():
        assert family["type"] in ("gauge", "counter"), name
        assert family["samples"], f"{name} has no samples"
    return families


#: ``key="value"`` with backslash escapes honoured, i.e. what a Prometheus
#: parser sees. Values keep their escapes; :func:`_unescape` undoes them.
_LABEL_PATTERN = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')


def _labels(line: str) -> dict[str, str]:
    """Parse the label set of a sample line (``{}`` when it has none)."""
    return {
        match.group(1): match.group(2)
        for match in _LABEL_PATTERN.finditer(line)
    }


def _unescape(value: str) -> str:
    """Undo Prometheus label escaping (``\\n``, ``\\"``, ``\\\\``)."""
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            out.append({"n": "\n", '"': '"', "\\": "\\"}.get(nxt, nxt))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _sample(families: dict[str, dict], name: str, **labels: str) -> str | None:
    """Return the sample of *name* whose labels include *labels*."""
    for line in families[name]["samples"]:
        parsed = _labels(line)
        if all(parsed.get(key) == value for key, value in labels.items()):
            return line
    return None


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


def test_healthz_down_when_daemon_is_not_running() -> None:
    code, body = health_response({"running": False})
    assert code == 503
    assert body["status"] == "down"


def test_healthz_ok_when_every_profile_is_healthy() -> None:
    code, body = health_response(_payload())
    assert code == 200
    assert body == {"status": "ok", "profiles": 1}


def test_healthz_degraded_names_the_broken_profile() -> None:
    """A 503 has to say *which* tunnel broke, or the alert is unactionable."""
    payload = _payload(
        profiles={
            "web": _profile(),
            "db": _profile(healthy=False, health_error="port 23335 is not listening"),
        }
    )
    code, body = health_response(payload)
    assert code == 503
    assert body["status"] == "degraded"
    assert body["unhealthy"] == ["db"]
    assert body["errors"] == {"db": "port 23335 is not listening"}


def test_healthz_starting_is_not_reported_as_down() -> None:
    """No health check yet must not fire a false alert on every restart.

    A fresh ``ponte start`` waits up to ``check_interval`` for its first
    answer; calling that "down" would page someone on every restart.
    """
    code, body = health_response(_payload(profiles={"web": _profile(healthy=None)}))
    assert code == 200
    assert body["status"] == "starting"


def test_healthz_ok_when_no_profile_reported_at_all() -> None:
    code, body = health_response(_payload(profiles={}))
    assert code == 200
    assert body["status"] == "starting"


def test_healthz_survives_a_malformed_payload() -> None:
    """The status file is written by whichever daemon version is installed."""
    code, body = health_response({"running": True, "profiles": "nonsense"})
    assert code == 200
    assert body["status"] == "starting"


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------


def test_metrics_exposes_the_profile_state() -> None:
    families = _parse_families(render_metrics(_payload(), now=_NOW))

    assert _sample(families, "ponte_up") == "ponte_up 1"
    assert _sample(families, "ponte_profiles_configured") == (
        "ponte_profiles_configured 1"
    )
    assert _sample(families, "ponte_profiles_unhealthy") == (
        "ponte_profiles_unhealthy 0"
    )
    assert _sample(families, "ponte_build_info") == (
        f'ponte_build_info{{version="{__version__}"}} 1'
    )
    assert _sample(families, "ponte_profile_healthy", profile="web") == (
        'ponte_profile_healthy{profile="web"} 1'
    )
    assert _sample(families, "ponte_profile_sessions_total", profile="web") == (
        'ponte_profile_sessions_total{profile="web"} 3'
    )
    # Identity travels in one info metric, so a legend can show the server
    # without repeating the string on every sample.
    assert _sample(
        families,
        "ponte_profile_info",
        profile="web",
        destination="deploy@example.com:22",
    ) == (
        'ponte_profile_info{profile="web",destination="deploy@example.com:22"} 1'
    )


def test_metrics_session_age_is_computed_live() -> None:
    families = _parse_families(render_metrics(_payload(), now=_NOW))
    assert _sample(families, "ponte_profile_session_uptime_seconds", profile="web") == (
        'ponte_profile_session_uptime_seconds{profile="web"} 120.000'
    )


def test_metrics_reports_port_listening_state() -> None:
    """The strongest signal of the lot: is the port *actually* forwarded?"""
    families = _parse_families(render_metrics(_payload(), now=_NOW))
    assert _sample(
        families, "ponte_profile_port_listening", profile="web", kind="remote", port="23334"
    ) == (
        'ponte_profile_port_listening{profile="web",kind="remote",port="23334"} 1'
    )
    assert _sample(
        families, "ponte_profile_port_listening", profile="web", kind="local", port="1080"
    ) == 'ponte_profile_port_listening{profile="web",kind="local",port="1080"} 0'


def test_metrics_omits_unknown_values_instead_of_exporting_nan() -> None:
    """A gap in a graph says "no data"; a NaN line invites a guess."""
    payload = _payload(
        profiles={
            "web": _profile(
                healthy=None,
                process_alive=None,
                current_session_at=None,
                availability=None,
                sessions_total=None,
                last_disconnect_at=None,
                last_notification_at=None,
                remote_ports={},
                local_ports={},
            )
        }
    )
    text = render_metrics(payload, now=_NOW)
    for absent in (
        "ponte_profile_healthy",
        "ponte_profile_process_alive",
        "ponte_profile_session_uptime_seconds",
        "ponte_profile_availability_ratio",
        "ponte_profile_sessions_total",
        "ponte_profile_port_listening",
    ):
        assert absent not in text
    assert "ponte_up 1" in text


def test_metrics_when_daemon_is_stopped_still_answers_200_up_zero() -> None:
    """The scrape must keep working while the thing it measures is down."""
    families = _parse_families(render_metrics({"running": False}))
    assert _sample(families, "ponte_up") == "ponte_up 0"
    assert "ponte_profile_sessions_total" not in families


def test_metrics_escapes_label_values() -> None:
    """A quote or newline in a name must not forge or split a label.

    Profile names come from the config and destinations from ``[ssh]``; a real
    newline leaking into a label would break the sample across two lines and
    corrupt the whole scrape, not just that metric.
    """
    name = 'we"b\n'
    destination = 'host"x\\y\nz'
    families = _parse_families(render_metrics(_payload(profiles={name: _profile(destination=destination)}), now=_NOW))

    healthy = _sample(families, "ponte_profile_healthy")
    assert healthy is not None
    assert _unescape(_labels(healthy)["profile"]) == name

    info = _sample(families, "ponte_profile_info")
    assert info is not None
    assert _unescape(_labels(info)["destination"]) == destination


# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------


def test_dashboard_shows_the_tunnel_state() -> None:
    page = dashboard_html(_payload(), refresh=7, now=_NOW)
    assert "web" in page
    assert "deploy@example.com:22" in page
    assert "2m 0s" in page  # current session duration
    assert "90.0%" in page  # availability
    assert "监听中" in page and "未监听" in page
    assert "ssh exited with code 255" in page
    assert '<meta http-equiv="refresh" content="7">' in page


def test_dashboard_escapes_everything_from_outside() -> None:
    """Profile names come from config and reasons from SSH's stderr."""
    payload = _payload(
        profiles={
            "<script>alert(1)</script>": _profile(
                destination='"><img src=x onerror="alert(2)">',
                last_disconnect_reason="<b>sshd</b> said no",
                recent_events=[
                    {"at": _NOW, "type": "disconnected", "reason": "<i>boom</i>"}
                ],
            )
        }
    )
    page = dashboard_html(payload, now=_NOW)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<img src=x onerror=" not in page
    assert "<b>sshd</b>" not in page
    assert "<i>boom</i>" not in page


def test_dashboard_reports_a_stopped_daemon() -> None:
    page = dashboard_html({"running": False}, now=_NOW)
    assert "守护进程未运行" in page
    assert "ponte start" in page


def test_dashboard_waits_quietly_for_the_first_check() -> None:
    page = dashboard_html(_payload(profiles={"web": _profile(healthy=None)}), now=_NOW)
    assert "等待首次检查" in page


# ---------------------------------------------------------------------------
# End to end: a real server, real HTTP
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _running_server(provider, **kwargs) -> Iterator[str]:
    """Start a server on an OS-assigned port and yield its base URL."""
    server = create_server(provider, host="127.0.0.1", port=0, **kwargs)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url: str, *, headers: dict[str, str] | None = None, method: str = "GET"):
    """Return ``(status, headers, body_text)`` for *url* without raising."""
    request = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read().decode("utf-8")


def test_end_to_end_endpoints_answer() -> None:
    with _running_server(lambda: _payload()) as base:
        status, headers, body = _get(base + "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert "web" in body

        status, _, body = _get(base + "/healthz")
        assert status == 200
        assert json.loads(body)["status"] == "ok"

        status, headers, body = _get(base + "/metrics")
        assert status == 200
        assert headers["Content-Type"].startswith("text/plain")
        assert "ponte_up 1" in body

        status, _, body = _get(base + "/status.json")
        assert status == 200
        assert json.loads(body)["profiles"]["web"]["sessions_total"] == 3


def test_end_to_end_healthz_tracks_the_tunnel() -> None:
    """The probe reports the tunnel, not the process: 503 when it breaks."""
    state = {"payload": _payload()}
    with _running_server(lambda: state["payload"]) as base:
        assert _get(base + "/healthz")[0] == 200
        state["payload"] = _payload(
            profiles={"web": _profile(healthy=False, health_error="port closed")}
        )
        status, _, body = _get(base + "/healthz")
        assert status == 503
        assert json.loads(body)["errors"] == {"web": "port closed"}


def test_end_to_end_status_is_never_cached() -> None:
    """A replayed "healthy" page is the exact failure the endpoint prevents."""
    state = {"payload": _payload()}
    with _running_server(lambda: state["payload"]) as base:
        _, headers, _ = _get(base + "/")
        assert headers["Cache-Control"] == "no-store"
        state["payload"] = {"running": False}
        assert "ponte start" in _get(base + "/")[2]


def test_end_to_end_unknown_path_and_method() -> None:
    with _running_server(lambda: _payload()) as base:
        status, _, body = _get(base + "/nope")
        assert status == 404
        assert "/metrics" in json.loads(body)["endpoints"]

        status, headers, body = _get(base + "/", method="POST")
        assert status == 405
        assert headers["Allow"] == "GET, HEAD"
        assert "read-only" in json.loads(body)["error"]


def test_end_to_end_head_sends_headers_without_a_body() -> None:
    with _running_server(lambda: _payload()) as base:
        status, headers, body = _get(base + "/metrics", method="HEAD")
        assert status == 200
        assert int(headers["Content-Length"]) > 0
        assert body == ""


def test_end_to_end_token_gate() -> None:
    """With a token set, nothing is served without it — header or query."""
    with _running_server(lambda: _payload(), token="s3cret") as base:
        for path in _ROUTES_FOR_TOKEN_TEST:
            status, headers, _ = _get(base + path)
            assert status == 401, path
            assert headers["WWW-Authenticate"].startswith("Bearer")

        assert _get(base + "/healthz?token=s3cret")[0] == 200
        assert _get(base + "/", headers={"Authorization": "Bearer s3cret"})[0] == 200
        assert _get(base + "/healthz?token=wrong")[0] == 401


_ROUTES_FOR_TOKEN_TEST = ("/", "/healthz", "/metrics", "/status.json")


def test_end_to_end_provider_failure_answers_500_without_dropping_the_client() -> None:
    def broken() -> dict:
        raise RuntimeError("status file is a mess")

    with _running_server(broken) as base:
        status, _, body = _get(base + "/healthz")
        assert status == 500
        assert "status unavailable" in json.loads(body)["error"]


# ---------------------------------------------------------------------------
# Bind safety and URL rendering
# ---------------------------------------------------------------------------


def test_serve_refuses_to_expose_without_a_token() -> None:
    """Refusing beats warning: the page maps your infrastructure."""
    with pytest.raises(ConfigValidationError) as caught:
        create_server(lambda: _payload(), host="0.0.0.0", port=0)
    assert "token" in str(caught.value)


def test_serve_allows_a_non_loopback_bind_when_a_token_is_set() -> None:
    """The gate is the token, not the address: with one, exposing is allowed.

    Checked through :func:`ensure_bindable` rather than by actually binding a
    public interface — a test has no business opening a port the whole LAN can
    reach (and on Windows that alone can raise a firewall prompt).
    """
    ensure_bindable("0.0.0.0", "s3cret")  # does not raise
    with pytest.raises(ConfigValidationError):
        ensure_bindable("0.0.0.0", "")


def test_create_server_carries_its_settings() -> None:
    server = create_server(
        lambda: _payload(), host="127.0.0.1", port=0, token="s3cret", refresh=9
    )
    try:
        assert server.token == "s3cret"
        assert server.refresh == 9
    finally:
        server.server_close()


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("", "http://127.0.0.1:8787/"),
        ("127.0.0.1", "http://127.0.0.1:8787/"),
        ("0.0.0.0", "http://0.0.0.0:8787/"),
        ("::1", "http://[::1]:8787/"),
        ("192.168.1.5", "http://192.168.1.5:8787/metrics"),
    ],
)
def test_serve_url_formats_the_link(host: str, expected: str) -> None:
    path = "/metrics" if host == "192.168.1.5" else "/"
    assert serve_url(host, 8787, path) == expected
