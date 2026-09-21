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

from _injection import START_DELAY_ENV, THREAD_CPU_ENV, THREAD_DELAY_ENV, active

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
#: 里面的字符**全部是 ASCII**（包括注释），这不是风格问题，是被 CI 实测出来的：非 UTF-8
#: 的机器上（C locale 腿，见 ci.yml）文件系统编码就是 ASCII，中文连 stderr 都写不出去，
#: 而作为 ``-c`` 的参数更直接——前一句注释曾让 ``os.posix_spawn`` 抛
#: ``UnicodeEncodeError: 'ascii' codec ...``：**argv 根本传不进去**。那时这个自检自己就
#: 成了它想找的那类环境依赖。
_NAP_PROBE = '''
import os
import threading
import time

import _injection

_injection.active()  # install the injection from this child's environment

seen = []


def worker():
    seen.append(1)


threading.Thread(target=worker, daemon=True).start()
time.sleep(float(os.environ["PROBE_NAP"]))
if not seen:
    raise SystemExit("worker did not run within the fixed nap")
'''


#: 第三条轴的探针："这段固定的 CPU 工作量应该在这个预算内跑完"——这正是头两条轴
#: 够不到的那类断言（它们模拟的是"谁什么时候跑到"，不是"这段时间能算多少"）。
#: 预算是按机器标定出来的**时长**（秒），不是固定的循环次数：固定次数在快机器上短到
#: 装不下几个竞争周期，效果就被抹平（实测在一个 CI runner 上只有 1.79，而本机 3.1）。
_PROBE_SECONDS = "0.3"
_PROBE_COMPETITORS = "3"
#: 同一档里重复几轮取最快的一次：外部负载只会把时间**拖长**，取最小值就是各档真实
#: 水平的一致估计，而不会把"机器正好忙"算成"这条轴起了作用"。
_PROBE_REPEATS = "2"
#: 刻意比 CI 用的 0.05s 夸张（同 ``_PROBE_START_DELAY``）：这条自检要在任何机器上
#: 确定性地分出高下，而不是复现 CI 的取值。
_PROBE_CPU_SHARE = "0.10"

#: 判据：被饿着的那次必须比两个基线都快这么多。实测的比值：本机 3.1，最坏一次 1.79
#: （CI 的 3.13/ubuntu 腿）；而"关掉注入"与"只拉长等待"几乎一样（约 1.0，最坏 1.2）。
#: 取 1.3 是**实测最坏值的一半以下**——这条自检自己也不能变成"只有机器够快才通过"的
#: 断言，否则它就是在重犯它要防的错。把 ``_burn`` 禁掉后比值落到 0.94，仍稳稳地在
#: 判据之下（已复现），所以放宽容度不等于失去灵敏度。
_MIN_SLOWDOWN = 1.3

