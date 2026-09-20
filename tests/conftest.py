"""Shared pytest configuration.

Keeps the configuration layer hermetic: without the autouse fixture below a
test that sets ``--config``/``PONTE_CONFIG`` would leak that state into the
next test, and ``get_config()`` could pick up the developer's real
``~/.config/ponte/config.toml``.

It also installs the optional *load injection* that turns "this test only passes
on a fast machine" from a random CI failure into a deterministic one. The
machinery lives in ``tests/_injection.py`` so that it can also be installed
without a conftest (the guard's own self-check runs it in a child process); this
file only decides when it happens — before any test module, so that no thread
created by a test module is left uninstrumented.
"""

from __future__ import annotations

import pytest

from _injection import active
from ponte import config as config_module

#: What this session was asked to emulate; installing is idempotent, so this is
#: the one place the environment switches are read for the whole session.
_INJECTION = active()


def pytest_report_header() -> str:
    """Show where the config layer resolves to, which explains most failures."""
    header = "ponte config search path: " + " | ".join(config_module.config_search_paths())
    if _INJECTION.enabled():
        header += "\n" + _INJECTION.banner()
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
