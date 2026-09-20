"""Daemon / process-lifecycle management for the ponte tunnel manager.

This module orchestrates the three layers below it into a single long-running
process:

* :mod:`ponte.core`   — establish and tear down a single SSH session
* :mod:`ponte.retry`  — exponential-backoff reconnect state machine
* :mod:`ponte.health` — periodic liveness and remote-port checks

``TunnelDaemon.run()`` runs the retry loop in the *foreground* (blocking) so a
Scheduled Task, a service wrapper, or the CLI's background mode can all drive
the exact same loop. On Windows, background mode is implemented by re-spawning
this process detached (``CREATE_DETACHED_PROCESS``) rather than by Unix style
double-forking.

Graceful stop on Windows works via a *stop marker* file: the CLI writes a
marker into the package directory and a daemon-side watcher thread converts it
into a clean ``manager.stop()`` + ``runner.stop()`` shutdown. If the daemon
does not exit within a timeout the CLI escalates to ``taskkill /T /F``.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, cast
from xml.sax.saxutils import escape as xml_escape

from ponte.config import (
    DEFAULT_PROFILE_NAME,
    ConfigError,
    Profile,
    TunnelConfig,
    get_config,
    load_config,
    package_dir,
)
from ponte.core import TunnelManager, creation_flags
from ponte.health import HealthChecker, HealthStatus
from ponte.notify import Notification, Notifier
from ponte.retry import RetryEvent, RetryRunner

__all__ = ["DaemonStatus", "ProfileRunner", "ProfileStatus", "TunnelDaemon"]

logger = logging.getLogger(__name__)

#: Seconds between stop-marker polls on Windows (see ``_watch_stop_marker``).
_STOP_POLL_INTERVAL = 0.5

#: ``GetExitCodeProcess``'s STILL_ACTIVE pseudo exit code.
_STILL_ACTIVE = 259

#: Consecutive unhealthy checks (with the SSH process still alive) before the
#: daemon force-reconnects a "zombie" tunnel — an SSH process that is still
#: running but whose remote forwarding ports have all dropped (network hang,
#: port-stealing race, …). Without this the retry loop would stay blocked in
#: ``manager.connect()`` forever, leaving the tunnel down.
_HEALTH_FAILURE_THRESHOLD = 3

#: How many recent retry-loop events to keep in the status JSON event feed
#: (rendered by ``ponte watch``, surfaced by ``ponte status``).
_EVENT_FEED_LIMIT = 20

#: Keys a profile section of the status file may hold. Only used to migrate a
#: pre-``profiles`` status file, whose single-tunnel state sat at the top level.
_PROFILE_KEYS = frozenset(
    {
        "process_alive",
        "healthy",
        "health_conclusive",
        "probe_error",
        "remote_ports",
        "local_ports",
        "health_error",
        "checked_at",
        "connect_attempts_total",
        "sessions_total",
        "reconnects_total",
        "tunnel_uptime_seconds",
        "tunnel_downtime_seconds",
        "current_session_at",
        "last_disconnect_at",
        "last_disconnect_reason",
        "last_notification_at",
        "recent_events",
        "error",
    }
)


def _disconnect_reason(event: RetryEvent) -> str:
    """Human-readable reason for a ``DISCONNECTED`` event.

    Shared by the statistics bookkeeping and the failure alert so the two can
    never disagree about *why* a tunnel dropped.
    """
    if event.error:
        return str(event.error)
    if event.exit_code is not None:
        return f"ssh exited with code {event.exit_code}"
    return "unknown"

#: Thread join timeout when shutting a profile down (seconds).
_PROFILE_JOIN_TIMEOUT = 10.0

#: Interval (seconds) the daemon's main thread sleeps between liveness sweeps.
_SUPERVISOR_INTERVAL = 0.5


def _derive_status_file(pid_file: str) -> str:
    """Derive the JSON status path from a ``.pid`` file path."""
    base, _ext = os.path.splitext(pid_file)
    return base + ".status.json"


def _format_duration(seconds: float) -> str:
    """Human readable duration, e.g. ``1h 2m 3s``."""
    seconds = int(max(0.0, seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h {minutes}m {secs}s"


def _derive_stop_marker(pid_file: str) -> str:
    """Derive the stop-marker path from a ``.pid`` file path."""
    base, _ext = os.path.splitext(pid_file)
    return base + ".stop"


def _derive_reload_marker(pid_file: str) -> str:
    """Derive the config-reload marker path from a ``.pid`` file path."""
    base, _ext = os.path.splitext(pid_file)
    return base + ".reload"


def _is_package_dir(path: str) -> bool:
    """Return ``True`` when *path* is the installed ``ponte`` package itself."""
    return os.path.normcase(os.path.abspath(path)) == os.path.normcase(
        os.path.abspath(package_dir())
    )


def _spawn_log_path(pid_file: str) -> str:
    """Where a background spawn's first output is captured."""
    return f"{pid_file}.spawn.log"


def _spawn_tail(path: str, limit: int = 600) -> str:
    """Tail of a spawn log for an error message (empty when there is nothing)."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return ""
    text = _decode_console(data).strip()
    if not text:
        return ""
    return f"\n---- child output ({path}) ----\n" + text[-limit:]


def _encode_ps(script: str) -> str:
    """Base64 UTF-16LE encode a PowerShell snippet for ``-EncodedCommand``.

    Using ``-EncodedCommand`` completely sidesteps Windows quoting and encoding
    mangling — the same class of problem that broke the earlier PowerShell
    attempts.
    """
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _decode_console(data: bytes) -> str:
    """Decode bytes from a Windows console process, tolerating GBK/ANSI output.

    PowerShell (and other console tools) on Chinese Windows emit the active
    code page (GBK) rather than UTF-8. Try UTF-8 first, then fall back to
    ``gbk`` with lossy replacement so a mis-decode never raises.
    """
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gbk", errors="replace")


def _windows_kernel32() -> Any:
    """Return the ``kernel32`` handle used by the Windows process probes.

    typeshed declares ``ctypes.windll`` for Windows only, so accessing it
    directly makes ``mypy`` fail with ``attr-defined`` on Linux — which is where
    CI runs the type check. The cast states the actual situation: the attribute
    is always there at runtime, but only the Windows stubs know about it. Both
    callers are Windows-only, so this never runs where ``windll`` is missing.
    """
    import ctypes

    return cast(Any, ctypes).windll.kernel32


def _windows_exit_code(pid: int) -> int | None:
    """Return *pid*'s exit code, or ``None`` when the handle cannot be opened.

    ``None`` means *unknown* — typically ACCESS_DENIED for a process owned by
    another account — and not "dead"; :func:`_windows_pid_alive` decides what to
    do about that. Split out into its own function so the liveness logic is
    testable on any platform: ``ctypes.windll`` only exists on Windows.
    """
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = _windows_kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return int(code.value)
    finally:
        kernel32.CloseHandle(handle)


def _windows_process_listed(pid: int) -> bool | None:
    """Return whether *pid* is in the process list, or ``None`` if unknown.

    A Toolhelp32 snapshot rather than ``tasklist`` on purpose: this runs on the
    ``status`` / ``stop`` path, and spawning a console program there is exactly
    how a harmless status check ends up flashing a black window. Enumeration
    needs no rights at all, which is the point — it works for a process owned by
    another account, where ``OpenProcess`` is refused outright.
    """
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = _windows_kernel32()
    # Without these, a 64-bit snapshot handle is truncated to 32 bits on return.
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32First.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return None
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        found = bool(kernel32.Process32First(snapshot, ctypes.byref(entry)))
        while found:
            if int(entry.th32ProcessID) == int(pid):
                return True
            found = bool(kernel32.Process32Next(snapshot, ctypes.byref(entry)))
        return False
    finally:
        kernel32.CloseHandle(snapshot)


def _windows_pid_alive(pid: int) -> bool:
    """Windows liveness check that survives being run by a lesser account.

    ``OpenProcess`` is the accurate answer — its exit code also proves the pid
    was not recycled — but a standard user cannot open a process owned by
    another account, and that is precisely the setup ponte recommends on
    Windows: the daemon runs as SYSTEM so the tunnel exists before anyone logs
    in, while ``status`` / ``stop`` are typed by the logged-in user. There
    ``OpenProcess`` fails with ACCESS_DENIED, and calling a healthy daemon "not
    running" is a lie with consequences: ``ponte start`` would launch a second
    daemon that fights for the same server-side ports, and ``ponte stop`` would
    refuse to stop the real one.

    So an unopenable pid falls back to the process list, which needs no rights.
    That cannot distinguish a recycled pid from the original — hence the
    fallback, not the primary check.
    """
    code = _windows_exit_code(pid)
    if code is not None:
        return code == _STILL_ACTIVE
    listed = _windows_process_listed(pid)
    if listed is None:
        # No answer at all: assume alive. Failing this way only costs a refused
        # ``start``; failing the other way starts a duplicate daemon.
        return True
    return listed


def _run_tool(
    args: list[str],
    *,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an external control tool (``taskkill`` / ``systemctl`` / ``launchctl``).

    Every child process goes through :func:`creation_flags`, so a console window
    can never flash. That matters most for ``taskkill``: the daemon normally runs
    windowless (``pythonw`` / Scheduled Task), so a bare ``subprocess.run`` would
    pop a black box in the user's face the moment they ask ponte to stop.
    """
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
        creationflags=creation_flags(),
    )


