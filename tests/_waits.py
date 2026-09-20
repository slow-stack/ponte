"""共用的确定性等待助手。

固定秒数的 ``sleep`` 断言的是"这台机器够快"，而不是被测行为：慢 runner 上它会随机
变红（main 上真的红过一次），快机器上它永远绿——两种结果都不回答"被测代码对不对"。
所以这里只做一件事：**等谓词成立，带截止时间**；等不到即失败，并带上现场。

为什么值得跨文件共用一个实现：每处自己发明"等一会儿"时，余量都会被写成某个具体秒数
（这个仓库里曾同时存在 0.05 / 0.15 / 0.3），而谁也说不清哪个是真约束、哪个只是习惯。
共用之后，唯一需要商量的是截止时间本身——它才是这些等待表达的约束。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

#: 轮询间隔。足够小以便及时看到副作用；又不为空转——空转要吃 GIL，会把等待者自己
#: 变成它本想模拟的那种负载，于是测出的是噪声而不是行为。
_POLL = 0.01

#: 默认截止时间。等待的**上界**才是它表达的约束：机器多慢都应该在这之内到达。
DEFAULT_TIMEOUT = 5.0


def wait_for(
    predicate: Callable[[], bool],
    describe: Callable[[], str],
    timeout: float = DEFAULT_TIMEOUT,
) -> None:
    """等到 *predicate* 成立，然后断言它成立。

    ``describe`` 是个函数而不是字符串：失败信息必须在**等过之后**才取，否则记录的是
    等待开始前那一刻的状态，只说明"当时还没到"，对定位毫无帮助。
    """
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(_POLL)
    assert predicate(), describe()


def wait_for_values(values: list, n: int, timeout: float = DEFAULT_TIMEOUT) -> None:
    """等到 ``values`` 至少有 ``n`` 个元素。"""
    wait_for(
        lambda: len(values) >= n,
        lambda: f"captured only {len(values)} values, need {n}",
        timeout,
    )


def join_thread(
    thread: threading.Thread,
    timeout: float = DEFAULT_TIMEOUT,
    what: str | None = None,
) -> None:
    """等 *thread* 结束，并断言它真的结束了。

    "停下来"这类断言该用 join 证明线程已死，而不是"睡一会儿看计数没变"——后者在慢
    机器上会通过（还没来得及变），在快机器上也可能通过（正好没变），两头都不作数。
    线程结束后计数不再变化，之后的遍历才是确定性的。
    """
    thread.join(timeout=timeout)
    assert not thread.is_alive(), f"{what or thread.name} still running after {timeout:g}s"
