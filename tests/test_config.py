"""pytest tests for :mod:`ponte.config` (offline, uses tmp files)."""

from __future__ import annotations

import os

import pytest

from ponte.config import (
    ConfigError,
    ConfigNotFoundError,
    ConfigParseError,
    ConfigValidationError,
    JumpHop,
    TunnelConfig,
    daemon_paths_from_file,
    ensure_bindable,
    get_config,
    is_loopback_host,
    load_config,
)


def _write_toml(tmp_path, body: str):
    cfg = tmp_path / "config.toml"
    cfg.write_text(body, encoding="utf-8")
    return str(cfg)


def _toml_str(path: object) -> str:
    """Render a path as a TOML basic string (escape backslashes)."""
    return str(path).replace("\\", "\\\\")


def _minimal(tmp_path) -> str:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
port = 22
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
known_hosts_file = "{_toml_str(tmp_path / 'known_hosts')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
"""
    return _write_toml(tmp_path, body)


def test_load_minimal(tmp_path) -> None:
    cfg = load_config(_minimal(tmp_path))
    assert isinstance(cfg, TunnelConfig)
    assert cfg.ssh.host == "example.com"
    assert cfg.ssh.destination == "testuser@example.com"
    assert len(cfg.tunnels) == 1
    assert cfg.tunnels[0].remote_port == 23334
    # 缺省 daemon 段 → 平台默认 pid/log 非空
    assert cfg.daemon.pid_file
    assert cfg.daemon.log_file
    # 缺省 health 段 → max_check_interval 取默认值
    assert cfg.health.max_check_interval == 300.0


def test_health_max_check_interval_default() -> None:
    """``HealthConfig.max_check_interval`` defaults to 300s (backoff ceiling)."""
    from ponte.config import HealthConfig

    assert HealthConfig().max_check_interval == 300.0


def test_missing_tunnels_rejected(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
known_hosts_file = "{_toml_str(tmp_path / 'known_hosts')}"
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_missing_required_field(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
known_hosts_file = "{_toml_str(tmp_path / 'known_hosts')}"

[[tunnels]]
remote_port = 9999
local_host = "localhost"
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_missing_identity_file_rejected(tmp_path) -> None:
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'nope')}"
known_hosts_file = "{_toml_str(tmp_path / 'known_hosts')}"

[[tunnels]]
remote_port = 9999
local_host = "localhost"
local_port = 2222
"""
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_get_config_caches(tmp_path) -> None:
    a = get_config(_minimal(tmp_path))
    b = get_config(_minimal(tmp_path))
    assert a is b


def test_ssh_options_extra_keys(tmp_path) -> None:
    """未知的 ssh.options 键应进入 extra，而不是报错。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
known_hosts_file = "{_toml_str(tmp_path / 'known_hosts')}"

[ssh.options]
ServerAliveInterval = 15
CustomFlag = true

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
"""
    cfg = load_config(_write_toml(tmp_path, body))
    extra = dict(cfg.ssh.options.extra)
    assert extra == {"CustomFlag": "yes"}  # 布尔渲染成 yes/no
    assert cfg.ssh.options.ServerAliveInterval == 15


