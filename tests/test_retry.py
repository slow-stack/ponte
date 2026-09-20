"""pytest tests for :mod:`ponte.retry` (pure logic, no network, no SSH).

These mirror the standalone ``_smoke_test.py`` script but are written as
pytest functions using the *real* imported modules so coverage is attributed
to the actual source files.
"""

from __future__ import annotations

import threading
import time

from _waits import join_thread
from ponte.config import RetryConfig
from ponte.retry import RetryEvent, RetryRunner


class _FakeManager:
    """A TunnelManager stand-in that drives the retry loop by itself.

    ``connect()`` must return on its own (it cannot wait for ``runner.stop()``
    to be called from the event driver, that would deadlock). It stays "up"
    briefly, then returns so the runner observes a disconnection.
    """

    def __init__(self, runner, connect_result: int = 0) -> None:
        self._runner = runner
        self.connect_result = connect_result
        self.calls = 0

    def connect(self) -> int:
        self.calls += 1
        deadline = time.monotonic() + 0.5
        while not self._runner._stop.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.connect_result


def _cfg(**kw) -> RetryConfig:
    defaults = dict(
        max_retries=3,
        base_delay=0.1,
        max_delay=0.4,
        backoff_factor=2.0,
        jitter=False,
    )
    defaults.update(kw)
    return RetryConfig(**defaults)


def test_basic_connect_stop_flow() -> None:
    """Connect -> disconnect -> retry -> stop, without exhausting the budget."""
    runner = RetryRunner(_cfg())
    mgr = _FakeManager(runner, connect_result=0)
    events: list[tuple] = []

    def driver() -> None:
        for ev in runner.run(mgr):
            events.append((ev.type, ev.exit_code, ev.delay, ev.attempt, ev.error))
            if ev.type == RetryEvent.RETRYING:
                runner.stop()

    t = threading.Thread(target=driver, name="retry-driver")
    t.start()
    # 这里曾经先 ``time.sleep(0.3)`` 再 ``join()``。那个 0.3 既不是约束也不是事实，
    # 只是"希望此时它已经跑完了"；慢 runner 上它什么也没保证。join 才是这段真正
    # 要的等待，而且它同时证明了驱动线程会自己退出（不会把套件挂住）。
    join_thread(t, what="重试驱动线程", timeout=10)

    assert events[0][0] == RetryEvent.CONNECTING, events
    idx = [e[0] for e in events]
    assert RetryEvent.CONNECTED in idx
    assert RetryEvent.DISCONNECTED in idx
    assert RetryEvent.RETRYING in idx
    assert RetryEvent.MAX_RETRIES_REACHED not in idx, events


class _FailManager:
    """connect() always returns immediately (tunnel dies instantly)."""

    def __init__(self) -> None:
        self.calls = 0

    def connect(self) -> None:
        self.calls += 1
        return None


class _ScriptedManager:
    """A TunnelManager stand-in with scripted per-call session durations.

    Each :meth:`connect` returns immediately and exposes the matching entry
    from ``durations`` as ``last_session_duration`` (the last value is reused
    for calls beyond the end of the list). If ``block_on_call`` is set, that
    1-based call blocks until the runner is stopped, simulating a tunnel the
    test keeps alive so the runner can be halted before it exhausts its
    budget.
    """

    def __init__(
        self,
        runner: RetryRunner,
        durations: list[float],
        block_on_call: int | None = None,
    ) -> None:
        self._runner = runner
        self.durations = durations
        self.block_on_call = block_on_call
        self.calls = 0
        self.last_session_duration: float | None = None

    def connect(self) -> int:
        self.calls += 1
        if self.block_on_call is not None and self.calls == self.block_on_call:
            while not self._runner._stop.is_set():
                time.sleep(0.01)
        self.last_session_duration = self.durations[
            min(self.calls - 1, len(self.durations) - 1)
        ]
        return 0


def test_stable_session_resets_retry_budget() -> None:
    """A session that ran >= stable_after resets ``retries_used``.

    With max_retries=2, two short-lived sessions put the runner on the verge
    of giving up. The stable session that follows resets the budget, so the
    runner retries again (attempt back to 1) instead of yielding
    MAX_RETRIES_REACHED. Without the reset it would give up immediately after
    the stable session drops.
    """
    runner = RetryRunner(_cfg(max_retries=2, base_delay=0.01, max_delay=0.1))
    mgr = _ScriptedManager(runner, durations=[5, 5, 70], block_on_call=4)
    events: list[RetryEvent] = []

    def driver() -> None:
        for ev in runner.run(mgr):
            events.append(ev)

    t = threading.Thread(target=driver)
    t.start()
    # Let the runner churn through the scripted sessions until it reaches the
    # blocking 4th connect, then stop it cleanly.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and mgr.calls < 4:
        time.sleep(0.01)
    runner.stop()
    t.join(timeout=5)
    assert not t.is_alive(), "driver thread did not finish"

    types = [e.type for e in events]
    assert RetryEvent.MAX_RETRIES_REACHED not in types, types
    # The two flaky sessions consume attempts 1 and 2; the stable session
    # resets the counter, so its retry is attempt 1 again.
    retrying_attempts = [e.attempt for e in events if e.type == RetryEvent.RETRYING]
    assert retrying_attempts == [1, 2, 1], retrying_attempts


