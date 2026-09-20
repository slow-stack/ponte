"""Guards for the latency injection itself (``PONTE_TEST_THREAD_DELAY``).

一个静默失效的守卫比没有守卫更糟：它会把"慢 runner"那条 CI 腿变成一场空跑，而
输出仍是绿的。所以这里直接测量机制，而不是相信开关的名字。
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

#: Mirrors ``tests/conftest.py``; read from the environment so this file does not
#: depend on how pytest happens to name the conftest module.
_DELAY = float(os.environ.get("PONTE_TEST_THREAD_DELAY", "0") or 0)

_PROBE_WAIT = 0.01


def _wait_in_worker() -> float:
    """Seconds a *worker* thread actually spends in ``Event.wait(0.01)``."""
    elapsed: list[float] = []

    def worker() -> None:
        started = time.monotonic()
        threading.Event().wait(_PROBE_WAIT)
        elapsed.append(time.monotonic() - started)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert elapsed, "worker thread never finished its wait"
    return elapsed[0]


def test_injection_slows_workers_only_when_the_switch_is_on() -> None:
    """开着时工作线程变慢、主线程不变；关着时两者都不受影响。

    主线程这一半是重点：把测试自己的 ``time.sleep`` 也拉长会保持所有比例，
    于是它测不出任何东西（见 conftest 里的说明）。
    """
    started = time.monotonic()
    threading.Event().wait(_PROBE_WAIT)
    main_elapsed = time.monotonic() - started
    worker_elapsed = _wait_in_worker()

    # `is` 而不是 `==`：开关是"开/关"两种情形，不是一条连续刻度。
    if _DELAY:
        assert worker_elapsed >= _PROBE_WAIT + _DELAY * 0.8, worker_elapsed
        assert main_elapsed < _PROBE_WAIT + _DELAY * 0.5, main_elapsed
    else:
        assert worker_elapsed < _PROBE_WAIT + 0.25, worker_elapsed
        assert main_elapsed < _PROBE_WAIT + 0.25, main_elapsed


def _conftest_header() -> str:
    """The header our own conftest contributes, via the module pytest loaded."""
    # 不调 pytestconfig.hook：那个钩子是 firstresult，返回的是**别的插件**的结果；
    # 也不 import：模块名取决于 pytest 的 import 模式，问 sys.modules 最稳。
    for name in ("conftest", "tests.conftest"):
        module = sys.modules.get(name)
        header = getattr(module, "pytest_report_header", None)
        if header is not None:
            return header()
    pytest.skip("conftest module is not loaded under this import mode")
    raise AssertionError("unreachable")  # pragma: no cover - pytest.skip never returns


def test_injection_is_announced_in_the_report_header() -> None:
    """横幅要出现在头部：静默生效的注入无法从日志里证明自己跑过。

    这条横幅也是 CI 那一步能自检的依据（job 会 grep 它，见 ci.yml）。
    """
    header = _conftest_header()
    if _DELAY:
        assert f"thread latency: +{_DELAY:g}s" in header, header
    else:
        assert "thread latency" not in header, header