def test_tunnels_must_be_array(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
known_hosts_file = "{_toml_str(tmp_path / 'known_hosts')}"

[tunnels]
remote_port = 23334
local_host = "localhost"
local_port = 2222
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_daemon_custom_paths(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
known_hosts_file = "{_toml_str(tmp_path / 'known_hosts')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222

[daemon]
pid_file = "/tmp/custom.pid"
log_file = "/tmp/custom.log"
log_backup_count = 5
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.daemon.pid_file == "/tmp/custom.pid"
    assert cfg.daemon.log_file == "/tmp/custom.log"
    assert cfg.daemon.log_backup_count == 5


def test_service_section(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
known_hosts_file = "{_toml_str(tmp_path / 'known_hosts')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222

[service]
name = "mytunnel"
autostart = false
kill_timeout = 3
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.service.name == "mytunnel"
    assert cfg.service.autostart is False
    assert cfg.service.kill_timeout == 3.0


def test_windows_run_as_user(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222

[windows]
run_as = "user"
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.windows.run_as == "user"


def test_windows_run_as_invalid_rejected(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222

[windows]
run_as = "root"
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_windows_run_as_default_is_user(tmp_path) -> None:
    """默认 run_as=user：SYSTEM 计划任务读不到 ~/.ssh 密钥，不能做默认值。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.windows.run_as == "user"


def test_expand_tilde(tmp_path, monkeypatch) -> None:
    # Windows 用 USERPROFILE，POSIX 用 HOME；两处都设，保证跨平台。
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    body = """
[ssh]
host = "example.com"
user = "testuser"
identity_file = "~/id_rsa"
known_hosts_file = "~/known_hosts"

[[tunnels]]
remote_port = 9999
local_host = "localhost"
local_port = 2222
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert os.path.normpath(cfg.ssh.identity_file) == os.path.normpath(tmp_path / "id_rsa")
    assert os.path.isfile(cfg.ssh.identity_file)


def test_config_file_not_found(tmp_path) -> None:
    with pytest.raises(ConfigNotFoundError):
        load_config(tmp_path / "missing.toml")


def test_config_invalid_toml(tmp_path) -> None:
    cfg = tmp_path / "bad.toml"
    cfg.write_text("[ssh\nhost = \"x\"", encoding="utf-8")
    with pytest.raises(ConfigParseError):
        load_config(cfg)


def test_ssh_section_as_string_rejected(tmp_path) -> None:
    """When [ssh] is omitted and ssh = "..." is a string, parsing fails."""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
ssh = "not a table"
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 9999
local_host = "localhost"
local_port = 2222
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_port_out_of_range_rejected(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
port = 99999

[[tunnels]]
remote_port = 9999
local_host = "localhost"
local_port = 2222
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_required_string_empty_rejected(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = """
[ssh]
host = ""
user = "testuser"
identity_file = "x"

[[tunnels]]
remote_port = 9999
local_host = "localhost"
local_port = 2222
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_optional_string_type_error(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
known_hosts_file = 123

[[tunnels]]
remote_port = 9999
local_host = "localhost"
local_port = 2222
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_retry_number_validation(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 9999
local_host = "localhost"
local_port = 2222

[retry]
base_delay = -1
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_health_boolean_validation(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 9999
local_host = "localhost"
local_port = 2222

[health]
remote_check_enabled = "yes"
"""
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_coerce_unsupported_type() -> None:
    from ponte.config import _coerce
    with pytest.raises(ConfigValidationError):
        _coerce("x", float, "test")


# ---------------------------------------------------------------------------
# 文档承诺过的可调项（此前被静默忽略）
# ---------------------------------------------------------------------------


def test_retry_stable_after_parsed(tmp_path) -> None:
    """``[retry] stable_after`` 必须真的生效，而不是被解析器丢掉。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222

[retry]
stable_after = 15
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.retry.stable_after == 15.0


def test_health_max_check_interval_parsed(tmp_path) -> None:
    """``[health] max_check_interval`` 同上。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222

[health]
max_check_interval = 45
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.health.max_check_interval == 45.0


def test_unknown_key_is_reported_not_ignored(tmp_path) -> None:
    """拼错的键要产生警告，而不是无声无息。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
parsel_port = 9999

[retry]
base_dely = 3
"""
    cfg = load_config(_write_toml(tmp_path, body))
    joined = " ".join(cfg.warnings)
    assert "retry.base_dely" in joined
    assert "tunnels[0].parsel_port" in joined
    # 未知键只是警告，不应让加载失败
    assert cfg.retry.base_delay == 5.0


def test_ssh_options_extras_are_not_warned(tmp_path) -> None:
    """``[ssh.options]`` 故意支持任意键，不应产生警告。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[ssh.options]
Compression = "yes"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.warnings == ()


# ---------------------------------------------------------------------------
# 配置文件位置：--config / PONTE_CONFIG / 用户目录 / 包内旧位置
# ---------------------------------------------------------------------------


def test_search_paths_order(monkeypatch, tmp_path) -> None:
    from ponte.config import config_search_paths, user_config_path

    monkeypatch.setenv("PONTE_CONFIG", str(tmp_path / "env.toml"))
    explicit = tmp_path / "explicit.toml"
    paths = config_search_paths(explicit)
    assert paths[0] == os.path.abspath(explicit)
    assert paths[1] == os.path.abspath(tmp_path / "env.toml")
    assert paths[2] == os.path.abspath(user_config_path())


def test_set_config_path_overrides_env(monkeypatch, tmp_path) -> None:
    from ponte.config import config_search_paths, set_config_path

    set_config_path(tmp_path / "pinned.toml")
    monkeypatch.setenv("PONTE_CONFIG", str(tmp_path / "env.toml"))
    assert config_search_paths()[0] == os.path.abspath(tmp_path / "pinned.toml")
    set_config_path(None)
    assert config_search_paths()[0] == os.path.abspath(tmp_path / "env.toml")


def test_get_config_uses_env_var(monkeypatch, tmp_path) -> None:
    """不传路径时按 PONTE_CONFIG 找到文件。"""
    target = _minimal(tmp_path)
    monkeypatch.setenv("PONTE_CONFIG", target)
    cfg = get_config()
    assert cfg.source_path == os.path.abspath(target)
    assert cfg.ssh.host == "example.com"


def test_get_config_missing_lists_searched_paths(monkeypatch, tmp_path) -> None:
    """找不到文件时给出可执行的提示，而不是一句 'not found'。"""
    monkeypatch.setenv("PONTE_CONFIG", str(tmp_path / "nope.toml"))
    with pytest.raises(ConfigNotFoundError) as excinfo:
        get_config()
    message = str(excinfo.value)
    assert "ponte init" in message
    assert "nope.toml" in message


def test_init_config_writes_and_refuses_overwrite(monkeypatch, tmp_path) -> None:
    from ponte.config import init_config

    target = tmp_path / "cfg" / "config.toml"
    written = init_config(target)
    assert os.path.isfile(written)
    # 模板必须能被解析（占位符除外：identity_file 指向不存在的密钥）
    assert "YOUR_SERVER_IP" in open(written, encoding="utf-8").read()

    with pytest.raises(ConfigError):
        init_config(target)

    init_config(target, force=True)  # --force 覆盖不报错


def test_init_config_prefers_legacy_file(monkeypatch, tmp_path) -> None:
    """已存在的包内旧配置应被迁移，而不是用占位模板覆盖。"""
    from ponte import config as config_module
    from ponte.config import init_config

    legacy = tmp_path / "config.toml"
    legacy.write_text("# legacy\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "legacy_config_path", lambda: str(legacy))
    monkeypatch.setattr(
        config_module, "example_config_path", lambda: str(tmp_path / "template.toml")
    )

    target = tmp_path / "out" / "config.toml"
    init_config(target)
    assert target.read_text(encoding="utf-8") == "# legacy\n"


def test_user_config_path_is_absolute_and_named_config_toml() -> None:
    from ponte.config import user_config_path

    path = user_config_path()
    assert os.path.isabs(path)
    assert os.path.basename(path) == "config.toml"


def test_example_config_ships_with_package() -> None:
    """模板必须真正随包发布（曾经 config.toml 不在 wheel 里）。"""
    from ponte.config import example_config_path

    assert os.path.isfile(example_config_path())


# ---------------------------------------------------------------------------
# 隧道类型：kind = remote / local / dynamic（-R / -L / -D）
# ---------------------------------------------------------------------------


def _tunnels_config(tmp_path, tunnels: str) -> str:
    """一份最小可用配置，其中 ``[[tunnels]]`` 段由 *tunnels* 决定。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "testuser"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

{tunnels}
"""
    return _write_toml(tmp_path, body)


def test_tunnel_kind_defaults_to_remote(tmp_path) -> None:
    """不写 kind 的旧配置行为完全不变（-R）。"""
    tunnel = load_config(_minimal(tmp_path)).tunnels[0]
    assert tunnel.kind == "remote"
    assert tunnel.is_remote is True
    assert tunnel.flag == "-R"
    assert tunnel.spec == "23334:localhost:2222"
    assert tunnel.remote_host is None


def test_local_tunnel_parsed(tmp_path) -> None:
    """-L：本机监听 local_host:local_port，目标是服务器侧 remote_host:remote_port。"""
    path = _tunnels_config(
        tmp_path,
        """
[[tunnels]]
kind = "local"
local_port = 8080
remote_host = "db.internal"
remote_port = 5432
""",
    )
    tunnel = load_config(path).tunnels[0]
    assert tunnel.kind == "local"
    assert tunnel.flag == "-L"
    assert tunnel.local_host == "127.0.0.1"  # 省略时默认只绑本机
    assert tunnel.spec == "127.0.0.1:8080:db.internal:5432"


def test_local_tunnel_requires_remote_host(tmp_path) -> None:
    """-L 缺 remote_host 必须报错，而不是生成一条语义错误的命令。"""
    path = _tunnels_config(
        tmp_path,
        """
[[tunnels]]
kind = "local"
local_port = 8080
remote_port = 5432
""",
    )
    with pytest.raises(ConfigValidationError, match="remote_host"):
        load_config(path)


def test_dynamic_tunnel_parsed(tmp_path) -> None:
    """-D：只需一个本地监听端口，目标由客户端每次连接自行选择。"""
    path = _tunnels_config(
        tmp_path,
        """
[[tunnels]]
kind = "dynamic"
local_port = 1080
""",
    )
    tunnel = load_config(path).tunnels[0]
    assert tunnel.flag == "-D"
    assert tunnel.spec == "127.0.0.1:1080"
    assert tunnel.remote_port is None


def test_dynamic_tunnel_reports_ignored_remote_keys(tmp_path) -> None:
    """-D 上写 remote_* 是误解：记一条告警，而不是静默丢弃。"""
    path = _tunnels_config(
        tmp_path,
        """
[[tunnels]]
kind = "dynamic"
local_port = 1080
remote_host = "ignored.example"
remote_port = 9999
""",
    )
    cfg = load_config(path)
    assert cfg.tunnels[0].remote_host is None
    assert any("已忽略" in warning for warning in cfg.warnings)


def test_unknown_tunnel_kind_rejected(tmp_path) -> None:
    path = _tunnels_config(
        tmp_path,
        """
[[tunnels]]
kind = "socks"
local_port = 1080
""",
    )
    with pytest.raises(ConfigValidationError, match="kind"):
        load_config(path)


def test_remote_bind_address_is_opt_in(tmp_path) -> None:
    """-R 的 remote_host 是服务器侧绑定地址（GatewayPorts 场景），不写就不加。"""
    path = _tunnels_config(
        tmp_path,
        """
[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
remote_host = "0.0.0.0"
""",
    )
    assert load_config(path).tunnels[0].spec == "0.0.0.0:23334:localhost:2222"


def test_duplicate_remote_ports_rejected(tmp_path) -> None:
    """同一服务器端口写两遍会让 ssh 直接断开整条连接（ExitOnForwardFailure）。"""
    path = _tunnels_config(
        tmp_path,
        """
[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 3333
""",
    )
    with pytest.raises(ConfigValidationError, match="remote port 23334"):
        load_config(path)


def test_duplicate_local_listeners_rejected(tmp_path) -> None:
    """-L 与 -D 都在本机监听，端口撞车同样要提前拒绝。"""
    path = _tunnels_config(
        tmp_path,
        """
[[tunnels]]
kind = "local"
local_port = 8080
remote_host = "db.internal"
remote_port = 5432

[[tunnels]]
kind = "dynamic"
local_port = 8080
""",
    )
    with pytest.raises(ConfigValidationError, match="listen on"):
        load_config(path)


def test_same_port_on_both_sides_is_not_a_conflict(tmp_path) -> None:
    """服务器端口与本机端口是两个命名空间，同号不算冲突。"""
    path = _tunnels_config(
        tmp_path,
        """
[[tunnels]]
remote_port = 8080
local_host = "localhost"
local_port = 2222

[[tunnels]]
kind = "dynamic"
local_port = 8080
""",
    )
    cfg = load_config(path)
    assert [tunnel.kind for tunnel in cfg.tunnels] == ["remote", "dynamic"]


# ---------------------------------------------------------------------------
# profiles：一个配置里的多条 SSH 连接
# ---------------------------------------------------------------------------


def _profile_entry(tmp_path, name: str, tunnels: str | None = None) -> str:
    """A ``[[profiles]]`` entry for *name*, with one -R tunnel by default."""
    rules = tunnels if tunnels is not None else """
[[profiles.tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
"""
    return f"""
[[profiles]]
name = "{name}"

[profiles.ssh]
host = "{name}.example.com"
user = "u"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"
{rules}"""


def test_profiles_parsed_independently(tmp_path) -> None:
    """每个 profile 自带 ssh 与 tunnels，互不影响。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = _profile_entry(tmp_path, "web") + _profile_entry(
        tmp_path,
        "db",
        tunnels="""
[[profiles.tunnels]]
kind = "local"
local_port = 5432
remote_host = "db.internal"
remote_port = 5432
""",
    )
    cfg = load_config(_write_toml(tmp_path, body))

    assert cfg.profile_names == ["web", "db"]
    assert cfg.get_profile().name == "web", "缺省取第一个 profile"
    assert cfg.get_profile("db").ssh.host == "db.example.com"
    assert cfg.get_profile("db").destination == "u@db.example.com"
    assert cfg.get_profile("db").tunnels[0].kind == "local"
    # 兼容属性仍指向第一个 profile（单隧道调用方不必索引列表）。
    assert cfg.ssh.host == "web.example.com"
    assert cfg.tunnels == cfg.profiles[0].tunnels


def test_get_profile_unknown_name_lists_configured(tmp_path) -> None:
    cfg = load_config(_minimal(tmp_path))
    with pytest.raises(ConfigError, match="unknown profile"):
        cfg.get_profile("nope")
    # 报错要列出已配置的名字，否则 --profile 手滑看起来就像隧道坏了。
    with pytest.raises(ConfigError, match="default"):
        cfg.get_profile("nope")


def test_profiles_reject_top_level_ssh(tmp_path) -> None:
    """两套写法混用是歧义的（顶层 tunnels 到底属于哪条连接？）。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = f"""
[ssh]
host = "example.com"
user = "u"
identity_file = "{_toml_str(tmp_path / 'id_rsa')}"

[[tunnels]]
remote_port = 1
local_host = "localhost"
local_port = 2

{_profile_entry(tmp_path, "web")}"""
    with pytest.raises(ConfigValidationError, match="cannot be combined"):
        load_config(_write_toml(tmp_path, body))


def test_duplicate_profile_name_rejected(tmp_path) -> None:
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = _profile_entry(tmp_path, "web") + _profile_entry(tmp_path, "web")
    with pytest.raises(ConfigValidationError, match="Duplicate profile name"):
        load_config(_write_toml(tmp_path, body))


@pytest.mark.parametrize("name", ["", "has space", "-leading", "sl/ash"])
def test_profile_name_is_validated(tmp_path, name) -> None:
    """名字会进 pid/状态文件名与 systemd 实例名，所以限定字符集。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, _profile_entry(tmp_path, name)))


def test_profile_needs_its_own_tunnel(tmp_path) -> None:
    """profile 不是空壳：没有 tunnel 的连接没有意义。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = _profile_entry(tmp_path, "web", tunnels="")
    with pytest.raises(ConfigValidationError, match="web"):
        load_config(_write_toml(tmp_path, body))


def test_same_remote_port_across_profiles_is_allowed(tmp_path) -> None:
    """不同服务器的同号端口不冲突：去重是按 profile 做的。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = _profile_entry(tmp_path, "web") + _profile_entry(tmp_path, "db")
    assert load_config(_write_toml(tmp_path, body)).profile_names == ["web", "db"]


def test_profile_unknown_key_reports_full_path(tmp_path) -> None:
    """profile 内部的拼写错误报到 profiles[0].ssh.x，而不是含糊的 ssh.x。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    entry = _profile_entry(tmp_path, "web").replace(
        'user = "u"', 'user = "u"\nhuost = "typo"'
    )
    cfg = load_config(_write_toml(tmp_path, entry))
    assert any("profiles[0].ssh.huost" in warning for warning in cfg.warnings)


def test_profile_invalid_identity_file_is_named(tmp_path) -> None:
    """多 profile 时报错必须点名是哪个 profile 的密钥不存在。"""
    (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
    body = _profile_entry(tmp_path, "web").replace(
        _toml_str(tmp_path / "id_rsa"), _toml_str(tmp_path / "missing_key")
    ) + _profile_entry(tmp_path, "db")
    with pytest.raises(ConfigValidationError, match="web"):
        load_config(_write_toml(tmp_path, body))


# ---------------------------------------------------------------------------
# [serve] —— 本地 HTTP 看板
# ---------------------------------------------------------------------------


def _serve_config_file(tmp_path, section: str) -> str:
    """一份最小可用配置，后面接上任意的 ``[serve]`` 段文本。"""
    return _tunnels_config(
        tmp_path,
        """
[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222

"""
        + section,
    )


def test_serve_defaults_to_loopback(tmp_path) -> None:
    """没有 [serve] 段时默认只监听本机：看板不需要用户做任何决定就应该是安全的。"""
    serve = load_config(_minimal(tmp_path)).serve
    assert serve.host == "127.0.0.1"
    assert serve.port == 8787
    assert serve.token == ""
    assert serve.loopback is True


def test_serve_section_is_parsed(tmp_path) -> None:
    path = _serve_config_file(
        tmp_path,
        """
[serve]
host = "192.168.1.5"
port = 9100
token = "s3cret"
refresh = 15
""",
    )
    serve = load_config(path).serve
    assert serve.host == "192.168.1.5"
    assert serve.port == 9100
    assert serve.token == "s3cret"
    assert serve.refresh == 15
    assert serve.loopback is False


def test_serve_non_loopback_without_token_is_rejected(tmp_path) -> None:
    """拒绝而不是警告：看板会列出服务器、用户与端口。"""
    path = _serve_config_file(
        tmp_path,
        """
[serve]
host = "0.0.0.0"
""",
    )
    with pytest.raises(ConfigValidationError, match="token"):
        load_config(path)


def test_serve_allows_an_explicit_non_loopback_bind_with_token(tmp_path) -> None:
    """带上令牌就允许对外，把选择权交给用户而不是替他决定。"""
    path = _serve_config_file(
        tmp_path,
        """
[serve]
host = "0.0.0.0"
token = "s3cret"
""",
    )
    assert load_config(path).serve.host == "0.0.0.0"


def test_serve_port_must_be_in_range(tmp_path) -> None:
    path = _serve_config_file(
        tmp_path,
        """
[serve]
port = 70000
""",
    )
    with pytest.raises(ConfigValidationError, match="serve.port"):
        load_config(path)


def test_serve_unknown_key_warns(tmp_path) -> None:
    """拼错的键要报出来，而不是默默用默认值。"""
    path = _serve_config_file(
        tmp_path,
        """
[serve]
hsot = "127.0.0.1"
""",
    )
    cfg = load_config(path)
    assert any("serve.hsot" in warning for warning in cfg.warnings)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.1.2.3", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("[::1]", True),
        ("", False),
        ("0.0.0.0", False),
        ("::", False),
        ("192.168.1.5", False),
        ("example.com", False),
    ],
)
def test_is_loopback_host(host, expected) -> None:
    """白名单判定：不认识的写法一律当成“对外暴露”。

    ``""`` 特别重要：``http.server`` 把空地址当成 *所有网卡*，所以它绝不是
    回环地址，不能因为“看起来是空的”就放行。
    """
    assert is_loopback_host(host) is expected


def test_ensure_bindable_treats_an_empty_host_as_exposed() -> None:
    with pytest.raises(ConfigValidationError, match="token"):
        ensure_bindable("", "")
    ensure_bindable("", "s3cret")


# ---------------------------------------------------------------------------
# [ssh] —— 省略 user / identity_file 即复用 ~/.ssh/config
# ---------------------------------------------------------------------------


def test_ssh_host_only_defers_to_ssh_config(tmp_path) -> None:
    """只写 host 必须能解析：user/identity_file 交给 ~/.ssh/config 与 ssh-agent。"""
    body = """
[ssh]
host = "myserver"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.ssh.host == "myserver"
    assert cfg.ssh.user == ""
    assert cfg.ssh.identity_file is None
    # 只有主机名，ssh 才能套用 Host 别名里的 User / IdentityFile
    assert cfg.ssh.destination == "myserver"
    assert cfg.ssh.port == 22


# ---------------------------------------------------------------------------
# [ssh] jump —— 经跳板机连接（ProxyJump / -J）
# ---------------------------------------------------------------------------

_JUMP_TUNNEL = """
[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
"""


def test_ssh_jump_single_hop(tmp_path) -> None:
    """jump 解析成 hop，之后原样交给 ssh -J；目标侧不受影响。"""
    body = '[ssh]\nhost = "10.0.0.9"\njump = "ops@bastion.example.com"\n' + _JUMP_TUNNEL
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.ssh.proxy_jump == "ops@bastion.example.com"
    assert cfg.ssh.first_hop == JumpHop(host="bastion.example.com", user="ops")
    assert cfg.ssh.destination == "10.0.0.9"


def test_ssh_jump_accepts_the_openssh_spelling_and_port(tmp_path) -> None:
    """proxy_jump 是同一个设置；host:port 写法保留端口（非 22 必须留在 -J 里）。"""
    body = (
        '[ssh]\nhost = "10.0.0.9"\nproxy_jump = "ops@bastion.example.com:2222"\n'
        + _JUMP_TUNNEL
    )
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.ssh.proxy_jump == "ops@bastion.example.com:2222"
    assert cfg.ssh.first_hop is not None
    assert cfg.ssh.first_hop.port == 2222


def test_ssh_jump_chain_keeps_every_hop(tmp_path) -> None:
    """多跳：逗号分隔，顺序保留，IPv6 字面量加回刮号。"""
    body = (
        '[ssh]\nhost = "10.0.0.9"\n'
        'jump = "ops@hop1:2222, root@[::1], hop3"\n' + _JUMP_TUNNEL
    )
    cfg = load_config(_write_toml(tmp_path, body))
    assert [hop.render() for hop in cfg.ssh.jumps] == [
        "ops@hop1:2222",
        "root@[::1]",
        "hop3",
    ]
    assert cfg.ssh.proxy_jump == "ops@hop1:2222,root@[::1],hop3"
    # 本机只可能直连第一跳，doctor 探测的就是它
    assert cfg.ssh.first_hop == JumpHop(host="hop1", user="ops", port=2222)


def test_ssh_jump_absent_is_none(tmp_path) -> None:
    """没写 jump 时为 None，且不会输出空的 -J。"""
    body = '[ssh]\nhost = "example.com"\n' + _JUMP_TUNNEL
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.ssh.jumps == ()
    assert cfg.ssh.proxy_jump is None
    assert cfg.ssh.first_hop is None


@pytest.mark.parametrize(
    "jump",
    [
        "ops@",            # 有空 user 没 host
        "bastion:",        # 冒号后没端口
        "a,,b",            # 空 hop
        "ops@bastion:0",   # 端口越界
        "ops@bastion:70000",
        "bastion example.com",
    ],
)
def test_ssh_jump_rejects_bad_hop(tmp_path, jump: str) -> None:
    body = f'[ssh]\nhost = "10.0.0.9"\njump = "{jump}"\n' + _JUMP_TUNNEL
    with pytest.raises(ConfigValidationError):
        load_config(_write_toml(tmp_path, body))


def test_ssh_jump_rejects_both_spellings(tmp_path) -> None:
    """两种写法同时出现是有歧义的，不能猜。"""
    body = (
        '[ssh]\nhost = "10.0.0.9"\n'
        'jump = "hop1"\nproxy_jump = "hop2"\n' + _JUMP_TUNNEL
    )
    with pytest.raises(ConfigValidationError, match="keep one"):
        load_config(_write_toml(tmp_path, body))


def test_ssh_jump_rejects_conflicting_options(tmp_path) -> None:
    """jump 与 [ssh.options] ProxyJump/ProxyCommand 说的是同一件事，直接拒绝。"""
    body = (
        '[ssh]\nhost = "10.0.0.9"\njump = "bastion"\n'
        '[ssh.options]\nProxyJump = "other"\n' + _JUMP_TUNNEL
    )
    with pytest.raises(ConfigValidationError, match="ProxyJump"):
        load_config(_write_toml(tmp_path, body))


def test_ssh_jump_works_inside_a_profile(tmp_path) -> None:
    """profiles 布局里同样可用，而每个 profile 各有自己的跳板机。"""
    body = (
        '[[profiles]]\nname = "web"\n'
        '[profiles.ssh]\nhost = "10.0.0.9"\njump = "bastion"\n'
        '[[profiles.tunnels]]\nremote_port = 23334\n'
        'local_host = "localhost"\nlocal_port = 2222\n'
    )
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.profiles[0].ssh.proxy_jump == "bastion"


def test_jumphop_render_and_destination() -> None:
    """render() 是给 ssh 看的：省略默认端口、给 IPv6 加刮号。"""
    assert JumpHop(host="bastion", user="ops").render() == "ops@bastion"
    assert JumpHop(host="bastion", user="ops").destination == "ops@bastion"
    assert JumpHop(host="bastion").render() == "bastion"
    assert JumpHop(host="bastion", port=2200).render() == "bastion:2200"
    assert JumpHop(host="::1", user="root", port=2200).render() == "root@[::1]:2200"


def test_ssh_user_without_identity_file(tmp_path) -> None:
    """写了 user 但不写 identity_file：目标带 user@，密钥仍由 ssh 解析。"""
    body = """
[ssh]
host = "myserver"
user = "deploy"

[[tunnels]]
remote_port = 23334
local_host = "localhost"
local_port = 2222
"""
    cfg = load_config(_write_toml(tmp_path, body))
    assert cfg.ssh.destination == "deploy@myserver"
    assert cfg.ssh.identity_file is None


# ---------------------------------------------------------------------------
# daemon_paths_from_file —— 配置坏掉时控制命令的兼底
# ---------------------------------------------------------------------------


def test_daemon_paths_from_file_recovers_a_config_that_fails_validation(tmp_path) -> None:
    """严格加载失败（缺隧道）仍要能取回 [daemon] 的 pid/log，否则 stop 被锁死。"""
    pid = tmp_path / "custom.pid"
    log = tmp_path / "custom.log"
    body = (
        "[daemon]\n"
        f'pid_file = "{pid.as_posix()}"\n'
        f'log_file = "{log.as_posix()}"\n'
        '\n[ssh]\nhost = "example.com"\n'
    )
    path = _write_toml(tmp_path, body)

    with pytest.raises(ConfigValidationError):
        load_config(path)

    assert daemon_paths_from_file(path) == (pid.as_posix(), log.as_posix())


def test_daemon_paths_from_file_falls_back_when_toml_is_broken(tmp_path) -> None:
    """连 TOML 都不合法时退回平台默认路径，而不是把异常抛给 stop。"""
    pid_file, log_file = daemon_paths_from_file(
        _write_toml(tmp_path, "not = toml = =\n")
    )
    assert os.path.isabs(pid_file) and os.path.basename(pid_file) == "ponte.pid"
    assert os.path.isabs(log_file) and os.path.basename(log_file) == "ponte.log"


def test_daemon_paths_from_file_falls_back_when_the_file_is_missing(tmp_path) -> None:
    """配置文件被删掉也一样：返回默认路径，不报错。"""
    pid_file, log_file = daemon_paths_from_file(tmp_path / "nope.toml")
    assert os.path.isabs(pid_file) and os.path.basename(pid_file) == "ponte.pid"
    assert os.path.isabs(log_file) and os.path.basename(log_file) == "ponte.log"