@dataclasses.dataclass
class ProfileStatus:
    """Health and cumulative statistics for one profile (one SSH connection).

    This is everything that used to be reported as "the tunnel": with several
    profiles configured, each one gets its own snapshot so a broken server is
    visible next to the healthy ones instead of being averaged away.
    """

    name: str
    #: ``user@host:port`` this profile connects to, taken from the
    #: configuration (not the status file). It is what makes a multi-profile
    #: ``status --json`` / dashboard row self-describing: a table of numbers is
    #: useless if you cannot tell which server is the broken one.
    destination: str | None = None
    #: The jump chain (``ssh -J`` value, e.g. ``ops@bastion:2222``) this profile
    #: reaches :attr:`destination` through, taken from the configuration like
    #: :attr:`destination` is. Kept next to it because a tunnel that only exists
    #: behind a bastion fails for a reason the destination alone cannot name:
    #: ``ssh`` reports a dead hop and a refused login with the same message.
    jump: str | None = None
    #: ``None`` until the first health check of this profile reports in.
    healthy: bool | None = None
    process_alive: bool | None = None
    remote_ports: dict[int, bool] = dataclasses.field(default_factory=dict)
    """``-R`` ports open on the server, ``{port: listening}``."""
    local_ports: dict[int, bool] = dataclasses.field(default_factory=dict)
    """``-L``/``-D`` ports this machine listens on, ``{port: listening}``."""
    health_error: str | None = None
    #: ``False`` when the last check could not be completed (the probe
    #: connection failed), so ``healthy is False`` is *not* a verdict about the
    #: tunnel. ``None`` for a status file written before this field existed.
    health_conclusive: bool | None = None
    #: The failed probe's message, when the remote ports could not be observed.
    probe_error: str | None = None
    #: Set when this profile's retry loop died of an unexpected exception.
    error: str | None = None

    # -- 隧道统计（cumulative; survives daemon restarts via the status file）--
    #
    # ``None`` 表示状态文件里没有这份统计（旧版本 daemon 写的文件）。

    #: Every ``CONNECTING`` — each SSH launch attempt.
    connect_attempts_total: int | None = None
    #: Sessions that were actually established (``CONNECTED`` events).
    sessions_total: int | None = None
    #: Scheduled reconnects (``RETRYING`` events).
    reconnects_total: int | None = None
    #: Accumulated duration of *completed* sessions, seconds.
    tunnel_uptime_seconds: float | None = None
    #: Accumulated wall-clock gaps disconnect → next attempt, seconds.
    tunnel_downtime_seconds: float | None = None
    #: Wall-clock time the current session started (``None`` when disconnected).
    current_session_at: float | None = None
    #: Wall-clock time of the most recent disconnect.
    last_disconnect_at: float | None = None
    #: Human-readable reason of the most recent disconnect.
    last_disconnect_reason: str | None = None
    #: Wall-clock time the most recent failure alert was delivered.
    last_notification_at: float | None = None
    #: Bounded feed of recent retry-loop events (oldest first).
    recent_events: list[dict] = dataclasses.field(default_factory=list)

    @property
    def session_uptime(self) -> str | None:
        """Human-readable duration of the current session, ``None`` when down.

        This is the number that exposes a "daemon alive but tunnel flapping"
        situation: the process uptime looks fine while the session keeps
        resetting to minutes.
        """
        if self.current_session_at is None:
            return None
        return _format_duration(time.time() - self.current_session_at)

    @property
    def availability(self) -> float | None:
        """Fraction of observed time this profile has been up (0..1).

        Based on the *completed* session/downtime bookkeeping; the ongoing
        session's elapsed time is not yet counted, so a freshly reconnected
        tunnel slightly understates availability. ``None`` when there is no
        history at all.
        """
        up = self.tunnel_uptime_seconds or 0.0
        down = self.tunnel_downtime_seconds or 0.0
        total = up + down
        if total <= 0:
            return None
        return up / total


@dataclasses.dataclass
class DaemonStatus:
    """A snapshot of daemon state for the ``status``/``stop`` commands."""

    running: bool
    pid: int | None = None
    started_at: float | None = None
    uptime_seconds: float = 0.0
    #: One entry per configured profile, in configuration order.
    profiles: list[ProfileStatus] = dataclasses.field(default_factory=list)
    message: str = ""

    @property
    def uptime(self) -> str:
        """Human readable uptime, e.g. ``1h 2m 3s``."""
        return _format_duration(self.uptime_seconds)

    @property
    def healthy(self) -> bool | None:
        """Overall health: ``True`` only when *every* profile is healthy.

        ``None`` when no profile has reported yet. Useful for a single boolean
        (the watch panel border) while ``profiles`` carries the detail.
        """
        if not self.profiles:
            return None
        marks = [profile.healthy for profile in self.profiles]
        if all(mark is None for mark in marks):
            return None
        return all(mark is True for mark in marks)

    def get_profile(self, name: str) -> ProfileStatus | None:
        """Return the status of *name*, or ``None`` if it never reported."""
        for profile in self.profiles:
            if profile.name == name:
                return profile
        return None


def _profile_status(
    name: str,
    section: dict,
    *,
    destination: str | None = None,
    jump: str | None = None,
) -> ProfileStatus:
    """Build a :class:`ProfileStatus` from one status-file section.

    Tolerant on purpose: the file is written by whichever daemon version is
    installed, so missing or malformed fields degrade to ``None``/empty rather
    than raising while the user is trying to read their status.
    """

    def _number(key: str) -> float | None:
        value = section.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _count(key: str) -> int | None:
        value = _number(key)
        return None if value is None else int(value)

    def _ports(key: str) -> dict[int, bool]:
        raw = section.get(key)
        ports: dict[int, bool] = {}
        if isinstance(raw, dict):
            for port, is_open in raw.items():
                try:
                    ports[int(port)] = bool(is_open)
                except (TypeError, ValueError):
                    continue
        return ports

    raw_healthy = section.get("healthy")
    raw_alive = section.get("process_alive")
    raw_conclusive = section.get("health_conclusive")
    return ProfileStatus(
        name=name,
        destination=destination,
        jump=jump,
        healthy=raw_healthy if isinstance(raw_healthy, bool) else None,
        process_alive=raw_alive if isinstance(raw_alive, bool) else None,
        remote_ports=_ports("remote_ports"),
        local_ports=_ports("local_ports"),
        health_error=section.get("health_error"),
        health_conclusive=raw_conclusive if isinstance(raw_conclusive, bool) else None,
        probe_error=section.get("probe_error"),
        error=section.get("error"),
        connect_attempts_total=_count("connect_attempts_total"),
        sessions_total=_count("sessions_total"),
        reconnects_total=_count("reconnects_total"),
        tunnel_uptime_seconds=_number("tunnel_uptime_seconds"),
        tunnel_downtime_seconds=_number("tunnel_downtime_seconds"),
        current_session_at=_number("current_session_at"),
        last_disconnect_at=_number("last_disconnect_at"),
        last_disconnect_reason=section.get("last_disconnect_reason"),
        last_notification_at=_number("last_notification_at"),
        recent_events=[
            dict(event)
            for event in section.get("recent_events", [])
            if isinstance(event, dict)
        ],
    )