def test_unstable_sessions_do_not_reset_retry_budget() -> None:
    """Short-lived sessions (< stable_after) never reset the budget.

    With max_retries=2, two short-lived connections exhaust the budget and a
    third connection attempt yields MAX_RETRIES_REACHED.
    """
    runner = RetryRunner(_cfg(max_retries=2, base_delay=0.01, max_delay=0.1))
    mgr = _ScriptedManager(runner, durations=[5, 5, 5])
    seq = [(e.type, e.attempt) for e in runner.run(mgr)]
    types = [t for t, _ in seq]
    assert types.count(RetryEvent.DISCONNECTED) == 3  # initial + 2 retries
    assert types.count(RetryEvent.CONNECTED) == 3
    assert seq[-1][0] == RetryEvent.MAX_RETRIES_REACHED
    assert [a for t, a in seq if t == RetryEvent.RETRYING] == [1, 2]


def test_max_retries_exhaustion() -> None:
    """max_retries=2 -> initial + 2 retries, then MAX_RETRIES_REACHED."""
    runner = RetryRunner(_cfg(max_retries=2, base_delay=0.01, max_delay=0.1))
    seq = [(e.type, e.attempt) for e in runner.run(_FailManager())]
    types_ = [s for s, _ in seq]
    assert types_.count(RetryEvent.DISCONNECTED) == 3  # initial + 2 retries
    assert types_.count(RetryEvent.CONNECTED) == 3
    assert seq[-1][0] == RetryEvent.MAX_RETRIES_REACHED


class _FlakyManager:
    """Fails on the first connect, then stays up until stop is requested."""

    def __init__(self, runner: RetryRunner) -> None:
        self._runner = runner
        self.calls = 0

    def connect(self) -> None:
        self.calls += 1
        if self.calls == 1:
            raise ChildProcessError("auth failed")
        while not self._runner._stop.is_set():
            time.sleep(0.01)
        return None


def test_max_retries_zero_retries_forever() -> None:
    """max_retries=0 -> infinite; a raised error is surfaced as DISCONNECTED."""
    runner = RetryRunner(_cfg(max_retries=0, base_delay=0.01, max_delay=0.05))
    mgr = _FlakyManager(runner)
    seq: list = []

    def driver() -> None:
        for ev in runner.run(mgr):
            seq.append(ev)
            if ev.type == RetryEvent.RETRYING and ev.attempt == 1:
                runner.stop()

    t = threading.Thread(target=driver)
    t.start()
    t.join(timeout=5)

    first_disc = seq[1]
    assert first_disc.type == RetryEvent.DISCONNECTED
    assert first_disc.error == "ChildProcessError: auth failed", first_disc
    assert RetryEvent.MAX_RETRIES_REACHED not in [e.type for e in seq], seq


def test_jitter_within_bounds() -> None:
    """Jittered delays land in [0, computed cap) with visible spread."""
    runner = RetryRunner(
        _cfg(max_retries=0, base_delay=5, max_delay=300, backoff_factor=2.0, jitter=True)
    )
    vals = [runner._backoff_delay(3) for _ in range(200)]
    cap = min(5 * 2.0 ** 3, 300)
    assert all(0 <= v < cap for v in vals), (min(vals), max(vals), cap)
    assert any(v > cap * 0.5 for v in vals), "expected spread"


def test_retry_event_repr() -> None:
    assert repr(RetryEvent.connecting()) == "RetryEvent('connecting')"
    assert "RetryEvent('disconnected'" in repr(RetryEvent.disconnected(1, error="boom"))
    assert "RetryEvent('retrying'" in repr(RetryEvent.retrying(1.5, 2))


def test_retry_event_equality() -> None:
    a = RetryEvent.connected()
    b = RetryEvent.connected()
    assert a == b
    assert a != RetryEvent.connecting()
    assert a != "not an event"


def test_sleep_interruptibly_zero_or_negative() -> None:
    runner = RetryRunner(_cfg())
    runner._sleep_interruptibly(0)
    runner._sleep_interruptibly(-1)
    assert not runner.stopped


def test_stop_idempotent() -> None:
    runner = RetryRunner(_cfg())
    runner.stop()
    assert runner.stopped
    runner.stop()
    assert runner.stopped
