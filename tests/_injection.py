"""把"慢机器"搬进测试进程的可选负载注入。

为什么需要它：依赖时序的断言只会**随机**报红。main 上真的红过一次——"睡 0.3 秒后至少
3 次检查"拿到 2；而它既不是产品代码错，也不是 runner 抽风，而是**断言考的是"这台机器
够快"**。快机器上它永远绿，慢机器上它偶尔红，两种结果都不回答"被测代码对不对"。

于是这里做的事是：在需要的时候，把"慢"变成一个**开关**，让那类测试当场失败。它属于
测试基础设施而不是产品代码，但它也是唯一能被"证明"的部分——两条轴都有对应的自检
（``tests/test_timing_guard.py``），因为一条静默失效的守卫比没有守卫更糟。

用法：由 ``tests/conftest.py`` 在会话开始时按环境变量装上（见 :func:`active`）。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

#: Seconds added to every *worker-thread* sleep/wait.
THREAD_DELAY_ENV = "PONTE_TEST_THREAD_DELAY"
#: Seconds a newly started thread waits *before running its body*.
START_DELAY_ENV = "PONTE_TEST_THREAD_START_DELAY"

# The unpatched primitives, captured at import time: without these the injection
# would slow itself (and a thread start delay would be stretched by the wait
# delay, so the two axes would stop being independent).
_ORIGINAL_SLEEP = time.sleep
_ORIGINAL_WAIT = threading.Event.wait
_ORIGINAL_THREAD_START = threading.Thread.start


def _number_env(name: str) -> float:
    """Read a numeric switch, refusing to silently ignore a broken value."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(
            f"{name}={raw!r} is not a number. Refusing to continue: this switch "
            "exists to make timing assumptions fail, so silently disabling it "
            "would hide exactly what it guards."
        ) from None
    if value < 0:
        raise SystemExit(f"{name} must not be negative: {raw!r}")
    return value


@dataclass(frozen=True)
class Injection:
    """The load this process is asked to emulate. Zero means "off"."""

    wait_delay: float = 0.0
    start_delay: float = 0.0

    def enabled(self) -> bool:
        return bool(self.wait_delay or self.start_delay)

    def banner(self) -> str:
        """One line per enabled axis, so a green run can prove the guard was on."""
        lines = []
        if self.wait_delay:
            lines.append(
                f"thread latency: +{self.wait_delay:g}s injected into every worker sleep/wait"
            )
        if self.start_delay:
            lines.append(
                f"thread start delay: +{self.start_delay:g}s before every thread body runs"
            )
        return "\n".join(lines)


_INSTALLED: Injection | None = None


def active() -> Injection:
    """The injection this process runs with, installing it on first call.

    一次安装、幂等：``conftest`` 和 ``-p _injection``（守卫的自检会用子进程这么跑）
    可能先后触发，装上两次会把延迟叠成两倍，那时测量到的一切都不再可信。
    """
    global _INSTALLED
    if _INSTALLED is None:
        _INSTALLED = Injection(_number_env(THREAD_DELAY_ENV), _number_env(START_DELAY_ENV))
        if _INSTALLED.enabled():
            _apply(_INSTALLED)
    return _INSTALLED


def pytest_configure(config) -> None:  # noqa: ARG001 - 只为让 `-p _injection` 生效
    """Allow ``-p _injection`` to install the injection without a conftest."""
    active()


def _apply(injection: Injection) -> None:
    """Make the code under test slower than the test that watches it.

    A slow machine fails timing-dependent tests in two distinct ways, and each
    one needs its own instrument:

    **1. the worker's own waits are stretched** (``wait_delay``). "Slow machine"
    is never uniform: every wait a *worker* performs gets queued behind whatever
    else the runner is doing, while the test's own ``time.sleep(0.3)`` stays
    exactly 0.3s. That asymmetry is what makes "slept 0.3s, expected 3 checks"
    fail on a loaded macOS runner and pass here. So the injection only slows
    threads that are not the main thread; stretching both sides equally would
    preserve every ratio and prove nothing (the test's nap would grow to 0.9s
    and the loop's ``wait(0.05)`` to 0.2s, still fitting six checks).

    **2. the worker does not get scheduled at all** (``start_delay``). This is
    the axis waits cannot reach: a thread that has been started but has not run
    its first bytecode yet performs *no* waits to stretch, so no amount of
    ``wait_delay`` makes it late. A test that naps 50ms and then expects a
    freshly started thread to have finished its work passes on an idle machine
    and fails on a busy one — the "slept 0.05s for the stderr drain thread" shape
    that ``tests/test_core.py`` had, and which the injection turned into a real
    race in ``TunnelManager.connect`` (the drain thread read ``self.process``,
    which the session's ``finally`` had already cleared).

    **...and deliberately no third axis for "the worker got no CPU at all".**
    That was tried and measured — a busy Python thread competing for the GIL,
    plus a duty-cycled variant — and it is both worse at finding bugs and far
    more expensive. On this suite, one such thread turned pytest's *collection*
    phase from 0.43s into 30s (two threads: 68s): GIL contention adds up to
    ``sys.getswitchinterval()`` of latency to every reacquisition, and a suite
    performs thousands of those. And it did not fail the very test this whole
    mechanism exists for — a counter-scheduled worker still runs inside a 50ms
    nap, because that nap releases the GIL. What a hog adds on top of the two
    axes above is wall-clock cost, not coverage. The genuine "no spare CPU"
    failure — a worker that never reaches its first bytecode in time — is axis 2.

    Two primitives cover how a background thread paces itself: ``Event.wait``
    and ``time.sleep``. Clocks (``time.monotonic``) are deliberately left alone —
    moving those would corrupt deadlines instead of emulating load. A ``sleep(0)``
    is a yield rather than a wait, and load does not stretch it, so it stays.

    Scope, honestly: both axes emulate *a worker that is late*, which is the
    failure mode behind every flake this exists for. What they cannot express is a
    main-thread budget measured in work rather than in time — "this loop should
    have run 10_000 times in 0.2s", which is the one thing the rejected hog above
    *would* have caught, and only by an unpredictable factor. Don't read a green
    run here as "no timing assumptions left".
    """
    if injection.start_delay:
        # Instance-level ``run`` override: the delay happens *inside* the new
        # thread (the GIL is released during it, as when a ready thread waits for
        # a core), which is precisely "started, but not yet running".
        def slow_start(self: threading.Thread) -> None:
            real_run = self.run

            def delayed_run() -> None:
                _ORIGINAL_SLEEP(injection.start_delay)
                real_run()

            self.run = delayed_run
            _ORIGINAL_THREAD_START(self)

        threading.Thread.start = slow_start  # type: ignore[method-assign]

    if injection.wait_delay:
        main_thread = threading.main_thread()

        def slow_sleep(seconds: float) -> None:
            if seconds > 0 and threading.current_thread() is not main_thread:
                seconds += injection.wait_delay
            _ORIGINAL_SLEEP(seconds)

        def slow_wait(event: threading.Event, timeout: float | None = None) -> bool:
            if timeout is not None and threading.current_thread() is not main_thread:
                timeout += injection.wait_delay
            return _ORIGINAL_WAIT(event, timeout)

        time.sleep = slow_sleep
        threading.Event.wait = slow_wait