class _StatusStore:
    """Locked read-modify-write access to the shared JSON status file.

    The payload is ``{"started_at": <daemon start>, "profiles": {name: {...}}}``.
    Every writer — each profile's retry loop and every health loop — goes through
    this class, so two threads can never interleave a read and a write.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    # -- low level ---------------------------------------------------------

    def _read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, payload: dict) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.warning("could not write status file: %s", exc)

    @staticmethod
    def _sections(data: dict) -> dict[str, dict]:
        """Return the per-profile sections of *data*, migrating old payloads.

        Pre-``profiles`` status files kept one tunnel's state at the top level;
        those keys are adopted as the ``default`` profile so an in-place
        upgrade keeps its counters instead of silently starting over.
        """
        sections = data.get("profiles")
        if isinstance(sections, dict):
            return {
                str(name): dict(section)
                for name, section in sections.items()
                if isinstance(section, dict)
            }
        legacy = {key: value for key, value in data.items() if key in _PROFILE_KEYS}
        return {DEFAULT_PROFILE_NAME: legacy} if legacy else {}

    # -- public ------------------------------------------------------------

    def read_profiles(self) -> dict[str, dict]:
        """Return every profile section, migrating a legacy payload on the fly."""
        with self._lock:
            return self._sections(self._read())

    def started_at(self) -> float | None:
        """Wall-clock time the current daemon process started."""
        with self._lock:
            value = self._read().get("started_at")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def begin(self, names: list[str], started_at: float) -> None:
        """Prime the file: one section per profile, counters defaulted to 0.

        Merges rather than overwrites, so the cumulative statistics of a tunnel
        survive a service-manager respawn (which is exactly when the user cares
        about the history).
        """
        with self._lock:
            sections = self._sections(self._read())
            for name in names:
                section = sections.setdefault(name, {})
                for key in (
                    "connect_attempts_total",
                    "sessions_total",
                    "reconnects_total",
                ):
                    section.setdefault(key, 0)
                for key in ("tunnel_uptime_seconds", "tunnel_downtime_seconds"):
                    section.setdefault(key, 0.0)
                section.setdefault("recent_events", [])
            self._write({"started_at": started_at, "profiles": sections})

    def remove(self, names: list[str]) -> None:
        """Drop the sections of *names* — profiles that left the config.

        Used by a reload: the daemon no longer supervises them, so leaving
        their stale health behind would make ``ponte status`` show a tunnel
        that does not exist any more.
        """
        if not names:
            return
        with self._lock:
            data = self._read()
            sections = self._sections(data)
            for name in names:
                sections.pop(name, None)
            self._write(
                {
                    "started_at": data.get("started_at", time.time()),
                    "profiles": sections,
                }
            )

    @contextlib.contextmanager
    def edit(self, name: str) -> Iterator[dict]:
        """Yield one profile's section for mutation, then persist the file.

        The lock is held for the whole read-modify-write, so the body must stay
        short and must not block (no process teardown inside the ``with``).
        """
        with self._lock:
            data = self._read()
            sections = self._sections(data)
            section = sections.setdefault(name, {})
            started_at = data.get("started_at", time.time())
            yield section
            self._write({"started_at": started_at, "profiles": sections})


class ProfileRunner:
    """Keep one profile's SSH connection alive.

    Owns that profile's session manager, reconnect loop and health monitor, and
    reports every event to the daemon's status file. The daemon builds one per
    configured profile and runs them concurrently, so a profile that cannot
    reach its server backs off on its own without disturbing the others.

    ``manager`` and ``retry_runner`` can be injected for tests; production goes
    through :meth:`TunnelDaemon.run`.
    """

    def __init__(
        self,
        profile: Profile,
        config: TunnelConfig,
        daemon: TunnelDaemon,
        *,
        manager: TunnelManager | None = None,
        retry_runner: RetryRunner | None = None,
    ) -> None:
        self.profile = profile
        self.config = config
        self.daemon = daemon
        self.manager = manager if manager is not None else TunnelManager(config, profile)
        self.retry = (
            retry_runner if retry_runner is not None else RetryRunner(config.retry)
        )
        self.health = HealthChecker(self.manager, config.health)
        self.notifier = daemon.notifier
        self.health_stop: threading.Event | None = None
        self.thread: threading.Thread | None = None
        #: Message of an unexpected exception that killed this profile's loop.
        self.error: str | None = None
        # Consecutive failed attempts, and whether this outage was already
        # reported (see ``_note_disconnect``).
        self._failures = 0
        self._alerted = False

    # -- Lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the health monitor and the reconnect loop on their own threads."""
        self.health_stop = self.health.run_loop(
            interval=self.config.health.check_interval,
            callback=self._on_health,
            name=f"ponte-health-{self.profile.name}",
        )
        self.thread = threading.Thread(
            target=self._run, name=f"ponte-{self.profile.name}", daemon=True
        )
        self.thread.start()

    def abort(self) -> None:
        """Ask the loops and the SSH session to stop. Safe from any thread.

        Deliberately does not join: it runs in the signal handler and the
        stop-marker watcher, where blocking would freeze the shutdown path.
        """
        if self.health_stop is not None:
            self.health_stop.set()
        self.retry.stop()  # abort any backoff sleep
        self.manager.stop()  # abort a blocked connect(), if any

    def finish(self) -> None:
        """:meth:`abort` plus wait for this profile's loop thread to end."""
        self.abort()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=_PROFILE_JOIN_TIMEOUT)

    def is_alive(self) -> bool:
        """``True`` while this profile's reconnect loop is still running."""
        return self.thread is not None and self.thread.is_alive()

    # -- Internals ---------------------------------------------------------

    def _run(self) -> None:
        """Drain this profile's retry generator until stopped."""
        log = logging.getLogger("ponte.daemon")
        name = self.profile.name
        try:
            for event in self.retry.run(self.manager):
                self.daemon._record_retry_event(event, self.manager, name)
                if event.type == RetryEvent.CONNECTING:
                    log.debug("[%s] connecting to %s …", name, self.profile.destination)
                elif event.type == RetryEvent.CONNECTED:
                    # Only means "an SSH session started": whether it stays up
                    # is known after it exits (DISCONNECTED carries the code).
                    log.info("[%s] SSH session started", name)
                elif event.type == RetryEvent.DISCONNECTED:
                    log.warning(
                        "[%s] tunnel down (exit=%s error=%s)",
                        name,
                        event.exit_code,
                        event.error,
                    )
                    self._note_disconnect(_disconnect_reason(event))
                elif event.type == RetryEvent.RETRYING:
                    log.warning(
                        "[%s] reconnecting attempt %d in %.1fs",
                        name,
                        event.attempt,
                        event.delay,
                    )
                elif event.type == RetryEvent.MAX_RETRIES_REACHED:
                    log.error("[%s] retry budget exhausted; giving up", name)
                if self.daemon._shutdown.is_set():
                    self.manager.stop()
        except Exception as exc:  # noqa: BLE001 - one profile must not kill the rest
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("[%s] retry loop died", name)
            self.daemon._record_profile_error(name, self.error)
        finally:
            self.retry.stop()
            self.manager.stop()

    def _on_health(self, status: HealthStatus) -> None:
        """Persist this profile's health and force-reconnect a zombie session."""
        self.daemon._on_health(status, self.manager, self.profile.name)

    def _note_disconnect(self, reason: str) -> None:
        """Count consecutive failures and alert once past the threshold.

        A session that stayed up for ``[retry] stable_after`` seconds counts as
        recovery — the same rule the reconnect budget uses — and re-arms the
        alert so the *next* outage is reported again.

        At most one alert per outage: whether the message was delivered,
        suppressed by the cooldown, or failed to send, ``_alerted`` stays set
        until a stable session, so a flapping tunnel cannot turn into a flood
        of HTTP requests (one per reconnect attempt).
        """
        duration = getattr(self.manager, "last_session_duration", None)
        if duration is not None and duration >= self.config.retry.stable_after:
            self._failures = 0
            self._alerted = False
            return

        self._failures += 1
        threshold = self.config.notify.on_consecutive_failures
        if self._alerted or self._failures < threshold or not self.notifier.enabled:
            return
        self._alerted = True
        delivered = self.notifier.notify(
            Notification(
                profile=self.profile.name,
                failures=self._failures,
                reason=reason,
                destination=self.profile.destination,
            )
        )
        if delivered:
            self.daemon._record_notification(self.profile.name)


