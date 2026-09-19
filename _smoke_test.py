import dataclasses
import importlib.util
import sys
import threading
import time
import types

# --- Stub out ponte.config and ponte.core before importing the real modules ---
pkg = types.ModuleType("ponte"); pkg.__path__ = []
pkg2 = types.ModuleType("ponte.config")
pkg2.get_config = lambda: None

@dataclasses.dataclass
class _RC:
    max_retries: int
    base_delay: float
    max_delay: float
    backoff_factor: float
    jitter: bool
    # 必须与 ponte.config.RetryConfig 的字段保持同步：漏字段会让脚本在
    # RetryRunner.__init__ 里直接 AttributeError（曾经就是这样挂掉的）。
    stable_after: float = 0.0
pkg2.RetryConfig = _RC

@dataclasses.dataclass
class _HC:
    check_interval: float
    remote_check_enabled: bool
    remote_check_timeout: float
    max_check_interval: float = 300.0
pkg2.HealthConfig = _HC

pkg3 = types.ModuleType("ponte.core")
@dataclasses.dataclass
class _TM:
    process = None
    def connect(self): ...
    def test_connection(self, timeout): ...
    def check_remote_ports(self): ...
    def build_args(self): ...
pkg3.TunnelManager = _TM

class ProbeError(RuntimeError):
    """探针没跑成 → 状态未知（stub：名字必须与 ponte.core 保持同步）。"""
pkg3.ProbeError = ProbeError

sys.modules["ponte"] = pkg
sys.modules["ponte.config"] = pkg2
sys.modules["ponte.core"] = pkg3

