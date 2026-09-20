"""Pure SSH tunnel logic — establish connection, forward traffic.

No retry, no daemon, no CLI. Just build the right SSH arguments and spawn
a subprocess. The caller is responsible for lifecycle management.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

from ponte.config import WILDCARD_HOSTS, Profile, TunnelConfig, get_config

__all__ = ["ProbeError", "TunnelManager", "port_is_open", "creation_flags"]

logger = logging.getLogger(__name__)

#: Seconds a killed probe gets to release its stdout pipe before we give up on
#: reading it (see :func:`_run_capture`).
_PROBE_KILL_GRACE = 2.0


class ProbeError(RuntimeError):
    """A probe could not be completed, so what it inspects is *unknown*.

    The distinction this exception exists for is "the probe ran and the answer
    was no" versus "the probe never got to ask". Only the first is evidence
    that a tunnel is down, and callers must not turn the second into a down
    verdict: a probe *connection* fails for reasons that say nothing about the
    tunnel (a shared/NATed uplink, provider-side connection rate limiting, a
    session reset), and reporting those as a closed port is how a health
    monitor ends up killing perfectly healthy tunnels.
    """


def creation_flags() -> int:
    """Return subprocess creation flags that suppress a console window.

    On Windows an SSH child spawned without ``CREATE_NO_WINDOW`` can pop a
    black console box (the same class of flicker this tool works hard to
    avoid). On POSIX the flag is meaningless, so return ``0``.

    ``create_no_window`` is resolved via ``getattr`` purely so that tests which
    monkeypatch ``sys.platform`` to ``"win32"`` on a POSIX host still work —
    the constant simply does not exist in ``subprocess`` there.

    This lives here (rather than as an inline expression at each call site)
    because ``sys.platform == "win32"`` is only understood by ``mypy`` as a
    platform guard in an ``if`` *statement*; the same check inside a
    conditional *expression* is type-checked on both branches and fails on
    Linux, where the constant is absent from typeshed.
    """
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


#: Default Windows ``ssh.exe`` locations, tried when ``ssh`` is not on PATH.
#: Git for Windows does not always add ``usr\bin`` to PATH, and the Windows
#: OpenSSH client lives under System32. Only *default* install locations are
#: listed — an earlier version also hardcoded a per-machine ``D:\Git\...`` path,
#: which is meaningless on any other computer.
_WINDOWS_SSH_FALLBACKS = (
    r"C:\Program Files\Git\usr\bin\ssh.exe",
    r"C:\Program Files (x86)\Git\usr\bin\ssh.exe",
    r"C:\Windows\System32\OpenSSH\ssh.exe",
)


def _run_capture(args: list[str], timeout: float) -> tuple[int | None, str]:
    """Run *args* and return ``(returncode, stdout)`` without ever stalling.

    Deliberately not ``subprocess.run``. On Windows that helper, once its
    *timeout* fires, kills the child and then blocks in ``communicate()``
    joining the stdout reader thread — and that join only returns when *every*
    write end of the pipe is closed. A long-lived process holding an inherited
    duplicate of the handle keeps the pipe open forever, so the call never
    comes back and neither does its timeout.

    That is not hypothetical: it froze the daemon's health monitor for hours
    while the tunnel itself was fine, because the first probe's pipe had been
    inherited by the SSH tunnel child (see :meth:`TunnelManager.connect`). The
    status file then sat on its last reading, ``ponte status`` kept saying
    "异常", and zombie-session recovery — which runs off health ticks — never
    fired at all.

    So the read is bounded twice: ``communicate(timeout)`` for the normal path,
    then a *short* second ``communicate`` after the kill. If even that fails to
    return (the handle is genuinely wedged elsewhere), give up on the output
    rather than on the caller: ``(None, "")`` reads as "probe failed", which is
    the conservative answer for both callers.
    """
    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        # stderr is not read by either caller; DEVNULL avoids a second pipe.
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        close_fds=True,
        creationflags=creation_flags(),
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or ""
    except subprocess.TimeoutExpired:
        logger.debug("probe timed out after %ss; killing %s", timeout, args[0])
        proc.kill()
        try:
            out, _ = proc.communicate(timeout=_PROBE_KILL_GRACE)
        except subprocess.TimeoutExpired:
            logger.warning(
                "probe pipe never closed after kill (leaked handle?); "
                "reporting the probe as failed",
            )
            return None, ""
        return proc.returncode, out or ""


def _windows_ssh_fallbacks() -> list[str]:
    """Return Windows ssh.exe candidates, including a per-user Git install."""
    candidates = list(_WINDOWS_SSH_FALLBACKS)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(
            os.path.join(local, "Programs", "Git", "usr", "bin", "ssh.exe")
        )
    return candidates


def _find_ssh(config: TunnelConfig | None = None) -> str:
    """Return the path to the ``ssh`` executable.

    Resolution order:

    1. On Windows, the config may specify an explicit path
       (``[windows] ssh_exe``); honour it if it exists.
    2. ``ssh`` found on ``PATH`` (Linux/macOS almost always, Git-for-Windows
       often adds it too).
    3. Windows fallbacks to common Git / OpenSSH installation paths.

    ``config`` is used to honour the Windows ``ssh_exe`` override. When not
    provided, the global config is loaded — callers that already hold a
    validated config (e.g. :class:`TunnelManager`) should pass it to avoid a
    second, possibly failing, config load.
    """
    cfg = config if config is not None else get_config()
    if sys.platform == "win32" and cfg.windows.ssh_exe:
        exe = cfg.windows.ssh_exe
        if os.path.isfile(exe):
            return exe
        logger.warning("Configured ssh_exe not found: %s, falling back to PATH", exe)

    found = shutil.which("ssh")
    if found:
        return found

    if sys.platform == "win32":
        for candidate in _windows_ssh_fallbacks():
            if os.path.isfile(candidate):
                return candidate
        logger.warning("ssh not found on PATH or common install paths; "
                       "trying 'ssh' verbatim")
    return "ssh"


class TunnelManager:
    """Manage a single SSH tunnel session (reverse, local and/or SOCKS).

    One manager drives exactly one SSH connection, i.e. one
    :class:`~ponte.config.Profile`. When the config holds several profiles the
    daemon builds one manager per profile and supervises them concurrently.

    Parameters:
        config: A validated :class:`TunnelConfig` (from ``ponte.config``).
        profile: Which profile to connect to. Defaults to the primary one
            (``config.profiles[0]``), which is what every single-tunnel caller
            wants.
    """

    def __init__(self, config: TunnelConfig, profile: Profile | None = None) -> None:
        self.config = config
        self.profile = profile if profile is not None else config.profiles[0]
        # Windows ``ssh_exe`` is a host-level setting, so the *global* config is
        # what _find_ssh needs — not the profile.
        self.ssh_exe = _find_ssh(self.config)
        self.process: subprocess.Popen | None = None
        # Session-duration bookkeeping, consumed by the retry layer to reset
        # its reconnect budget once a session has stayed up long enough.
        self._connected_at: float | None = None
        self._last_session_duration: float | None = None

    # -- Connection ---------------------------------------------------------

    def connect(self) -> int:
        """Spawn SSH and block until the session ends.

        Returns the exit code of the SSH process. Raises
        :class:`subprocess.SubprocessError` (or a subclass) if the process
        cannot be launched.

        Call :meth:`stop` from another thread to terminate the session
        gracefully.
        """
        args = self.build_args()
        logger.info("Launching: %s", " ".join(args))
        self.process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            # ``close_fds`` must stay on *even on Windows*. This child is
            # long-lived (hours), and on Windows an inheriting child keeps a
            # copy of every handle that is inheritable at spawn time. Windows
            # has no close-on-exec, so a sibling's pipe write end inherited
            # here outlives that sibling: whoever reads the pipe (e.g. the
            # health probe's ``communicate()``) then never sees EOF and blocks
            # forever. Suppressing a console window is the job of
            # ``creationflags`` below, not of handle inheritance.
            close_fds=True,
            creationflags=creation_flags(),
        )
        self._connected_at = time.monotonic()
        # Drain stderr on a daemon thread: the pipe can never fill up (which
        # would stall SSH), and disconnect reasons are logged in real time
        # instead of only after the session ends.
        threading.Thread(
            target=self._drain_stderr,
            name="ponte-ssh-stderr",
            daemon=True,
        ).start()
        try:
            try:
                returncode = self.process.wait()
            finally:
                self._last_session_duration = self.uptime
                self._connected_at = None
            return returncode
        finally:
            self.process = None

    def _drain_stderr(self) -> None:
        """Read the SSH child's stderr line by line until EOF.

        Runs on a daemon thread for the lifetime of the session. Prevents the
        ``stderr=PIPE`` buffer from filling up and logs server-side disconnect
        reasons (e.g. ``Connection to host closed by remote host``) as they
        happen, so a dropped tunnel is diagnosable even after the fact.
        """
        proc = self.process
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    logger.warning("SSH stderr: %s", line)
        except (ValueError, OSError) as exc:
            logger.debug("stderr reader stopped: %s", exc)

    def stop(self) -> None:
        """Terminate the running SSH process (if any)."""
        if self.process is not None and self.process.poll() is None:
            logger.info("Terminating SSH process (PID %d)", self.process.pid)
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("SSH did not exit, killing")
                self.process.kill()
                self.process.wait()

    def is_running(self) -> bool:
        """Return ``True`` if the SSH process is still alive."""
        if self.process is None:
            return False
        return self.process.poll() is None

    # -- Session duration ------------------------------------------------------

    @property
    def uptime(self) -> float:
        """Seconds the current SSH session has been up (``0.0`` when idle)."""
        if self._connected_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self._connected_at)

    @property
    def last_session_duration(self) -> float | None:
        """Duration in seconds of the most recently completed session.

        ``None`` until the first :meth:`connect` finishes. The retry layer
        uses this to decide whether a session was stable enough to reset its
        reconnect budget.
        """
        return self._last_session_duration

    # -- Argument building --------------------------------------------------

    def _connection_args(self, *, connect_timeout: int | None = None) -> list[str]:
        """Return the ``ssh`` invocation prefix that reaches the server.

        Every path that talks to the server — the tunnel itself, the login test
        the health loop and ``ponte test`` use, and the server-side port probe —
        starts from this one list. They each used to build it themselves, which
        is how a setting could reach the tunnel but not the checks that
        supervise it; ``[ssh] jump`` (``-J``) would have been exactly such a
        setting: the tunnel would come up through the bastion while every health
        check declared it dead.
        """
        cfg = self.profile.ssh
        args = [self.ssh_exe]

        # SSH options
        for key, value in cfg.options.as_pairs():
            args.extend(["-o", f"{key}={value}"])

        # Known hosts file
        if cfg.known_hosts_file:
            args.extend(["-o", f"UserKnownHostsFile={cfg.known_hosts_file}"])

        # Identity file. Optional: without it OpenSSH falls back to
        # ~/.ssh/config, ssh-agent and its default key locations, which is how
        # a machine whose plain ``ssh host`` already works stays that way.
        if cfg.identity_file:
            args.extend(["-i", cfg.identity_file])

        # Jump host(s). Handed to OpenSSH as -J rather than tunnelled by ponte:
        # ssh opens (and authenticates) the hop itself, so the bastion's own
        # user/key come from ~/.ssh/config just like the destination's do.
        if cfg.proxy_jump:
            args.extend(["-J", cfg.proxy_jump])

        # Port
        if cfg.port != 22:
            args.extend(["-p", str(cfg.port)])

        if connect_timeout is not None:
            args.extend(["-o", f"ConnectTimeout={connect_timeout}"])

        return args

    def build_args(self) -> list[str]:
        """Construct the full SSH command line as a list of strings.

        Every configured :class:`~ponte.config.Tunnel` contributes exactly one
        forwarding flag followed by its spec, so a mixed ``-R``/``-L``/``-D``
        set becomes a single connection. Example::

            ["ssh", "-o", "ServerAliveInterval=30", "-N",
             "-R", "23334:localhost:2222",
             "-L", "127.0.0.1:8080:db.internal:5432",
             "-D", "127.0.0.1:1080", "user@server-ip"]
        """
        args = self._connection_args()

        # No shell, just forwarding
        args.append("-N")

        # Forwarding rules: -R (server listens), -L (we listen), -D (SOCKS proxy)
        for tunnel in self.profile.tunnels:
            args.extend([tunnel.flag, tunnel.spec])

        # Destination
        args.append(self.profile.ssh.destination)
        return args

    # -- Health / diagnostics -----------------------------------------------

    def test_connection(self, timeout: int = 10) -> bool:
        """Run a quick ``ssh … echo OK`` to verify connectivity.

        Returns ``True`` if the server responds with "OK". Goes through the
        configured jump host, so this is the check that validates a *chain*
        rather than only its last link.
        """
        args = self._connection_args(connect_timeout=timeout)
        args.extend([self.profile.ssh.destination, "echo OK"])
        try:
            code, output = _run_capture(args, timeout=timeout + 5)
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("Connection test failed: %s", exc)
            return False
        return code == 0 and "OK" in output

    def check_remote_ports(self, timeout: int = 10) -> dict[int, bool]:
        """Connect to the server and check which ``-R`` ports are listening.

        The probe runs *on the server*. It prefers a pure-Python socket check
        (no external tools), falling back to ``ss``/``lsof``/``netstat`` for
        servers without python3. Returns a ``{port: is_listening}`` mapping.

        Only ``kind = "remote"`` tunnels have a server-side listening port, so
        ``-L``/``-D`` rules are skipped here (their local end is covered by the
        far cheaper :meth:`check_local_ports`). Returns ``{}`` — never a probe
        connection — when no remote tunnel is configured.

        Raises:
            ProbeError: the probe connection itself failed (ssh could not be
                spawned, exited non-zero, or its output never arrived), so the
                ports' state is *unknown* rather than closed. Every port in the
                returned mapping was actually observed on the server.
        """
        cfg = self.profile.ssh
        ports = {
            int(t.remote_port)
            for t in self.profile.tunnels
            if t.is_remote and t.remote_port is not None
        }
        if not ports:
            return {}

        port_literal = ", ".join(str(p) for p in sorted(ports))
        # python3 socket probe first (portable across server OSes), else
        # fall back to the common listener tools with `ss`-style output.
        remote_cmd = (
            "if command -v python3 >/dev/null 2>&1; then\n"
            "python3 - <<'PY'\n"
            "import socket\n"
            f"ports=[{port_literal}]\n"
            "open_ports=[]\n"
            "for p in ports:\n"
            "    s=socket.socket(); s.settimeout(1)\n"
            "    try:\n"
            "        s.connect(('127.0.0.1',p)); open_ports.append(p)\n"
            "    except OSError:\n"
            "        pass\n"
            "    finally:\n"
            "        s.close()\n"
            "print(' '.join(str(p) for p in open_ports))\n"
            "PY\n"
            "else\n"
            "(ss -tlnp 2>/dev/null || lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null || netstat -tln 2>/dev/null)\n"
            "fi"
        )

        args = self._connection_args(connect_timeout=timeout)
        args.extend([cfg.destination, remote_cmd])
        try:
            code, output = _run_capture(args, timeout=timeout + 5)
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("Remote port check could not run: %s", exc)
            raise ProbeError(
                f"探测连接失败（{exc}）：{port_literal} 的状态未知"
            ) from exc
        if code != 0:
            # ssh itself failed — auth, a connection reset, provider rate
            # limiting, or our own timeout kill. The check command never ran,
            # so nothing was learned about the ports. Reporting them closed
            # here is what turns a flaky probe into a forced reconnect.
            logger.debug("Remote port check: ssh exited %s", code)
            raise ProbeError(
                f"探测连接失败（ssh 退出码 {code}）：{port_literal} 的状态未知"
            )

        # python3 branch prints the open ports space-separated on one line.
        python_ports = {int(p) for p in output.split() if p.isdigit()}
        status: dict[int, bool] = {}
        for port in ports:
            # Either the python3 line named the port, or a tool listing
            # contains a ``:<port> `` token (e.g. ``*:23334 ``).
            in_tool_output = f":{port} " in output or f":{port}\n" in output
            status[port] = port in python_ports or in_tool_output
        return status

    def check_local_ports(self, timeout: float = 1.0) -> dict[int, bool]:
        """Check that the local listeners of ``-L`` / ``-D`` tunnels are up.

        A loopback TCP connect — no SSH, no external tool — so unlike the
        server-side probe it is cheap enough to run on every health tick. A
        SOCKS proxy (``-D``) accepts a plain TCP connection just like a
        ``-L`` forward does, so the same probe covers both.

        Returns a ``{port: is_listening}`` mapping; a wildcard bind address
        (``0.0.0.0`` / ``::``) is probed as loopback, which is reachable
        whenever the wildcard bind succeeded.
        """
        status: dict[int, bool] = {}
        for tunnel in self.profile.tunnels:
            if tunnel.is_remote:
                continue
            host = tunnel.local_host
            if host in WILDCARD_HOSTS:
                host = "127.0.0.1"
            status[tunnel.local_port] = port_is_open(host, tunnel.local_port, timeout)
        return status


def port_is_open(host: str, port: int, timeout: float) -> bool:
    """Return ``True`` if a TCP connect to ``host:port`` succeeds.

    Public because ``ponte doctor`` probes a jump host with exactly this — the
    one question a bastion check can answer locally.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
