"""Shared pytest configuration.

Keeps the configuration layer hermetic: without the autouse fixture below a
test that sets ``--config``/``PONTE_CONFIG`` would leak that state into the
next test, and ``get_config()`` could pick up the developer's real
``~/.config/ponte/config.toml``.

It also owns the optional *latency injection* that turns "this test only passes
on a fast machine" from a random CI failure into a deterministic one — see
:func:`_install_thread_delay` and ``PONTE_TEST_THREAD_DELAY``.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from ponte import config as config_module

#: Seconds added to every *worker-thread* sleep/wait. Unset or ``0`` disables the
#: injection entirely, so the normal suite is untouched.
_THREAD_DELAY_ENV = "PONTE_TEST_THREAD_DELAY"


def _thread_delay() -> float:
    """Seconds to inject into worker-thread sleeps and waits (``0`` = off)."""
    raw = os.environ.get(_THREAD_DELAY_ENV, "").strip()
    if not raw:
        return 0.0
    try:
        delay = float(raw)
    except ValueError:
        raise SystemExit(
            f"{_THREAD_DELAY_ENV}={raw!r} is not a number. Refusing to continue: "
            "this switch exists to make timing assumptions fail, so silently "
            "disabling it would hide exactly what it guards."
        ) from None
    if delay < 0:
        raise SystemExit(f"{_THREAD_DELAY_ENV} must not be negative: {raw!r}")
    return delay


def _install_thread_delay(delay: float) -> None:
    """Make the code under test slower than the test that watches it.

    "Slow machine" is never uniform. Every wait and sleep a *worker* performs
    gets queued behind whatever else the runner is doing, while the test's own
    ``time.sleep(0.3)`` stays exactly 0.3s — that asymmetry is what makes
    "slept 0.3s, expected 3 checks" fail on a loaded macOS runner and pass here.
    So the injection only slows threads that are not the main thread; stretching
    both sides equally would preserve every ratio and prove nothing (the test's
    nap would grow to 0.9s and the loop's ``wait(0.05)`` to 0.2s, still fitting
    six checks).

    Two primitives cover how a background thread paces itself: ``Event.wait``
    and ``time.sleep``. Clocks (``time.monotonic``) are deliberately left alone —
    moving those would corrupt deadlines instead of emulating load. A ``sleep(0)``
    is a yield rather than a wait, and load does not stretch it, so it stays.
    """
    main_thread = threading.main_thread()
    real_sleep = time.sleep
    real_wait = threading.Event.wait

    def slow_sleep(seconds: float) -> None:
        if seconds > 0 and threading.current_thread() is not main_thread:
            seconds += delay
        real_sleep(seconds)

    def slow_wait(event: threading.Event, timeout: float | None = None) -> bool:
        if timeout is not None and threading.current_thread() is not main_thread:
            timeout += delay
        return real_wait(event, timeout)

    time.sleep = slow_sleep
    threading.Event.wait = slow_wait


_DELAY = _thread_delay()
if _DELAY:
    _install_thread_delay(_DELAY)


def pytest_report_header() -> str:
    """Show where the config layer resolves to, which explains most failures."""
    header = "ponte config search path: " + " | ".join(config_module.config_search_paths())
    if _DELAY:
        header += f"\nthread latency: +{_DELAY:g}s injected into every worker sleep/wait"
    return header


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch):
    """Reset the config override, the environment override and the cache."""
    monkeypatch.delenv(config_module.CONFIG_ENV_VAR, raising=False)
    config_module.set_config_path(None)
    config_module.clear_config_cache()
    yield
    config_module.set_config_path(None)
    config_module.clear_config_cache()