def load(name, path):
    spec = importlib.util.spec_from_file_location("ponte." + name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ponte." + name] = mod
    spec.loader.exec_module(mod)
    return mod

retry = load("retry", "ponte/retry.py")
health = load("health", "ponte/health.py")

# ================= Test RetryRunner =================
class FakeManager:
    def __init__(self, connect_result=None):
        self.connect_result = connect_result
        self.calls = 0
    def connect(self):
        self.calls += 1
        # Simulate a tunnel that stays up briefly, then terminates. Looping on
        # runner._stop alone would deadlock: stop() is only called from the
        # event driver after connect() returns, so connect must self-return.
        deadline = time.monotonic() + 0.5
        while not runner._stop.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.connect_result

runner = retry.RetryRunner(_RC(max_retries=3, base_delay=0.1, max_delay=0.4,
                               backoff_factor=2.0, jitter=False))
mgr = FakeManager(connect_result=0)
events = []
def driver():
    for ev in runner.run(mgr):
        events.append((ev.type, ev.exit_code, ev.delay, ev.attempt, ev.error))
        if ev.type == retry.RetryEvent.RETRYING:
            runner.stop()
t = threading.Thread(target=driver)
t.start()
time.sleep(0.3)
t.join()
assert events[0][0] == retry.RetryEvent.CONNECTING, events
idx = [e[0] for e in events]
assert retry.RetryEvent.CONNECTED in idx
assert retry.RetryEvent.DISCONNECTED in idx
assert retry.RetryEvent.RETRYING in idx
assert retry.RetryEvent.MAX_RETRIES_REACHED not in idx, events
print("retry: basic connect/stop flow OK ->", [e[0] for e in events])

# ---- max_retries exhaustion ----
class FailMgr:
    def __init__(self): self.calls = 0
    def connect(self):
        self.calls += 1
        return None

runner2 = retry.RetryRunner(_RC(max_retries=2, base_delay=0.01, max_delay=0.1,
                                backoff_factor=2.0, jitter=False))
seq = [(e.type, e.attempt) for e in runner2.run(FailMgr())]
print("retry: exhaustion seq ->", seq)
types_ = [s for s, _ in seq]
assert types_.count(retry.RetryEvent.DISCONNECTED) == 3   # initial + 2 retries
assert seq[-1][0] == retry.RetryEvent.MAX_RETRIES_REACHED
assert types_.count(retry.RetryEvent.CONNECTED) == 3

# ---- max_retries == 0 -> retry forever; connect raises once then works ----
class FlakyMgr:
    def __init__(self): self.calls = 0
    def connect(self):
        self.calls += 1
        if self.calls == 1:
            raise ChildProcessError("auth failed")
        while not runner3._stop.is_set():
            time.sleep(0.01)
        return None

runner3 = retry.RetryRunner(_RC(max_retries=0, base_delay=0.01, max_delay=0.05,
                                backoff_factor=2.0, jitter=False))
seq3 = []
for ev in runner3.run(FlakyMgr()):
    seq3.append(ev)
    if ev.type == retry.RetryEvent.RETRYING and ev.attempt == 1:
        runner3.stop()
first_disc = seq3[1]
assert first_disc.type == retry.RetryEvent.DISCONNECTED and first_disc.error == "ChildProcessError: auth failed", first_disc
assert retry.RetryEvent.MAX_RETRIES_REACHED not in [e.type for e in seq3], seq3
print("retry: raise-on-error carried through OK ->", first_disc.error)

# ---- jitter produces value within [0, computed) ----
runner4 = retry.RetryRunner(_RC(max_retries=0, base_delay=5, max_delay=300, backoff_factor=2.0, jitter=True))
vals = [runner4._backoff_delay(3) for _ in range(200)]
cap = min(5 * 2.0 ** 3, 300)
assert all(0 <= v < cap for v in vals), (min(vals), max(vals), cap)
assert any(v > cap * 0.5 for v in vals), "expected spread"
print("retry: jitter in [0,%g) OK" % cap)

# ================= Test HealthChecker =================
class Proc:
    def __init__(self, alive): self.alive = alive
    def poll(self): return None if self.alive else 1

class TM2:
    def __init__(self, alive=True, ports="dict", fail_ports=False, probe_error=None):
        self._proc = Proc(alive)
        self.ports = ports
        self.fail_ports = fail_ports
        self.probe_error = probe_error
        self._timeout = None
    @property
    def process(self): return self._proc
    def check_remote_ports(self, **kw):
        self._timeout = kw.get("timeout")
        if self.probe_error is not None: raise ProbeError(self.probe_error)
        if self.fail_ports: raise ConnectionError("refused")
        if self.ports == "dict": return {23334: True, 17897: False}
        if self.ports == "list": return [23334, 17897]
        return None

hc = health.HealthChecker(TM2(alive=True, ports="dict"), _HC(60, True, 10))
s = hc.check()
assert s.process_alive is True and s.remote_ports == {23334: True, 17897: False}
assert s.all_healthy is False
assert s.error is None
assert s.timestamp > 0
assert hc.manager._timeout == 10, "timeout passed through"
print("health: dict check OK ->", str(s))

hc2 = health.HealthChecker(TM2(alive=True, ports="list"), _HC(60, True, 10))
s2 = hc2.check()
assert s2.remote_ports == {23334: True, 17897: True}
print("health: list normalization OK ->", s2.remote_ports)

hc3 = health.HealthChecker(TM2(alive=False, ports="dict"), _HC(60, True, 10))
s3 = hc3.check()
assert s3.process_alive is False and s3.all_healthy is False
assert s3.remote_ports == {23334: True, 17897: False}
print("health: dead process OK ->", s3.all_healthy)

hc4 = health.HealthChecker(TM2(alive=True, ports="dict", fail_ports=True), _HC(60, True, 10))
s4 = hc4.check()
assert s4.all_healthy is False and s4.error is not None and "ConnectionError" in s4.error
print("health: port check failure ->", s4.error)

# 探测连接失败 = 未知：不得报成“端口未监听”，且必须可判定为“不确定”。
hc6 = health.HealthChecker(TM2(probe_error="ssh 退出码 255"), _HC(60, True, 10))
s6 = hc6.check()
assert s6.remote_ports == {} and s6.all_healthy is False, s6
assert s6.remote_probe_error and "255" in s6.remote_probe_error
assert s6.conclusive is False and "unknown" in str(s6)
print("health: unanswered probe is unknown OK ->", s6.remote_probe_error)

hc5 = health.HealthChecker(TM2(alive=True, ports="bad"), _HC(60, False, 10))
s5 = hc5.check()
assert s5.remote_ports == {} and s5.all_healthy is True, s5
print("health: remote check disabled OK ->", s5.all_healthy)

# ---- local listeners of -L / -D ----
# 没有本地探测能力的 manager 自动跳过（不报错、不影响健康）。
assert s5.local_ports == {}, s5.local_ports

class TM3(TM2):
    def __init__(self, local):
        super().__init__(alive=True, ports="list")
        self._local = local
    def check_local_ports(self, **kw): return self._local

hc8 = health.HealthChecker(TM3({1080: False}), _HC(60, True, 10))
s8 = hc8.check()
assert s8.local_ports == {1080: False} and s8.all_healthy is False, s8
assert "local_ports" in str(s8)
print("health: local -L/-D listener check OK ->", s8.local_ports)

class TM4(TM2):
    def check_local_ports(self, **kw): raise ConnectionError("local probe boom")

s9 = health.HealthChecker(TM4(alive=True, ports="list"), _HC(60, True, 10)).check()
assert s9.all_healthy is False and "local port check failed" in (s9.error or ""), s9.error
print("health: local probe failure surfaced OK ->", s9.error)

# ---- check-interval backoff (pure function, deterministic) ----
# 健康检查失败会指数退避（上限 max_check_interval），正常时保持基础间隔。
assert health.HealthChecker._backoff_interval(60, 0, 300) == 60
assert health.HealthChecker._backoff_interval(60, 1, 300) == 120
assert health.HealthChecker._backoff_interval(60, 5, 300) == 300, "must be capped"
print("health: check-interval backoff OK (base 60s, cap 300s)")

# ---- run_loop ----
# 用「健康」的假 manager：失败会触发退避，间隔不再是 0.05s，断言会变成时序竞态。
hc6 = health.HealthChecker(TM2(alive=True, ports="list"), _HC(60, True, 10))
seen = []
stop = hc6.run_loop(interval=0.05, callback=lambda st: seen.append(st))
assert isinstance(stop, threading.Event)
time.sleep(0.3)
stop.set()
time.sleep(0.15)
assert len(seen) >= 3, len(seen)
assert all(isinstance(st, health.HealthStatus) for st in seen)
n = len(seen)
time.sleep(0.15)
assert len(seen) == n, (len(seen), n)
print("health: run_loop OK (%d callbacks, stopped cleanly)" % len(seen))

# ---- run_loop with raising callback ----
hc7 = health.HealthChecker(TM2(alive=True, ports="dict"), _HC(60, True, 10))
def bad_cb(st):
    raise ValueError("cb boom")
stop7 = hc7.run_loop(interval=0.02, callback=bad_cb)
time.sleep(0.15)
stop7.set()
assert isinstance(hc7.last_callback_error, ValueError)
print("health: callback error swallowed + recorded OK")

print("\nALL SMOKE TESTS PASSED")