class TunnelDaemon:
    """Run and manage one persistent SSH connection per configured profile.

    A single daemon supervises every profile: each gets a
    :class:`ProfileRunner` (its own SSH session, reconnect loop and health
    monitor) while this class owns the process-level concerns — PID file, stop
    marker, JSON status file and service registration. One profile failing to
    reach its server therefore leaves the others alone.

    The daemon is intentionally *stateless on disk* beyond that: everything it
    needs lives in ``config.toml``, and the only mutable artifacts are the PID
    file, the status file and the stop marker.
    """

    def __init__(self, config: TunnelConfig | None = None) -> None:
        self.config = config if config is not None else get_config()
        self.pid_file = self.config.daemon.pid_file
        self.log_file = self.config.daemon.log_file
        self.status_file = _derive_status_file(self.pid_file)
        self.stop_marker = _derive_stop_marker(self.pid_file)
        self.reload_marker = _derive_reload_marker(self.pid_file)
        self._store = _StatusStore(self.status_file)
        self._shutdown = threading.Event()
        #: Serializes config reloads; also the "one reload at a time" guard.
        self._reload_lock = threading.Lock()
        #: Set while ``_reconcile`` swaps runners, so the supervisor does not
        #: mistake the swap for "every profile died" and shut the daemon down.
        self._reconciling = threading.Event()
        #: Shared by every profile: the channels and the rate limit are policy,
        #: not per-connection state.
        self.notifier = Notifier(self.config.notify)
        # Per-profile in-memory bookkeeping, keyed by profile name.
        self._last_health: dict[str, HealthStatus] = {}
        # Wall-clock time of the most recent DISCONNECTED of a profile whose
        # downtime has not yet been closed by a new connection attempt.
        self._pending_disconnect_at: dict[str, float | None] = {}
        # Consecutive unhealthy health checks per profile, used to detect a
        # "zombie" SSH process and force a reconnect (see
        # ``_HEALTH_FAILURE_THRESHOLD``).
        self._health_failures: dict[str, int] = {}
        # Populated by run(); exposed for diagnostics and tests.
        self._runners: list[ProfileRunner] = []

    # -- Paths -----------------------------------------------------------------

    @property
    def work_dir(self) -> str:
        """Directory the spawned daemon / generated service runs in.

        The directory holding the active config file, falling back to the
        user's home. It used to be the package's *parent* directory, which is
        wrong once the package is installed: ``site-packages`` is not a
        meaningful working directory and may not even be writable.

        One case has to stay the parent, though: when the config file *is* the
        one inside the package (the pre-0.3 layout, still supported). The daemon
        and the generated service are started as ``python -m ponte.main``, and
        from inside the package that import cannot resolve — Python finds the
        package's parent on ``sys.path``, not the package itself. The child died
        instantly, so ``ponte start`` reported only "daemon did not write its
        PID file within 10 s" and ``ponte install`` would register a task that
        can never start.
        """
        source = self.config.source_path
        if source:
            directory = os.path.dirname(os.path.abspath(source))
            if os.path.isdir(directory):
                if not _is_package_dir(directory):
                    return directory
                # Legacy in-package config: the package's parent is where
                # ``python -m ponte.main`` resolves from, and it is writable.
                return os.path.dirname(os.path.abspath(package_dir()))
        return os.path.expanduser("~")

    # -- PID helpers -----------------------------------------------------------

    def write_pid(self) -> None:
        os.makedirs(os.path.dirname(self.pid_file), exist_ok=True)
        with open(self.pid_file, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))

    def read_pid(self) -> int | None:
        try:
            with open(self.pid_file, encoding="utf-8") as handle:
                return int(handle.read().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Return ``True`` if *pid* names a live process on this OS."""
        if sys.platform == "win32":
            return _windows_pid_alive(pid)
        # POSIX: signal 0 just probes for existence.
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    # -- Status JSON -----------------------------------------------------------

    @property
    def profile_names(self) -> list[str]:
        """Names of the configured profiles, in configuration order."""
        return self.config.profile_names

    # -- Health callback -------------------------------------------------------

    def _on_health(
        self,
        status: HealthStatus,
        manager: TunnelManager | None = None,
        profile: str = DEFAULT_PROFILE_NAME,
    ) -> None:
        """Store *profile*'s latest health snapshot and mirror it to the file.

        Also tracks *consecutive* unhealthy checks for that profile. When its
        SSH process is still alive but health has failed for
        ``_HEALTH_FAILURE_THRESHOLD`` checks in a row, the process is presumed
        to be a "zombie" (alive, yet its forwarding ports have all dropped —
        e.g. after a network hang or a port-stealing race). In that case
        ``manager.stop()`` is called to kill the session, which makes the retry
        loop's blocking ``connect()`` return so the tunnel is re-established
        instead of being left down forever.

        Only *conclusive* unhealthy checks count towards that threshold. A
        check that could not be completed (``status.conclusive`` is False — the
        probe connection itself failed, so nothing was learned about the ports)
        is neither a failure nor a recovery: it leaves the counter untouched and
        can never trigger a forced reconnect. Otherwise a path that drops
        probe connections — a shared uplink, provider rate limiting — would
        let the monitor kill a healthy tunnel.

        ``manager`` may be ``None`` (e.g. in unit tests, or before ``run()``),
        in which case the forced-reconnect path is skipped — the health data is
        still persisted and logged as usual.
        """
        self._last_health[profile] = status
        with self._store.edit(profile) as section:
            section.update(
                {
                    "checked_at": time.time(),
                    "process_alive": status.process_alive,
                    "healthy": status.all_healthy,
                    "health_conclusive": status.conclusive,
                    "probe_error": status.remote_probe_error,
                    "remote_ports": {
                        str(p): ok for p, ok in status.remote_ports.items()
                    },
                    "local_ports": {
                        str(p): ok for p, ok in status.local_ports.items()
                    },
                    "health_error": status.error,
                }
            )
        health = logger.warning if not status.all_healthy else logger.debug
        health("health[%s]: %s", profile, status)

        # Healthy check: reset the consecutive-failure counter.
        if status.all_healthy:
            self._health_failures[profile] = 0
            return

        # Inconclusive check: the tunnel's state is unknown, so this is not
        # evidence of a zombie — and killing the session on it would be a
        # self-inflicted outage. Note the counter is deliberately *left alone*
        # rather than reset, so a real zombie is still caught after
        # _HEALTH_FAILURE_THRESHOLD conclusive failures even when inconclusive
        # checks are interleaved.
        if not status.conclusive:
            logger.warning(
                "health[%s]: 状态未知 — 本次检查没有得出结论（%s），"
                "不计入强制重连",
                profile,
                status.remote_probe_error or status.error or "原因未知",
            )
            return

        # Conclusive unhealthy check: count it, and if the SSH process is still
        # alive (i.e. a zombie rather than a cleanly-exited process) force
        # reconnect.
        failures = self._health_failures.get(profile, 0) + 1
        self._health_failures[profile] = failures
        if (
            failures >= _HEALTH_FAILURE_THRESHOLD
            and status.process_alive
            and manager is not None
        ):
            logger.warning(
                "假死，强制重连 [%s]: %d consecutive unhealthy checks while the "
                "SSH process is still alive; stopping session so the retry "
                "loop reconnects",
                profile,
                failures,
            )
            manager.stop()
            # Reset so a persistent zombie re-triggers only after another full
            # run of consecutive failures (avoids repeating every check).
            self._health_failures[profile] = 0

    # -- Retry-loop statistics -------------------------------------------------

    def _record_retry_event(
        self,
        event: RetryEvent,
        manager: TunnelManager,
        profile: str = DEFAULT_PROFILE_NAME,
    ) -> None:
        """Fold a retry-loop event into *profile*'s cumulative statistics.

        Event semantics (see :mod:`ponte.retry`): ``CONNECTING`` fires *before*
        the blocking ``connect()`` (session start), while ``CONNECTED`` and
        ``DISCONNECTED`` both fire *after* the session has ended — so the
        session duration is taken from ``manager.last_session_duration``
        rather than from wall-clock deltas between events.

        The counters are merged into the profile's section of the status JSON
        (read-modify-write under the store's lock) so they survive daemon
        restarts.
        """
        now = time.time()
        reason: str | None = None
        if event.type == RetryEvent.DISCONNECTED:
            reason = _disconnect_reason(event)

        with self._store.edit(profile) as section:
            attempts = int(section.get("connect_attempts_total", 0))
            sessions = int(section.get("sessions_total", 0))
            reconnects = int(section.get("reconnects_total", 0))
            uptime_total = float(section.get("tunnel_uptime_seconds", 0.0))
            downtime_total = float(section.get("tunnel_downtime_seconds", 0.0))
            feed = list(section.get("recent_events", []))

            if event.type == RetryEvent.CONNECTING:
                attempts += 1
                # Close the previous downtime gap (disconnect → this attempt).
                pending = self._pending_disconnect_at.get(profile)
                if pending is not None:
                    downtime_total += max(0.0, now - pending)
                    self._pending_disconnect_at[profile] = None
            elif event.type == RetryEvent.CONNECTED:
                sessions += 1
            elif event.type == RetryEvent.DISCONNECTED:
                duration = getattr(manager, "last_session_duration", None)
                if duration is not None:
                    uptime_total += max(0.0, float(duration))
                self._pending_disconnect_at[profile] = now
            elif event.type == RetryEvent.RETRYING:
                reconnects += 1

            entry = {
                "at": now,
                "type": event.type,
            }
            if event.type == RetryEvent.DISCONNECTED:
                entry["reason"] = reason or "unknown"
                if event.exit_code is not None:
                    entry["exit_code"] = event.exit_code
            elif event.type == RetryEvent.RETRYING:
                entry["attempt"] = event.attempt
                entry["delay"] = round(event.delay, 3)
            feed.append(entry)
            del feed[:-_EVENT_FEED_LIMIT]

            section.update(
                {
                    "connect_attempts_total": attempts,
                    "sessions_total": sessions,
                    "reconnects_total": reconnects,
                    "tunnel_uptime_seconds": uptime_total,
                    "tunnel_downtime_seconds": downtime_total,
                    "current_session_at": now
                    if event.type == RetryEvent.CONNECTING
                    else (
                        None
                        if event.type == RetryEvent.DISCONNECTED
                        else section.get("current_session_at")
                    ),
                    "last_disconnect_at": now
                    if event.type == RetryEvent.DISCONNECTED
                    else section.get("last_disconnect_at"),
                    "last_disconnect_reason": reason
                    if event.type == RetryEvent.DISCONNECTED
                    else section.get("last_disconnect_reason"),
                    "recent_events": feed,
                }
            )

    # -- Foreground loop -------------------------------------------------------

    def run(self) -> int:
        """Block, keeping every configured tunnel up, until stopped.

        Each profile runs its own retry + health loops on their own threads;
        this thread only supervises, and exits once every profile loop has
        ended (retry budget exhausted, or a crash that
        :class:`ProfileRunner` recorded). Returns the daemon exit code (``0``
        for a clean, requested stop).
        """
        self._setup_logging()
        log = logging.getLogger("ponte.daemon")
        # The spawn log exists only to explain a startup that never got this
        # far; now that logging works it would sit next to the pid file forever.
        self._safe_remove(_spawn_log_path(self.pid_file))

        import ponte
        log.info(
            "ponte v%s daemon starting (pid %d, profiles: %s)",
            ponte.__version__,
            os.getpid(),
            ", ".join(self.profile_names) or "none",
        )
        self.write_pid()
        self._safe_remove(self.stop_marker)
        # A reload request left behind by a previous, already-dead daemon must
        # not fire on this one's first sweep.
        self._safe_remove(self.reload_marker)

        # Prime the status file with a start time before the first health tick.
        # Merge, don't overwrite: the cumulative tunnel statistics must survive
        # daemon restarts (systemd / the Scheduled Task respawn the process on
        # crash, and the user cares about the tunnel's history).
        self._store.begin(self.profile_names, time.time())

        self._runners = [
            ProfileRunner(profile, self.config, self)
            for profile in self.config.profiles
        ]

        def request_stop(reason: str) -> None:
            """Request shutdown from any thread. Idempotent, never raises."""
            if self._shutdown.is_set():
                return
            log.info("shutdown requested: %s", reason)
            self._shutdown.set()
            for runner in self._runners:
                runner.abort()

        def request_reload() -> None:
            """Apply a new config on a worker thread, leaving the caller free.

            Reconciling can block while it joins a retired profile's thread, so
            it must not run on the marker watcher (which also has to notice a
            stop request)."""
            threading.Thread(
                target=self._reload_worker, name="ponte-reload", daemon=True
            ).start()

        # SIGINT (Ctrl+C) and, where catchable, SIGTERM.
        try:
            signal.signal(signal.SIGINT, lambda *_a: request_stop("SIGINT"))
            signal.signal(signal.SIGTERM, lambda *_a: request_stop("SIGTERM"))
        except (ValueError, OSError):
            pass  # SIGTERM may be uncatchable on some Windows builds

        # SIGHUP is the POSIX convention for "re-read your config"; the marker
        # file covers every platform, including Windows where SIGHUP is not
        # deliverable.
        if sys.platform != "win32":
            try:
                signal.signal(signal.SIGHUP, lambda *_a: request_reload())
            except (ValueError, OSError, AttributeError):
                pass

        # Stop-marker watcher gives cross-process graceful stop on Windows.
        threading.Thread(
            target=self._watch_stop_marker,
            args=(request_stop,),
            daemon=True,
            name="ponte-stop-watch",
        ).start()
        threading.Thread(
            target=self._watch_reload_marker,
            args=(request_reload,),
            daemon=True,
            name="ponte-reload-watch",
        ).start()

        log.info(
            "starting SSH retry loop(s) (max_retries=%s, %d profile(s))",
            self.config.retry.max_retries,
            len(self._runners),
        )
        try:
            for runner in self._runners:
                runner.start()
            while not self._shutdown.is_set():
                self._shutdown.wait(_SUPERVISOR_INTERVAL)
                # ``self._runners`` is rebound (not mutated) by a reload, and
                # during that swap it can briefly hold only retired runners;
                # treating that as "all profiles died" would kill the daemon.
                if self._reconciling.is_set():
                    continue
                if not any(runner.is_alive() for runner in self._runners):
                    log.warning("every profile loop has exited")
                    break
        except KeyboardInterrupt:  # pragma: no cover - must reload to trigger
            request_stop("KeyboardInterrupt")
        finally:
            request_stop("daemon shutdown")
            for runner in self._runners:
                runner.finish()
            self._cleanup()
        log.info("daemon exited cleanly")
        return 0

    def _record_profile_error(self, profile: str, message: str) -> None:
        """Record that a profile's loop died, so ``status`` can surface it."""
        with self._store.edit(profile) as section:
            section["error"] = message

    def _record_notification(self, profile: str) -> None:
        """Stamp the moment *profile*'s failure alert was delivered."""
        with self._store.edit(profile) as section:
            section["last_notification_at"] = time.time()

    def _watch_stop_marker(self, request_stop: Callable[[str], None]) -> None:
        """Watch for a stop marker file and request a graceful shutdown."""
        while not self._shutdown.is_set():
            if os.path.exists(self.stop_marker):
                request_stop("stop marker file present")
                return
            self._shutdown.wait(_STOP_POLL_INTERVAL)

    # -- Config reload (hot) ---------------------------------------------------

    def request_reload(self) -> None:
        """Ask a *running* daemon (another process) to re-read its config.

        Drops the reload marker the daemon's watcher polls — the same
        cross-process mechanism as the stop marker, so it works on Windows
        where SIGHUP cannot be delivered. Returns as soon as the request is on
        disk; the caller cannot observe the outcome synchronously, which is why
        ``ponte reload`` validates the config *before* writing the marker.
        """
        directory = os.path.dirname(self.reload_marker)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.reload_marker, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))

    def _watch_reload_marker(self, request_reload: Callable[[], None]) -> None:
        """Watch for a reload marker and apply the new config when it appears."""
        while not self._shutdown.is_set():
            if os.path.exists(self.reload_marker):
                # Remove before reconciling: a request that lands mid-reload is
                # then kept for the next sweep instead of being lost.
                self._safe_remove(self.reload_marker)
                request_reload()
            self._shutdown.wait(_STOP_POLL_INTERVAL)

    def _reload_worker(self) -> None:
        """Run :meth:`_reconcile` and log its summary."""
        summary = self._reconcile()
        logging.getLogger("ponte.daemon").info("reload: %s", summary)

    def _retire(self, runner: ProfileRunner) -> None:
        """Stop a profile's loops and wait for its thread to end, best effort."""
        try:
            runner.finish()
        except Exception as exc:  # noqa: BLE001 - one bad runner must not abort a reload
            logger.warning(
                "[%s] could not stop cleanly during reload: %s", runner.profile.name, exc
            )

    def _reconcile(self) -> str:
        """Re-read the config file and apply it in place; returns a summary.

        Only profiles whose configuration actually changed are restarted, so
        adding a tunnel or fixing one profile's key no longer drops every other
        connection. ``[retry]`` and ``[health]`` are baked into each runner at
        construction time, so a change to either rebuilds every profile (the
        connection must be re-established to pick up new policy).

        Never raises: a broken or unreadable config leaves the running tunnels
        exactly as they are and comes back as a message, because a reload that
        can kill a working tunnel on a typo is worse than no reload at all.
        """
        source = self.config.source_path
        if not source:
            return "配置没有来源文件，未重载"
        try:
            new_config = load_config(source)
        except ConfigError as exc:
            logger.error("reload rejected, keeping the running config: %s", exc)
            return f"重载失败，继续沿用旧配置：{exc}"

        if not self._reload_lock.acquire(blocking=False):
            return "已有一次重载在进行，忽略本次请求"
        try:
            self._reconciling.set()
            policy_changed = (
                new_config.retry != self.config.retry
                or new_config.health != self.config.health
            )

            previous = {runner.profile.name: runner for runner in self._runners}
            latest: list[ProfileRunner] = []
            started: list[str] = []
            kept: list[str] = []
            for profile in new_config.profiles:
                existing = previous.pop(profile.name, None)
                unchanged = (
                    existing is not None
                    and not policy_changed
                    and existing.profile == profile
                )
                if unchanged and existing is not None:
                    latest.append(existing)
                    kept.append(profile.name)
                    continue
                if existing is not None:
                    self._retire(existing)
                runner = ProfileRunner(profile, new_config, self)
                runner.start()
                latest.append(runner)
                started.append(profile.name)

            removed = sorted(previous)
            for runner in previous.values():
                self._retire(runner)

            self._runners = latest
            self.config = new_config
            # Notify policy is global: rebuild the notifier and hand it to every
            # runner, including the ones that were left running.
            self.notifier = Notifier(new_config.notify)
            for runner in latest:
                runner.notifier = self.notifier

            # Merge (don't overwrite) so the counters of surviving tunnels keep
            # their history; the start time is preserved so daemon uptime does
            # not reset on a reload.
            self._store.begin(
                new_config.profile_names, self._store.started_at() or time.time()
            )
            self._store.remove(removed)
        finally:
            self._reconciling.clear()
            self._reload_lock.release()

        parts: list[str] = []
        if kept:
            parts.append(f"保持 {len(kept)} 条（{', '.join(kept)}）")
        if started:
            parts.append(f"重启/新增 {len(started)} 条（{', '.join(started)}）")
        if removed:
            parts.append(f"移除 {len(removed)} 条（{', '.join(removed)}）")
        if policy_changed:
            parts.append("retry/health 策略有变，全部重建")
        return "配置已重载：" + ("；".join(parts) if parts else "无变化")

    # -- Start / background ----------------------------------------------------

    def _daemon_args(self) -> list[str]:
        """CLI arguments that re-run this daemon in the foreground.

        The active config file is passed explicitly (``--config``) so a
        service-managed daemon never resolves a *different* file than the one
        the user installed it with — the Windows SYSTEM task, for example, has
        its own ``%APPDATA%`` and would otherwise look in the wrong place.
        """
        args = ["-m", "ponte.main"]
        if self.config.source_path:
            args.extend(["--config", self.config.source_path])
        args.extend(["start", "--foreground"])
        return args

    def _daemon_args_string(self) -> str:
        """`_daemon_args` rendered for a Windows Scheduled-Task action string."""
        return " ".join(
            f'"{arg}"' if " " in arg else arg for arg in self._daemon_args()
        )

    def start(self, foreground: bool = False) -> int:
        """Start the daemon, optionally in the background.

        In background mode the daemon re-spawns itself detached and the parent
        returns the child PID once it writes its PID file.
        """
        if not foreground:
            return self._spawn_background()
        return self.run()

    def _spawn_background(self) -> int:
        """Re-launch this CLI as a detached background process."""
        cmd = [sys.executable, *self._daemon_args()]
        # Capture the child's first words instead of dropping them: if it cannot
        # even import ponte (a wrong working directory, a broken install) it dies
        # before it can log anything, and "did not write its PID file" on its own
        # sends the user looking in the wrong place.
        captured = _spawn_log_path(self.pid_file)
        try:
            handle = open(captured, "wb")
        except OSError:
            handle = None  # diagnostics are optional; never fail the spawn for it
        if sys.platform == "win32":
            flags = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            )
            subprocess.Popen(
                cmd,
                cwd=self.work_dir,
                creationflags=flags,
                stdin=subprocess.DEVNULL,
                stdout=handle or subprocess.DEVNULL,
                stderr=subprocess.STDOUT if handle else subprocess.DEVNULL,
            )
        else:
            # POSIX: start a new session so the child detaches from the
            # controlling terminal and survives the parent shell exiting.
            subprocess.Popen(
                cmd,
                cwd=self.work_dir,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=handle or subprocess.DEVNULL,
                stderr=subprocess.STDOUT if handle else subprocess.DEVNULL,
            )
        if handle is not None:
            handle.close()  # the child holds its own handle
        # Wait for the child to write its PID file (up to 10 s).
        for _ in range(100):
            pid = self.read_pid()
            if pid is not None:
                if captured:
                    self._safe_remove(captured)
                return pid
            time.sleep(0.1)
        raise RuntimeError(
            "daemon did not write its PID file within 10 s" + _spawn_tail(captured)
        )

    # -- Stop ------------------------------------------------------------------

    def stop(self, timeout: float = 20.0) -> DaemonStatus:
        """Gracefully stop a running daemon, escalating to a force kill.

        Steps: stop the scheduled task (so it does not respawn us), drop the
        stop marker, wait for the PID to vanish, then ``taskkill /T /F`` if the
        daemon ignores the request.

        The returned status carries a ``message`` when the graceful path failed,
        so the CLI can tell the user a force kill was used instead of silently
        escalating.
        """
        status = self.status()
        if not status.running or status.pid is None:
            return status

        # 1. Stop the auto-start hook first so it cannot restart the daemon.
        self._stop_autostart()

        # 2. Drop the stop marker for a graceful shutdown.
        try:
            with open(self.stop_marker, "w", encoding="utf-8") as handle:
                handle.write(str(time.time()))
        except OSError as exc:
            logger.warning("could not write stop marker: %s", exc)

        # 3. Wait for the daemon to exit.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self._pid_alive(status.pid):
            time.sleep(0.2)

        # 4. Escalate if still alive.
        forced = False
        if self._pid_alive(status.pid):
            logger.warning(
                "daemon did not stop gracefully; force killing pid %d", status.pid
            )
            self._force_kill(status.pid)
            forced = True

        self._safe_remove(self.stop_marker)
        self._safe_remove(self.pid_file)
        final = self.status()
        if forced:
            return dataclasses.replace(
                final,
                message=(
                    f"守护进程未在 {timeout:.0f}s 内退出，已强制 kill（含子进程）"
                ),
            )
        return final

    def _force_kill(self, pid: int) -> None:
        """Kill *pid* and its whole tree, regardless of platform.

        On Windows the whole process tree is killed. On POSIX we first send a
        graceful ``SIGTERM`` and only escalate to ``SIGKILL`` after the
        configured grace period, so the daemon gets a chance to clean up.
        """
        if sys.platform == "win32":
            # `taskkill` is a console program: spawning it bare flashes a black
            # box at exactly the wrong moment -- while the user is trying to
            # stop a windowless daemon.
            _run_tool(["taskkill", "/PID", str(pid), "/T", "/F"])
            return
        # POSIX: graceful SIGTERM, then escalate to SIGKILL.
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + self.config.service.kill_timeout
        while time.monotonic() < deadline and self._pid_alive(pid):
            time.sleep(0.2)
        if self._pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    # -- Status ----------------------------------------------------------------

    def status(self) -> DaemonStatus:
        """Build a snapshot with one :class:`ProfileStatus` per profile."""
        pid = self.read_pid()
        if pid is None or not self._pid_alive(pid):
            return DaemonStatus(running=False, message="daemon is not running")
        sections = self._store.read_profiles()
        started = self._store.started_at()
        uptime = (time.time() - started) if started else 0.0
        # Configuration order first, then anything else the status file knows
        # about — a profile dropped from the config keeps showing up until the
        # daemon that still supervises it is stopped.
        names = list(self.profile_names)
        names += [name for name in sections if name not in names]
        destinations = {
            profile.name: profile.destination for profile in self.config.profiles
        }
        # Same origin as the destination: the config as the *running* daemon read
        # it, so a jump chain added to the file is not shown until the reload
        # that actually puts it on the command line.
        jumps = {
            profile.name: profile.ssh.proxy_jump for profile in self.config.profiles
        }
        return DaemonStatus(
            running=True,
            pid=pid,
            started_at=started,
            uptime_seconds=max(0.0, uptime),
            profiles=[
                _profile_status(
                    name,
                    sections.get(name, {}),
                    destination=destinations.get(name),
                    jump=jumps.get(name),
                )
                for name in names
            ],
        )

    # -- Diagnostics -----------------------------------------------------------

    def test_connection(self, timeout: int = 10, profile: str | None = None) -> bool:
        """Run a one-off ``ssh ... echo OK`` against one profile's endpoint."""
        manager = TunnelManager(self.config, self.config.get_profile(profile))
        return manager.test_connection(timeout=timeout)

    def check_remote_ports(
        self, timeout: int = 10, profile: str | None = None
    ) -> dict[int, bool]:
        """Probe one profile's remote (``-R``) ports on the server."""
        manager = TunnelManager(self.config, self.config.get_profile(profile))
        return manager.check_remote_ports(timeout=timeout)

    def check_local_ports(
        self, timeout: float = 1.0, profile: str | None = None
    ) -> dict[int, bool]:
        """Probe one profile's local (``-L``/``-D``) listeners."""
        manager = TunnelManager(self.config, self.config.get_profile(profile))
        return manager.check_local_ports(timeout=timeout)

    # -- Cross-platform service install ----------------------------------------

    def install_service(self) -> str:
        """Register OS-level auto-start + crash-restart for the daemon.

        Dispatches to the platform backend: Windows Scheduled Task, a systemd
        *user* unit on Linux, or a launchd LaunchAgent on macOS.
        """
        if sys.platform == "win32":
            return self.install_scheduled_task()
        if sys.platform == "linux":
            return self._install_systemd()
        if sys.platform == "darwin":
            return self._install_launchd()
        raise RuntimeError(f"service install not supported on {sys.platform}")

    def uninstall_service(self) -> str:
        """Remove the auto-start service registered by :meth:`install_service`."""
        if sys.platform == "win32":
            return self.uninstall_scheduled_task()
        if sys.platform == "linux":
            return self._uninstall_systemd()
        if sys.platform == "darwin":
            return self._uninstall_launchd()
        raise RuntimeError(f"service uninstall not supported on {sys.platform}")

    def service_installed(self) -> bool | None:
        """Best-effort: is the auto-start service registered for this config?

        Used by ``ponte doctor``. ``None`` means "cannot tell" (unknown
        platform, or the query tool is not installed) — which the caller must
        report as unknown rather than as "not installed", because those two
        need completely different fixes.
        """
        try:
            if sys.platform == "win32":
                result = _run_tool(
                    ["schtasks", "/Query", "/TN", self.config.windows.task_name],
                    timeout=15,
                )
                return result.returncode == 0
            if sys.platform == "linux":
                name = self.config.service.name
                result = _run_tool(
                    ["systemctl", "--user", "is-enabled", f"{name}.service"],
                    timeout=15,
                )
                return result.returncode == 0
            if sys.platform == "darwin":
                return os.path.exists(self._launchd_plist())
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("could not query the service state: %s", exc)
        return None

    def _stop_autostart(self) -> None:
        """Best-effort: stop the auto-start hook so it cannot respawn us."""
        try:
            if sys.platform == "win32":
                self._run_powershell_stop_task()
            elif sys.platform == "linux":
                name = self.config.service.name
                _run_tool(["systemctl", "--user", "stop", f"{name}.service"])
            elif sys.platform == "darwin":
                _run_tool(["launchctl", "unload", self._launchd_plist()])
        except Exception as exc:  # noqa: BLE001 - best effort
            logger.debug("could not stop service autostart: %s", exc)

    def _launchd_plist(self) -> str:
        """Return the per-user LaunchAgent plist path (reverse-DNS label)."""
        label = f"com.modusensus.{self.config.service.name}"
        return os.path.join(
            os.path.expanduser("~/Library/LaunchAgents"), f"{label}.plist"
        )

    def _install_systemd(self) -> str:
        """Write a systemd *user* unit and enable+start it."""
        name = self.config.service.name
        unit_dir = os.path.expanduser("~/.config/systemd/user")
        os.makedirs(unit_dir, exist_ok=True)
        unit_path = os.path.join(unit_dir, f"{name}.service")
        exec_args = " ".join(f'"{arg}"' for arg in self._daemon_args())
        unit = (
            "[Unit]\n"
            f"Description=ponte SSH reverse tunnel daemon ({name})\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f'ExecStart="{sys.executable}" {exec_args}\n'
            f"WorkingDirectory={self.work_dir}\n"
            "Restart=always\n"
            "RestartSec=15\n"
            "Environment=PYTHONUNBUFFERED=1\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        with open(unit_path, "w", encoding="utf-8") as handle:
            handle.write(unit)
        _run_tool(["systemctl", "--user", "daemon-reload"], check=True)
        _run_tool(
            ["systemctl", "--user", "enable", "--now", f"{name}.service"],
            check=True,
        )
        return "installed"

    def _uninstall_systemd(self) -> str:
        """Disable+stop the systemd user unit and remove the file."""
        name = self.config.service.name
        _run_tool(["systemctl", "--user", "disable", "--now", f"{name}.service"])
        unit_path = os.path.join(
            os.path.expanduser("~/.config/systemd/user"), f"{name}.service"
        )
        if os.path.exists(unit_path):
            os.remove(unit_path)
        _run_tool(["systemctl", "--user", "daemon-reload"])
        return "uninstalled"

    def _install_launchd(self) -> str:
        """Write a LaunchAgent plist and load it (auto-start + KeepAlive)."""
        plist_path = self._launchd_plist()
        os.makedirs(os.path.dirname(plist_path), exist_ok=True)
        label = f"com.modusensus.{self.config.service.name}"
        program_args = "\n".join(
            f"        <string>{xml_escape(arg)}</string>"
            for arg in [sys.executable, *self._daemon_args()]
        )
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            f"    <key>Label</key><string>{xml_escape(label)}</string>\n"
            "    <key>ProgramArguments</key>\n"
            "    <array>\n"
            f"{program_args}\n"
            "    </array>\n"
            f"    <key>WorkingDirectory</key><string>{self.work_dir}</string>\n"
            "    <key>RunAtLoad</key><true/>\n"
            "    <key>KeepAlive</key><true/>\n"
            "    <key>ProcessType</key><string>Background</string>\n"
            f"    <key>StandardOutPath</key><string>{xml_escape(self.log_file)}</string>\n"
            f"    <key>StandardErrorPath</key><string>{xml_escape(self.log_file)}</string>\n"
            "</dict>\n"
            "</plist>\n"
        )
        with open(plist_path, "w", encoding="utf-8") as handle:
            handle.write(plist)
        _run_tool(["launchctl", "load", "-w", plist_path], check=True)
        return "installed"

    def _uninstall_launchd(self) -> str:
        """Unload and remove the LaunchAgent plist."""
        plist_path = self._launchd_plist()
        _run_tool(["launchctl", "unload", "-w", plist_path])
        if os.path.exists(plist_path):
            os.remove(plist_path)
        return "uninstalled"

    # -- Windows Scheduled Task ------------------------------------------------

    def install_scheduled_task(self) -> str:
        """Register a Scheduled Task with OS-level auto-restart.

        The task runs ``pythonw -m ponte.main --config <file> start
        --foreground`` with the working directory set to
        :attr:`work_dir`.  The identity/timing depends on ``[windows] run_as``:

        * ``user`` (default) — logon-time task running as the installing user
          (``Interactive`` + ``Limited``), can read the user's keys and needs
          no elevation, but runs only after an interactive logon.
        * ``system`` — boot-time task running as SYSTEM (``ServiceAccount``),
          so the tunnel is up before login; install requires elevation and the
          task cannot reach per-user SSH keys, so point ``identity_file`` at a
          key SYSTEM can read.

        ``RestartCount`` (999, every minute) covers task-level restarts on top
        of the in-process retry loop.
        """
        if sys.platform != "win32":
            raise RuntimeError("Scheduled Tasks are only supported on Windows")
        exe = self._pythonw_path()
        task_name = self.config.windows.task_name
        run_as = self.config.windows.run_as

        if run_as == "user":
            trigger = "$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name\n$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser"
            principal = "$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited"
        else:
            trigger = "$trigger = New-ScheduledTaskTrigger -AtStartup"
            principal = "$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest"

        script = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute '{exe}' -Argument '{self._daemon_args_string()}' -WorkingDirectory '{self.work_dir}'
{trigger}
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
{principal}
Register-ScheduledTask -TaskName '{task_name}' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Output 'installed'
"""
        return self._run_powershell(script).strip()

    def uninstall_scheduled_task(self) -> str:
        """Remove the Scheduled Task registered by :meth:`install_scheduled_task`."""
        task_name = self.config.windows.task_name
        script = (
            f"Unregister-ScheduledTask -TaskName '{task_name}' -Confirm:$false; "
            "Write-Output 'uninstalled'"
        )
        try:
            return self._run_powershell(script).strip()
        except RuntimeError:
            return "uninstalled"

    def _run_powershell_stop_task(self) -> None:
        task_name = self.config.windows.task_name
        script = f"Stop-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue"
        self._run_powershell(script)

    def _pythonw_path(self) -> str:
        """Return the *windowless* interpreter the Scheduled Task must run.

        ``pythonw.exe`` is what keeps a logon/boot task from flashing a console
        window, so this deliberately never falls back to ``python.exe``: a
        console interpreter in an interactive task is precisely the "sometimes
        it does not start silently" popup.

        Resolution order:

        1. ``[windows] pythonw_exe``, when configured.
        2. ``sys.executable`` itself, when it already is a ``pythonw`` binary.
        3. ``pythonw.exe`` next to ``sys.executable``.

        Raises:
            RuntimeError: when none of the above exists, naming the interpreter
                and the two available fixes rather than installing a task that
                pops up a console window at every logon.
        """
        configured = self.config.windows.pythonw_exe
        if configured:
            if os.path.exists(configured):
                return configured
            raise RuntimeError(
                f"[windows] pythonw_exe 指向的路径不存在：{configured}"
            )
        if os.path.basename(sys.executable).lower().startswith("pythonw"):
            return sys.executable
        candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
        raise RuntimeError(
            "找不到 pythonw.exe（只有 python.exe）：用它注册计划任务会在每次登录时"
            f"弹出黑色控制台窗口，因此已中止安装。当前解释器：{sys.executable}。"
            "可选解决办法：① 用 pipx 安装（其虚拟环境自带 pythonw.exe）；"
            "② 在配置里显式指定 [windows] pythonw_exe = \"...pythonw.exe\"。"
        )

    def _run_powershell(self, script: str, timeout: float = 90.0) -> str:
        """Run a PowerShell snippet via ``-EncodedCommand`` and return stdout."""
        cmd = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-EncodedCommand", _encode_ps(script),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=creation_flags(),
        )
        # PowerShell writes ANSI/GBK on Chinese systems; decode defensively so a
        # garbled line never masks the real exit code / stderr below.
        stdout = _decode_console(result.stdout)
        stderr = _decode_console(result.stderr)
        if result.returncode != 0:
            raise RuntimeError(
                f"PowerShell exited {result.returncode}: "
                f"{(stderr or stdout).strip()}"
            )
        return stdout

    # -- Logging ---------------------------------------------------------------

    def _setup_logging(self) -> None:
        """Configure rotating file logging. Idempotent."""
        root = logging.getLogger("ponte")
        if getattr(root, "_ponte_setup_ok", False):
            return
        root.setLevel(logging.INFO)
        max_bytes = self.config.daemon.log_max_bytes
        backups = self.config.daemon.log_backup_count
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            self.log_file,
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(handler)
        root._ponte_setup_ok = True  # type: ignore[attr-defined]

    # -- Cleanup helpers -------------------------------------------------------

    def _cleanup(self) -> None:
        try:
            self._safe_remove(self.pid_file)
            self._safe_remove(self.stop_marker)
            self._safe_remove(self.reload_marker)
        finally:
            self._shutdown.set()

    @staticmethod
    def _safe_remove(path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
