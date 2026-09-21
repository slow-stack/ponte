"""把"慢机器"搬进测试进程的可选负载注入。

为什么需要它：依赖时序的断言只会**随机**报红。main 上真的红过一次——"睡 0.3 秒后至少
3 次检查"拿到 2；而它既不是产品代码错，也不是 runner 抽风，而是**断言考的是"这台机器
够快"**。快机器上它永远绿，慢机器上它偶尔红，两种结果都不回答"被测代码对不对"。

于是这里做的事是：在需要的时候，把"慢"变成一个**开关**，让那类测试当场失败。它属于
测试基础设施而不是产品代码，但它也是唯一能被"证明"的部分——三条轴都有对应的自检
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
#: Seconds of *busy* work (GIL held) added to every worker sleep/wait.
THREAD_CPU_ENV = "PONTE_TEST_THREAD_CPU"

# The unpatched primitives, captured at import time: without these the injection
# would slow itself (and a thread start delay would be stretched by the wait
# delay, so the axes would stop being independent). ``perf_counter`` is captured
# for the same reason — the burn must measure its own deadline, not a patched one.
_ORIGINAL_SLEEP = time.sleep
_ORIGINAL_WAIT = threading.Event.wait
_ORIGINAL_THREAD_START = threading.Thread.start
_ORIGINAL_PERF_COUNTER = time.perf_counter


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


def _burn(seconds: float) -> None:
    """Hold the GIL for *seconds* of real work — the thing a sleeping thread never does.

    A busy loop rather than another ``sleep``: the point of this axis is that the
    worker *uses* the CPU, so it competes for the GIL with whatever else is
    running. Sleeping releases the GIL and therefore takes nobody's CPU, which is
    exactly why the delay axis cannot emulate this.
    """
    if seconds <= 0:
        return
    deadline = _ORIGINAL_PERF_COUNTER() + seconds
    while _ORIGINAL_PERF_COUNTER() < deadline:
        pass


@dataclass(frozen=True)
class Injection:
    """The load this process is asked to emulate. Zero means "off"."""

    wait_delay: float = 0.0
    start_delay: float = 0.0
    thread_cpu: float = 0.0

    def enabled(self) -> bool:
        return bool(self.wait_delay or self.start_delay or self.thread_cpu)

    def banner(self) -> str:
        """One line per enabled axis, so a green run can prove the guard was on."""
        lines = []
        if self.wait_delay:
            lines.append(
                f"thread latency: +{self.wait_delay:g}s injected into every worker sleep/wait"
            )
        if self.thread_cpu:
            lines.append(
                f"worker CPU share: +{self.thread_cpu:g}s of busy work per worker sleep/wait"
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
        _INSTALLED = Injection(
            _number_env(THREAD_DELAY_ENV),
            _number_env(START_DELAY_ENV),
            _number_env(THREAD_CPU_ENV),
        )
        if _INSTALLED.enabled():
            _apply(_INSTALLED)
    return _INSTALLED


def pytest_configure(config) -> None:  # noqa: ARG001 - 只为让 `-p _injection` 生效
    """Allow ``-p _injection`` to install the injection without a conftest."""
    active()


def _apply(injection: Injection) -> None:
    """Make the code under test slower than the test that watches it.

    A slow machine fails timing-dependent tests in three distinct ways, and each
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

    **3. the worker runs, but it is not alone on the CPU** (``thread_cpu``).
    The first two axes are both "a worker that is *late*", and both of them cost
    nothing but wall clock — which is the point, and also the limit: a thread
    that is merely asleep takes nobody's CPU, so a test that measures **how much
    work fits in a fixed budget** (rather than "did this happen yet") sails
    through both. Real contention looks different: something else is holding the
    CPU, so whoever else wants it waits for the GIL. ``thread_cpu`` adds real
    busy work *inside the worker's own sleep/wait*, which means two things at
    once — the worker's iteration takes longer, and while it burns, every other
    thread waits up to ``sys.getswitchinterval()`` to get the GIL back.

    Cost is what made this axis worth designing carefully. The obvious
    implementation — a free-running busy thread for the whole process — was tried
    and measured, and it is unusable: one such thread turned pytest's *collection*
    phase from 0.43s into 30s (two threads: 68s), because collection performs
    thousands of tiny GIL reacquisitions and each one can cost up to
    ``sys.getswitchinterval()``. Worse, it did not even fail the tests this
    mechanism exists for, since their workers sleep (releasing the GIL). Burning
    *proportionally to the worker's own activity* fixes both halves: the cost is
    bounded by the number of worker sleep/waits rather than by wall time (nothing
    burns during collection, or in the test body, or while injection is off), and
    the contention lands exactly on the threads that are supposedly competing.

    Scope, honestly: axis 3 emulates *another thread in this process* holding the
    CPU, which is what GIL contention is. It cannot express "this container got a
    fraction of a core" for a worker that never sleeps or waits — there is no
    Python-level hook for "every bytecode of that thread" that would not itself
    distort the measurement — nor OS-level effects such as a stalled filesystem
    or a cold CPU. Don't read a green run here as "no timing assumptions left".

    Two primitives cover how a background thread paces itself: ``Event.wait``
    and ``time.sleep``. Clocks (``time.monotonic``) are deliberately left alone —
    moving those would corrupt deadlines instead of emulating load. A ``sleep(0)``
    is a yield rather than a wait, and load does not stretch it, so it stays.
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

    if injection.wait_delay or injection.thread_cpu:
        main_thread = threading.main_thread()

        def slow_sleep(seconds: float) -> None:
            if seconds > 0 and threading.current_thread() is not main_thread:
                seconds += injection.wait_delay
                _burn(injection.thread_cpu)
            _ORIGINAL_SLEEP(seconds)

        def slow_wait(event: threading.Event, timeout: float | None = None) -> bool:
            if timeout is not None and threading.current_thread() is not main_thread:
                timeout += injection.wait_delay
                _burn(injection.thread_cpu)
            return _ORIGINAL_WAIT(event, timeout)

        time.sleep = slow_sleep
        threading.Event.wait = slow_wait