#: 探针源码刻意全 ASCII：C locale 下（见 ci.yml 的 env-edges 腿）非 ASCII 连
#: ``-c`` 的 argv 都传不进子进程（``os.posix_spawn`` 抛 UnicodeEncodeError）。
_CPU_CONTENTION_PROBE = '''
import os
import threading
import time

import _injection

_injection.active()

unit = 200000
began = time.perf_counter()
total = 0
for i in range(unit):
    total += i * i
unit_seconds = max(time.perf_counter() - began, 1e-6)
work = max(unit, int(unit * float(os.environ["PROBE_SECONDS"]) / unit_seconds))

stop = threading.Event()


def worker():
    event = threading.Event()
    while not stop.is_set():
        event.wait(0.001)


for _ in range(int(os.environ["PROBE_COMPETITORS"])):
    threading.Thread(target=worker, daemon=True).start()

time.sleep(0.2)

best = None
for _ in range(int(os.environ["PROBE_REPEATS"])):
    began = time.perf_counter()
    total = 0
    for i in range(work):
        total += i * i
    elapsed = time.perf_counter() - began
    best = elapsed if best is None else min(best, elapsed)

stop.set()
print(best)
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


def _contention_probe(*, delay: str, cpu: str) -> float:
    """在子进程里跑一次固定 CPU 预算，返回它花掉的秒数。

    子进程自带开关（而不是继承本进程的），所以这条自检在**任何**配置下都有效——包括
    注入全关的普通矩阵腿里：它验证的是机制本身，而不是"CI 那一步恰好开着"。
    """
    env = os.environ.copy()
    for name in (
        "COV_CORE_SOURCE",
        "COV_CORE_CONFIG",
        "COV_CORE_DATAFILE",
        "PYTEST_CURRENT_TEST",
        THREAD_DELAY_ENV,
        START_DELAY_ENV,
        THREAD_CPU_ENV,
    ):
        env.pop(name, None)
    env[THREAD_DELAY_ENV] = delay
    env[THREAD_CPU_ENV] = cpu
    env["PROBE_SECONDS"] = _PROBE_SECONDS
    env["PROBE_COMPETITORS"] = _PROBE_COMPETITORS
    env["PROBE_REPEATS"] = _PROBE_REPEATS
    tests_dir = str(Path(__file__).parent)
    env["PYTHONPATH"] = tests_dir + os.pathsep + env.get("PYTHONPATH", "")
    done = subprocess.run(
        [sys.executable, "-c", _CPU_CONTENTION_PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return float(done.stdout.strip())


def test_cpu_share_slows_a_competing_thread_where_delays_cannot() -> None:
    """第三条轴的**独有**覆盖：同一段 CPU 工作量，在竞争者真的烧 CPU 时明显变慢。

    两个基线缺一不可，因为一个比值本身说明不了"是谁干的"：关掉注入那次给出这台机器
    本来多快；``delay`` 那次才是重点——**拉长等待做不到这件事**。睡觉的线程不占 CPU，
    所以"工作线程缺 CPU"这类断言在 delay 轴下照样通过，这正是第三条轴存在的理由。
    """
    quiet = _contention_probe(delay="0", cpu="0")
    delayed = _contention_probe(delay="0.15", cpu="0")
    starved = _contention_probe(delay="0", cpu=_PROBE_CPU_SHARE)
    assert starved >= max(quiet, delayed) * _MIN_SLOWDOWN, (
        f"quiet={quiet:.3f}s delayed={delayed:.3f}s starved={starved:.3f}s "
        f"(ratio={starved / max(quiet, delayed):.2f}, need {_MIN_SLOWDOWN})"
    )


def _thread_cpu_spent_in_a_wait(seconds: float, *, in_worker: bool) -> float:
    """一次 ``Event.wait(seconds)`` 花掉**本线程**多少 CPU 秒。

    ``time.thread_time()`` 而不是墙钟：这样断言是"花/不花 CPU"的类别差别，不随机器
    快慢漂移，也不会因为 runner 忙而误报。
    """
    spent: list[float] = []

    def run() -> None:
        before = time.thread_time()
        threading.Event().wait(seconds)
        spent.append(time.thread_time() - before)

    if in_worker:
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=5)
    else:
        run()
    assert spent, "the wait never completed"
    return spent[0]


def test_cpu_share_burns_worker_threads_and_not_the_main_thread() -> None:
    """第三条轴只让**工作线程**花 CPU；主线程那次等待一毫秒都不多。

    主线程这一半与第一条轴同理（见 ``_injection``）：把测试自己的时间也拿去竞争，会
    让"测试的预算"和"被测代码的预算"一起变，比例不变、什么都测不出来。
    """
    worker_cpu = _thread_cpu_spent_in_a_wait(0.02, in_worker=True)
    main_cpu = _thread_cpu_spent_in_a_wait(0.02, in_worker=False)
    if _INJECTION.thread_cpu:
        assert worker_cpu >= _INJECTION.thread_cpu * 0.6, worker_cpu
        assert main_cpu < _INJECTION.thread_cpu * 0.3, main_cpu
    else:
        assert worker_cpu < 0.02, worker_cpu
        assert main_cpu < 0.02, main_cpu


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
        assert "worker CPU share" not in header, header
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
