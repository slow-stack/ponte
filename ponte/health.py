"""Health monitoring for the ponte SSH reverse tunnel manager.

Independently of the retry layer, this module periodically checks whether the
underlying SSH process is still alive and whether the remote forwarding ports
are listening. A monitor callback can then decide to restart a dead tunnel,
log a warning, or leave the retry loop to handle it.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections.abc import Callable

from ponte.config import HealthConfig
from ponte.core import ProbeError, TunnelManager

__all__ = ["HealthChecker", "HealthStatus"]

logger = logging.getLogger(__name__)

#: Type of a user-supplied run-loop callback: ``Callable[[HealthStatus], None]``.
HealthCallback = Callable[["HealthStatus"], None]

#: Timeout (seconds) for the in-process probe of a ``-L``/``-D`` local listener.
#: A loopback connect either succeeds or is refused immediately, so this is
#: deliberately much shorter than ``remote_check_timeout`` (which has to pay
#: for an SSH round trip).
_LOCAL_PROBE_TIMEOUT = 1.0


@dataclasses.dataclass(frozen=True)
class HealthStatus:
    """A snapshot of tunnel health taken at ``timestamp``.

    Attributes:
        process_alive: True if the SSH process is still running.
        remote_ports: Mapping of remote port -> listening (True/False), for the
            ``-R`` tunnels probed on the server.
        local_ports: Mapping of local port -> listening (True/False), for the
            ``-L``/``-D`` listeners probed on this machine.
        all_healthy: Overall health — the process is alive, every checked
            remote *and* local port is listening, and the check did not error.
            Note that ``all_healthy is False`` does **not** by itself mean the
            tunnel is down: a check that could not be completed is also not
            healthy (see ``conclusive``).
        timestamp: Unix time (``time.time()``) when the check was performed.
        error: Human-readable error message if the check partially failed,
            else ``None``.
        remote_probe_error: Set when the *server-side* probe connection itself
            failed, so the ``-R`` ports could not be observed at all. Their
            state is then unknown — ``remote_ports`` stays empty rather than
            claiming every port is closed.
    """

    process_alive: bool
    remote_ports: dict[int, bool]
    all_healthy: bool
    timestamp: float
    error: str | None = None
    local_ports: dict[int, bool] = dataclasses.field(default_factory=dict)
    remote_probe_error: str | None = None

    @property
    def conclusive(self) -> bool:
        """Whether this snapshot is a verdict about the tunnel at all.

        A dead SSH process, or a port that a probe *did* reach and found
        closed, is a verdict. A probe that never ran is not: it says the
        inspector failed, not the tunnel. Callers use this to keep the two
        apart — only a conclusive unhealthy reading may escalate (the daemon
        force-reconnects after ``_HEALTH_FAILURE_THRESHOLD`` of them), because
        a probe connection fails for reasons that leave the tunnel untouched.
        """
        if not self.process_alive:
            return True
        return self.error is None and self.remote_probe_error is None

    def __str__(self) -> str:  # human-friendly one-liner for logs
        remote: object
        if self.remote_probe_error is not None:
            remote = "unknown"
        else:
            remote = {
                port: ("ok" if ok else "down")
                for port, ok in self.remote_ports.items()
            }
        local = {
            port: ("ok" if ok else "down") for port, ok in self.local_ports.items()
        }
        return (
            f"process={'alive' if self.process_alive else 'dead'}, "
            f"remote_ports={remote}, "
            f"local_ports={local},"
            f" healthy={self.all_healthy}"
            + ("" if self.conclusive else ", conclusive=False")
            + (f", error={self.error!r}" if self.error else "")
            + (
                f", probe_error={self.remote_probe_error!r}"
                if self.remote_probe_error
                else ""
            )
        )


class HealthChecker:
    """Periodic health checks for a :class:`TunnelManager`.

    ``config`` is a :class:`HealthConfig` carrying the ``[health]`` section of
    ``config.toml``. Used fields:

    * ``check_interval``       — default seconds between checks (``run_loop``
      accepts its own ``interval`` override).
    * ``remote_check_enabled`` — whether the *server-side* probe (which costs
      an SSH connection) runs at all. The local-listener probe of ``-L``/``-D``
      tunnels is an in-process loopback connect, so it always runs.
    * ``remote_check_timeout`` — per-port probe timeout, seconds.
    * ``max_check_interval``   — ceiling (seconds) on the check interval under
      exponential backoff (see :meth:`run_loop`).
    """

    def __init__(self, manager: TunnelManager, config: HealthConfig) -> None:
        self.manager = manager
        self.config = config
        self.check_interval = config.check_interval
        self.remote_check_enabled = config.remote_check_enabled
        self.remote_check_timeout = config.remote_check_timeout
        # Last exception raised by a user callback in run_loop(), if any. The
        # loop swallows callback errors so one bad callback cannot kill the
        # monitor, but it records the most recent one here for diagnostics.
        self.last_callback_error: BaseException | None = None

    # -- Checks ---------------------------------------------------------------

    def check(self) -> HealthStatus:
        """Perform one health check and return a :class:`HealthStatus` snapshot.

        Never raises: failures are reported through the ``error`` field and
        the ``all_healthy`` flag rather than propagating.
        """
        snapshot_time = time.time()
        error_messages: list[str] = []

        # 1. Is the SSH process still running?
        process_alive = False
        try:
            process_alive = self._is_process_alive()
        except Exception as exc:  # noqa: BLE001
            error_messages.append(f"process check failed: {type(exc).__name__}: {exc}")

        # 2. Are the remote forwarding ports listening?
        remote_ports: dict[int, bool] = {}
        remote_probe_error: str | None = None
        if self.remote_check_enabled:
            try:
                remote_ports = self.check_remote_ports()
            except ProbeError as exc:
                # The probe never ran. The ports are *unknown*, not down:
                # leaving them out entirely means no display layer can turn a
                # failed probe connection into "未监听".
                remote_probe_error = str(exc)
                logger.debug("remote port probe could not run: %s", exc)
            except Exception as exc:  # noqa: BLE001
                error_messages.append(
                    f"remote port check failed: {type(exc).__name__}: {exc}"
                )
        # When the remote check is disabled we leave remote_ports empty, so
        # ``all(remote_ports.values())`` is vacuously True and process_alive
        # alone determines health.

        # 3. Are the local listeners of -L/-D tunnels up? Cheap enough
        # (loopback connect, no SSH) to run on every tick.
        local_ports: dict[int, bool] = {}
        try:
            local_ports = self.check_local_ports()
        except Exception as exc:  # noqa: BLE001
            error_messages.append(
                f"local port check failed: {type(exc).__name__}: {exc}"
            )

        # 4. Aggregate. An unexpected error on any sub-check makes the result
        # unhealthy — not healthy is the safe default — but it is *not* a
        # verdict, so callers must read ``conclusive`` before escalating.
        error = "; ".join(error_messages) if error_messages else None
        all_healthy = (
            error is None
            and remote_probe_error is None
            and process_alive
            and all(remote_ports.values())
            and all(local_ports.values())
        )
        return HealthStatus(
            process_alive=process_alive,
            remote_ports=remote_ports,
            all_healthy=all_healthy,
            timestamp=snapshot_time,
            error=error,
            local_ports=local_ports,
            remote_probe_error=remote_probe_error,
        )

    def check_remote_ports(self) -> dict[int, bool]:
        """Probe remote ports via ``manager.check_remote_ports()``.

        Returns a ``{port: bool}`` mapping of which configured remote ports are
        listening. Tolerates both a ``dict[int, bool]`` and a simple iterable of
        open ports as return values. A :class:`~ponte.core.ProbeError` from the
        manager propagates untouched: it means the probe connection failed and
        the ports were never observed.
        """
        method = getattr(self.manager, "check_remote_ports", None)
        if not callable(method):
            raise RuntimeError(
                "TunnelManager.check_remote_ports() is not available"
            )
        try:
            result = method(timeout=self.remote_check_timeout)
        except TypeError:
            # The tunnel manager may not accept a timeout argument.
            result = method()

        if isinstance(result, dict):
            return {int(port): bool(ok) for port, ok in result.items()}
        if isinstance(result, (list, tuple, set, frozenset)):
            # An iterable of ports that are open — treat each as healthy.
            return {int(port): True for port in result}
        # Unknown shape: fail loudly rather than silently report health.
        raise TypeError(
            f"check_remote_ports() returned unsupported type {type(result).__name__}"
        )

    def check_local_ports(self) -> dict[int, bool]:
        """Probe the local listeners of ``-L``/``-D`` tunnels.

        Returns ``{}`` — with no error — when the manager has no local probe
        (a test stand-in, or a config whose tunnels are all ``-R``), so health
        quietly degrades to the process + remote-port checks.
        """
        method = getattr(self.manager, "check_local_ports", None)
        if not callable(method):
            return {}
        try:
            result = method(timeout=_LOCAL_PROBE_TIMEOUT)
        except TypeError:
            # The tunnel manager may not accept a timeout argument.
            result = method()

        if isinstance(result, dict):
            return {int(port): bool(ok) for port, ok in result.items()}
        if isinstance(result, (list, tuple, set, frozenset)):
            # An iterable of ports that are open — treat each as healthy.
            return {int(port): True for port in result}
        raise TypeError(
            f"check_local_ports() returned unsupported type {type(result).__name__}"
        )

    # -- Background loop ------------------------------------------------------

    def run_loop(
        self,
        interval: float | None = None,
        callback: HealthCallback | None = None,
        name: str = "ponte-health-check",
    ) -> threading.Event:
        """Run checks every ``interval`` seconds in a background daemon thread.

        Args:
            interval: Seconds between checks. Defaults to ``config.check_interval``.
            callback: Called with each :class:`HealthStatus`. A raising callback
                is caught and recorded in ``last_callback_error`` so it cannot
                kill the monitor thread.
            name: Thread name. The daemon makes it per-profile
                (``ponte-health-<profile>``) so one monitor per tunnel stays
                identifiable in a thread dump.

        Returns:
            A :class:`threading.Event` that, when set, stops the loop. The loop
            performs one check immediately, then runs until the event is set.

        Backoff: consecutive *unhealthy* checks grow the interval exponentially
        (``interval * 2 ** failures``), capped at ``config.max_check_interval``.
        Every remote-port check opens a new SSH connection, so a prolonged
        outage must not hammer the server hard enough to trip ``MaxStartups``.
        The interval returns to the base ``interval`` as soon as a check is
        healthy again. An inconclusive check (a probe connection that failed)
        counts as "not healthy" here on purpose: backing off is exactly what a
        rate-limiting path wants, even though the daemon will not treat it as
        evidence that the tunnel is down.
        """
        if interval is None:
            interval = self.check_interval
        if interval < 0:
            raise ValueError(f"interval must be >= 0, got {interval}")
        if callback is None:
            callback = lambda status: None  # noqa: E731 - intentional no-op

        max_interval = self.config.max_check_interval
        stop_event = threading.Event()

        def _loop() -> None:
            failures = 0
            wait = interval

            def _advance(healthy: bool) -> None:
                """Update the consecutive-failure counter and next wait."""
                nonlocal failures, wait
                if healthy:
                    failures = 0
                else:
                    failures += 1
                wait = self._backoff_interval(interval, failures, max_interval)

            try:
                status = self.check()
            except Exception as exc:  # noqa: BLE001 - check() should not
                self.last_callback_error = exc  # raise, but be defensive
                _advance(False)
            else:
                _advance(status.all_healthy)
                try:
                    callback(status)
                except Exception as exc:  # noqa: BLE001
                    self.last_callback_error = exc

            while not stop_event.is_set():
                # wait() returns True if the event was set -> stop.
                if stop_event.wait(wait):
                    return
                try:
                    status = self.check()
                except Exception as exc:  # noqa: BLE001 - check() should not
                    self.last_callback_error = exc  # raise, but be defensive
                    _advance(False)
                    continue
                _advance(status.all_healthy)
                try:
                    callback(status)
                except Exception as exc:  # noqa: BLE001
                    self.last_callback_error = exc

        thread = threading.Thread(target=_loop, name=name, daemon=True)
        thread.start()
        return stop_event

    # -- Internals -------------------------------------------------------------

    @staticmethod
    def _backoff_interval(
        base_interval: float, failures: int, max_interval: float
    ) -> float:
        """Return the next check interval after *failures* consecutive failures.

        Grows exponentially from ``base_interval`` (``base * 2 ** failures``)
        and is capped at ``max_interval`` so a long outage never stops probing
        entirely while never probing the SSH server harder than necessary.
        """
        return min(base_interval * (2**failures), max_interval)

    def _is_process_alive(self) -> bool:
        """Determines whether the underlying SSH process is still running.

        The tunnel manager stores its child process either as ``manager.process``
        or ``manager.proc`` (a ``subprocess.Popen``-like object), or exposes an
        ``is_running()`` method. ``poll()`` returning ``None`` means the process
        is still alive. If no process handle is available, the process is
        assumed alive so that healthy-teardown races do not produce false
        negatives.
        """
        is_running = getattr(self.manager, "is_running", None)
        if callable(is_running):
            try:
                return bool(is_running())
            except Exception:  # noqa: BLE001
                pass  # fall through to the process-handle check

        proc = getattr(self.manager, "process", None)
        if proc is None:
            proc = getattr(self.manager, "proc", None)
        if proc is None:
            return True  # no handle -> assume alive (conservative for liveness)

        poll = getattr(proc, "poll", None)
        if callable(poll):
            return poll() is None  # None return value == still running
        return True
