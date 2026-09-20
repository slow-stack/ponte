"""Guards for the load injection itself (``tests/_injection.py``).

一条静默失效的守卫比没有守卫更糟：它会把"慢 runner"那条 CI 腿变成一场空跑，而输出
仍是绿的。所以这里做两件事，而不是相信开关的名字：

1. 直接**测量机制**——工作线程真的被拖慢了、主线程没有；
2. 用子进程跑一条**故意依赖时序的探针测试**，证明注入确实把它从绿变成红。第 2 条是
   整个机制的核心承诺，只有它做得到"证明"，前一条只是"测量"。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from _injection import START_DELAY_ENV, THREAD_DELAY_ENV, active

_INJECTION = active()

_PROBE_WAIT = 0.01

#: 子进程探针的固定睡眠与注入的启动延迟。取值刻意比 CI 用的 0.15s 夸张：这条自检必须
#: 在任何机器上都确定性地翻红，包括空闲的开发机——它证明的是机制，不是 CI 的取值。
_PROBE_NAP = "0.3"
_PROBE_START_DELAY = "1.0"

#: 探针就是"主线程睡一个固定秒数，然后假设刚起的工作线程已经跑过"——本仓库真的这么
#: 写过（``tests/test_core.py`` 里"给 drain 线程 50ms"）。用 ``-c`` 而不是落盘文件：
#: 子进程的启动成本要压在零点几秒，否则这条自检本身就成了套件里最慢的东西。
#:
#: 它的报错文本故意用 ASCII：子进程继承套件当时的环境，而在非 UTF-8 的机器上（CI 真
#: 有这种腿，见 ci.yml），一句中文连 stderr 都写不出去——那时这个自检自己就成了它想
#: 找的那类环境依赖。（不是猜测：本仓就是在 ``PYTHONIOENCODING=ascii`` 下把这条测试
#: 弄红过。）
_NAP_PROBE = '''
import os
import threading
import time

import _injection

_injection.active()  # 子进程自己装上负载注入，按环境变量决定装什么

seen = []


def worker():
    seen.append(1)


threading.Thread(target=worker, daemon=True).start()
time.sleep(float(os.environ["PROBE_NAP"]))
if not seen:
    raise SystemExit("worker did not run within the fixed nap")
'''


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


def _start_latency() -> float:
    """从 ``Thread.start()`` 返回，到线程体的第一行被执行，隔了多久。"""
    began: list[float] = []
    thread = threading.Thread(target=lambda: began.append(time.monotonic()), daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(timeout=5)
    assert began, "worker thread never ran its body"
    return began[0] - started


def test_injection_slows_workers_only_when_the_switch_is_on() -> None:
    """开着时工作线程变慢、主线程不变；关着时两者都不受影响。

    主线程这一半是重点：把测试自己的 ``time.sleep`` 也拉长会保持所有比例，
    于是它测不出任何东西（见 ``_injection`` 里的说明）。
    """
    started = time.monotonic()
    threading.Event().wait(_PROBE_WAIT)
    main_elapsed = time.monotonic() - started
    worker_elapsed = _wait_in_worker()

    # `is` 而不是 `==`：开关是"开/关"两种情形，不是一条连续刻度。
    if _INJECTION.wait_delay:
        assert worker_elapsed >= _PROBE_WAIT + _INJECTION.wait_delay * 0.8, worker_elapsed
        assert main_elapsed < _PROBE_WAIT + _INJECTION.wait_delay * 0.5, main_elapsed
    else:
        assert worker_elapsed < _PROBE_WAIT + 0.25, worker_elapsed
        assert main_elapsed < _PROBE_WAIT + 0.25, main_elapsed


def test_start_delay_holds_a_new_thread_before_its_body_runs() -> None:
    """第二条轴：线程已经被 start()、但还没跑第一行——等待被拉长管不到这种情况。"""
    latency = _start_latency()
    if _INJECTION.start_delay:
        assert latency >= _INJECTION.start_delay * 0.8, latency
    else:
        assert latency < 0.25, latency


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
    if _INJECTION.enabled():
        assert _INJECTION.banner() in header, header
    else:
        assert "thread latency" not in header, header
        assert "thread start delay" not in header, header


def _run_nap_probe(*, start_delay: str) -> subprocess.CompletedProcess:
    """Run the nap-based probe in a child process, with *start_delay* injected."""
    env = os.environ.copy()
    # 子进程自己跑解释器：把父进程的 coverage / 当前测试状态摘掉，免得互相污染。
    for name in (
        "COV_CORE_SOURCE",
        "COV_CORE_CONFIG",
        "COV_CORE_DATAFILE",
        "PYTEST_CURRENT_TEST",
    ):
        env.pop(name, None)
    env.pop(THREAD_DELAY_ENV, None)  # 这条自检只启用第二条轴，证明它单独就够用
    env[START_DELAY_ENV] = start_delay
    env["PROBE_NAP"] = _PROBE_NAP
    tests_dir = str(Path(__file__).parent)
    env["PYTHONPATH"] = tests_dir + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", _NAP_PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_injection_turns_a_timing_dependent_test_from_green_to_red() -> None:
    """守卫的核心承诺，两个方向都要看到：注入开启时探针必须红，关闭时必须绿。

    只验"开着时红"是不够的——一个把**所有**东西都弄红的坏注入同样满足它。
    """
    injected = _run_nap_probe(start_delay=_PROBE_START_DELAY)
    assert injected.returncode != 0, injected.stdout + injected.stderr
    assert "worker did not run within the fixed nap" in injected.stderr, injected.stderr

    clean = _run_nap_probe(start_delay="0")
    assert clean.returncode == 0, clean.stdout + clean.stderr
