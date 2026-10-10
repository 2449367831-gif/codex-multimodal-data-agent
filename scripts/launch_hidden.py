"""Start and supervise the local query stack without creating a console window.

This launcher is invoked by ``start_hidden.vbs`` with ``pythonw.exe``. The
short-lived bootstrap starts a detached supervisor; the supervisor keeps both
loopback services alive and restarts a service when its listener disappears.
Diagnostics are written to ``var/logs`` instead of stdout.
"""

from __future__ import annotations

from datetime import datetime, timezone
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKEND_PORT = 8010
DEFAULT_CHAT_PORT = 8021
DEFAULT_DATABASE = "ywzt_ods"
DEFAULT_CHAT_URL = "http://127.0.0.1:8021/"
SUPERVISOR_INTERVAL_SECONDS = 2.0
SUPERVISOR_RETRY_SECONDS = 5.0
SUPERVISOR_MAX_RETRY_SECONDS = 300.0
HEALTH_PROBE_TIMEOUT_SECONDS = 1.5
STARTUP_HEALTH_WAIT_SECONDS = 45.0
SERVICE_HEALTH_FAILURE_LIMIT = 10
SUPERVISOR_LOCK_PATH = PROJECT_ROOT / "var" / "run" / "service-supervisor.lock"
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

# The daily VBS entrypoint is launched outside the user's interactive shell.
# Keep the service configuration small and explicit so a stale test process or
# an IDE environment cannot silently redirect the agent to another database or
# another runtime directory.
RUNTIME_CONFIG_KEYS = frozenset(
    {
        "DORIS_EXECUTION_MODE",
        "DORIS_HOST",
        "DORIS_PORT",
        "DORIS_USER",
        "DORIS_PASSWORD",
        "DORIS_DATABASE",
        "AGENT_PROJECT_ROOT",
        "AGENT_RELEASE_ID",
        "AGENT_TRUSTED_RELEASE_SHA256",
        "AGENT_ALLOW_UNAPPROVED_CONTRACTS",
        "AGENT_QUERY_TIMEOUT_SECONDS",
        "AGENT_MAX_ROWS",
        "AGENT_MAX_SQL_LENGTH",
        "AGENT_MAX_RESPONSE_BYTES",
        "AGENT_SQL_ALLOWED_SCHEMAS",
        "AGENT_SQL_ALLOWED_CATALOGS",
        "AGENT_AUDIT_DB",
        "AGENT_HOST",
        "AGENT_PORT",
        "AGENT_BACKEND_PORT",
        "AGENT_CHAT_HOST",
        "AGENT_CHAT_PORT",
        "AGENT_CHAT_NETWORK_MODE",
        "AGENT_CHAT_ALLOWED_CIDRS",
        "AGENT_CHAT_BACKEND_URL",
        "AGENT_CHAT_IDENTITY_MODE",
        "AGENT_CHAT_IDENTITY_SECRET",
        "AGENT_CHAT_PRINCIPAL_PEPPER",
        "AGENT_CHAT_SESSION_DB_PATH",
        "AGENT_CHAT_SECURE_COOKIES",
        "AGENT_CHAT_MAX_CONVERSATIONS",
        "AGENT_CHAT_TIMEOUT_SECONDS",
        "AGENT_CHAT_TASK_TIMEOUT_SECONDS",
        "AGENT_MODEL_TURN_TIMEOUT_SECONDS",
        "AGENT_MODEL_RETRY_TIMEOUT_SECONDS",
        "AGENT_CHAT_MAX_CONCURRENT",
        "AGENT_CODEX_HOME",
        "AGENT_CODEX_BASE_URL",
        "AGENT_CODEX_MODEL",
        "AGENT_CODEX_MODEL_PROVIDER",
        "AGENT_CODEX_REASONING_EFFORT",
        "AGENT_CODEX_FALLBACK_MODEL",
        "AGENT_CHAT_MODEL",
        "AGENT_CHAT_MODEL_PROVIDER",
        "AGENT_CHAT_MODEL_BASE_URL",
        "AGENT_CHAT_REASONING_EFFORT",
        "AGENT_INSTALLATION_ID_PATH",
    }
)


def _dotenv_values() -> dict[str, str]:
    """Read simple KEY=VALUE entries from the project-local configuration."""

    path = PROJECT_ROOT / ".env"
    if not path.is_file():
        return {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _read_dotenv(environment: dict[str, str]) -> None:
    """Load project-local values without overriding an existing environment."""

    for key, value in _dotenv_values().items():
        environment.setdefault(key, value)


def _windows_user_environment() -> dict[str, str]:
    """Read only known service settings from the current user's environment."""

    if os.name != "nt":
        return {}
    try:
        import winreg
    except ImportError:  # pragma: no cover - only relevant on non-Windows Python
        return {}

    values: dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            for name in RUNTIME_CONFIG_KEYS:
                try:
                    value, value_type = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                if not isinstance(value, (str, int, float)):
                    continue
                text = str(value)
                if value_type == getattr(winreg, "REG_EXPAND_SZ", object()):
                    text = os.path.expandvars(text)
                values[name] = text
    except OSError:
        return {}
    return values


def _select_runtime_environment(
    process_environment: dict[str, str],
    user_environment: dict[str, str],
    dotenv_environment: dict[str, str],
) -> dict[str, str]:
    """Build a clean service environment with deterministic config precedence.

    Project ``.env`` is the explicit deployment file, followed by Windows
    user-level settings.  Known runtime keys inherited from an interactive
    process are discarded when neither source defines them; this prevents old
    QA runs from leaking their database and SQLite paths into the daily agent.
    """

    environment = dict(process_environment)
    for key in RUNTIME_CONFIG_KEYS:
        if key in dotenv_environment:
            environment[key] = dotenv_environment[key]
        elif key in user_environment:
            environment[key] = user_environment[key]
        else:
            environment.pop(key, None)
    return environment


def _int_env(environment: dict[str, str], name: str, default: int) -> int:
    try:
        value = int(environment.get(name, str(default)).strip())
    except (TypeError, ValueError):
        return default
    return value if 1024 <= value <= 65535 else default


def _port_is_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.25)
        return connection.connect_ex((host, port)) == 0


def _service_is_ready(port: int, host: str = "127.0.0.1") -> bool:
    """Require an actual ready response, not only a bound TCP listener."""

    return _service_probe(port, "/health", host)


def _service_is_live(port: int, host: str = "127.0.0.1") -> bool:
    """Restart on lost process liveness, never on an unavailable dependency."""

    return _service_probe(port, "/live", host)


def _service_probe(port: int, endpoint: str, host: str = "127.0.0.1") -> bool:

    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if not _port_is_open(port, probe_host):
        return False
    request = urllib.request.Request(
        f"http://{probe_host}:{port}{endpoint}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        # Bypass machine proxy settings: both services are intentionally local.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=HEALTH_PROBE_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read(64 * 1024).decode("utf-8"))
        return isinstance(payload, dict) and payload.get("ready") is True
    except (
        OSError,
        ValueError,
        TypeError,
        UnicodeError,
        json.JSONDecodeError,
        urllib.error.HTTPError,
        urllib.error.URLError,
    ):
        return False


def _retry_delay(failure_count: int) -> float:
    """Back off failed launches without making a healthy service slower."""

    if not isinstance(failure_count, int) or failure_count < 0:
        failure_count = 0
    return min(
        SUPERVISOR_MAX_RETRY_SECONDS,
        SUPERVISOR_RETRY_SECONDS * (2 ** min(failure_count, 6)),
    )


def _log(message: str) -> None:
    log_root = PROJECT_ROOT / "var" / "logs"
    try:
        log_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        with (log_root / "hidden-launcher.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{stamp} {message}\n")
    except OSError:
        # A logging failure must not turn a hidden launcher into a visible UI.
        pass


def _pythonw_path() -> Path:
    candidate = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if candidate.is_file():
        return candidate
    sibling = Path(sys.executable).with_name("pythonw.exe")
    if sibling.is_file():
        return sibling
    return Path(sys.executable)


def _creation_options() -> tuple[int, object | None]:
    if os.name != "nt":
        return 0, None
    # Codex/terminal hosts may place descendants in a kill-on-close job. Ask
    # Windows to break away from that job and create a new process group while
    # retaining the no-console flags. The fallback below handles machines that
    # disallow breakaway from the parent job.
    flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        | CREATE_NEW_PROCESS_GROUP
        | CREATE_BREAKAWAY_FROM_JOB
    )
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return flags, startup


def _popen_hidden(
    command: list[str],
    *,
    environment: dict[str, str],
    stdin: object,
    stdout: object,
    stderr: object,
) -> subprocess.Popen:
    """Start a child without inheriting a visible console or host job when possible."""

    flags, startup = _creation_options()
    kwargs = {
        "cwd": str(PROJECT_ROOT),
        "env": environment,
        "stdin": stdin,
        "stdout": stdout,
        "stderr": stderr,
        "creationflags": flags,
        "startupinfo": startup,
        "close_fds": True,
    }
    try:
        return subprocess.Popen(command, **kwargs)
    except OSError:
        if os.name != "nt" or not (flags & CREATE_BREAKAWAY_FROM_JOB):
            raise
        # Some enterprise policies deny CREATE_BREAKAWAY_FROM_JOB. The child
        # must still remain hidden, so retry with the other isolation flags.
        _log("process breakaway unavailable; using hidden process fallback")
        kwargs["creationflags"] = flags & ~CREATE_BREAKAWAY_FROM_JOB
        return subprocess.Popen(command, **kwargs)


def _start_service(
    *,
    name: str,
    port: int,
    module: str,
    environment: dict[str, str],
) -> int | None:
    if _port_is_open(port):
        _log(f"{name} already listening on 127.0.0.1:{port}")
        return None

    log_root = PROJECT_ROOT / "var" / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    stdout_path = log_root / f"{name}.out.log"
    stderr_path = log_root / f"{name}.err.log"
    bind_host = (
        environment.get("AGENT_CHAT_HOST", "127.0.0.1")
        if name == "chat"
        else "127.0.0.1"
    )
    command = [
        str(_pythonw_path()),
        "-m",
        "uvicorn",
        module,
        "--host",
        bind_host,
        "--port",
        str(port),
    ]
    child_environment = dict(environment)
    child_environment["PYTHONUNBUFFERED"] = "1"
    with stdout_path.open("a", encoding="utf-8") as stdout, stderr_path.open(
        "a", encoding="utf-8"
    ) as stderr:
        process = _popen_hidden(
            command,
            environment=child_environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
        )
    _log(f"started {name} on {bind_host}:{port} pid={process.pid}")
    return process.pid


def _terminate_managed_service(pid: int) -> None:
    """Stop only a PID previously returned by ``_start_service``."""

    if not isinstance(pid, int) or pid <= 0:
        return
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                startupinfo=startup,
                check=False,
            )
        except OSError:
            _log("managed service termination failed")
        return
    try:  # pragma: no cover - exercised only on non-Windows hosts
        os.kill(pid, 15)
    except OSError:
        pass


class _SupervisorLock:
    """Hold one cross-platform lock for the installation supervisor."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None
        self.mutex = None

    def acquire(self) -> bool:
        if os.name == "nt":
            # A named mutex is atomic across independently launched hidden
            # processes, including launches started from different shells.
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            ctypes.set_last_error(0)
            mutex_name = (
                "Local\\RoyalTigerQueryAgent-"
                + hashlib.sha256(str(PROJECT_ROOT).casefold().encode("utf-8")).hexdigest()[:16]
            )
            handle = kernel32.CreateMutexW(None, 1, mutex_name)
            if not handle:
                return False
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                return False
            self.mutex = (kernel32, handle)
            return True

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        self.handle = self.path.open("r+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - exercised only on non-Windows hosts
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.handle.close()
            self.handle = None
            return False
        return True

    def release(self) -> None:
        if self.mutex is not None:
            kernel32, handle = self.mutex
            try:
                kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
                kernel32.ReleaseMutex.restype = ctypes.c_int
                kernel32.ReleaseMutex(handle)
            except OSError:
                pass
            finally:
                kernel32.CloseHandle(handle)
                self.mutex = None
            return
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised only on non-Windows hosts
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.handle.close()
            self.handle = None


def _start_supervisor(environment: dict[str, str]) -> int:
    supervisor_path = Path(__file__).resolve()
    log_root = PROJECT_ROOT / "var" / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    stdout_path = log_root / "service-supervisor.out.log"
    stderr_path = log_root / "service-supervisor.err.log"
    command = [
        str(_pythonw_path()),
        str(supervisor_path),
        "--supervise",
        "--no-browser",
    ]
    with stdout_path.open("a", encoding="utf-8") as stdout, stderr_path.open(
        "a", encoding="utf-8"
    ) as stderr:
        process = _popen_hidden(
            command,
            environment=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
        )
    _log(f"started service supervisor pid={process.pid}")
    return process.pid


def _run_supervisor() -> int:
    lock = _SupervisorLock(SUPERVISOR_LOCK_PATH)
    try:
        if not lock.acquire():
            _log("service supervisor already running")
            return 0
        environment, backend_port, chat_port = _configure_environment()
        last_attempt: dict[str, float] = {}
        managed: dict[str, dict[str, float | int]] = {}
        health_failures: dict[str, int] = {}
        launch_failures: dict[str, int] = {}
        services = (
            ("query-backend", backend_port, "app.query_backend:app"),
            ("chat", chat_port, "app.chat_api:app"),
        )
        _log(
            f"service supervisor active backend_port={backend_port} chat_port={chat_port}"
        )
        while True:
            now = time.monotonic()
            for name, port, module in services:
                bind_host = (
                    environment.get("AGENT_CHAT_HOST", "127.0.0.1")
                    if name == "chat"
                    else "127.0.0.1"
                )
                probe_host = "127.0.0.1" if bind_host in {"0.0.0.0", "::"} else bind_host
                if not _port_is_open(port, probe_host):
                    # Keep one failed child under observation for the startup
                    # window. This prevents an import/configuration error from
                    # creating a new process every few seconds.
                    service = managed.get(name)
                    if service is not None:
                        started_at = float(service.get("started_at", now))
                        if now - started_at < STARTUP_HEALTH_WAIT_SECONDS:
                            continue
                        pid = service.get("pid")
                        if isinstance(pid, int):
                            _log(f"stopping unready {name} pid={pid}")
                            _terminate_managed_service(pid)
                        managed.pop(name, None)
                        launch_failures[name] = launch_failures.get(name, 0) + 1
                        last_attempt[name] = now

                    if now - last_attempt.get(name, 0.0) >= _retry_delay(
                        launch_failures.get(name, 0)
                    ):
                        try:
                            pid = _start_service(
                                name=name,
                                port=port,
                                module=module,
                                environment=environment,
                            )
                            if pid is not None:
                                managed[name] = {"pid": pid, "started_at": now}
                        except Exception as exc:  # pragma: no cover - hidden runtime path
                            _log(f"{name} restart failed error={type(exc).__name__}")
                            launch_failures[name] = launch_failures.get(name, 0) + 1
                        health_failures[name] = 0
                        last_attempt[name] = now
                    continue

                service = managed.get(name)
                if service is None:
                    # A listener that was not created by this supervisor is not
                    # ours to terminate, even if its health endpoint is bad.
                    continue
                if _service_is_live(port, probe_host):
                    health_failures[name] = 0
                    launch_failures[name] = 0
                    continue
                if now - float(service.get("started_at", now)) < STARTUP_HEALTH_WAIT_SECONDS:
                    continue
                failures = health_failures.get(name, 0) + 1
                health_failures[name] = failures
                if failures < SERVICE_HEALTH_FAILURE_LIMIT:
                    continue
                pid = service.get("pid")
                if isinstance(pid, int):
                    _log(f"restarting unhealthy {name} pid={pid}")
                    _terminate_managed_service(pid)
                managed.pop(name, None)
                health_failures[name] = 0
                launch_failures[name] = launch_failures.get(name, 0) + 1
                last_attempt[name] = now
            time.sleep(SUPERVISOR_INTERVAL_SECONDS)
    except KeyboardInterrupt:  # pragma: no cover - supervisor has no console
        _log("service supervisor stopped by signal")
        return 0
    except Exception as exc:  # pragma: no cover - pythonw has no console
        _log(f"service supervisor failed error={type(exc).__name__}")
        return 1
    finally:
        lock.release()


def _configure_environment() -> tuple[dict[str, str], int, int]:
    dotenv_environment = _dotenv_values()
    user_environment = _windows_user_environment()
    environment = _select_runtime_environment(
        dict(os.environ), user_environment, dotenv_environment
    )
    backend_port = _int_env(environment, "AGENT_BACKEND_PORT", DEFAULT_BACKEND_PORT)
    chat_port = _int_env(environment, "AGENT_CHAT_PORT", DEFAULT_CHAT_PORT)

    environment.setdefault("AGENT_PROJECT_ROOT", str(PROJECT_ROOT))
    environment.setdefault("DORIS_DATABASE", DEFAULT_DATABASE)
    environment.setdefault("AGENT_BUSINESS_TIMEZONE", "Asia/Shanghai")
    # DMS orders live in a separately governed schema on the same Doris FE.
    # Keep it explicit; never widen this to a wildcard.
    environment.setdefault(
        "AGENT_SQL_ALLOWED_SCHEMAS",
        f"{environment['DORIS_DATABASE']},dms_ods,information_schema",
    )
    environment.setdefault("AGENT_CHAT_IDENTITY_MODE", "installation")
    environment.setdefault("AGENT_CHAT_MAX_CONVERSATIONS", "1")
    environment.setdefault(
        "AGENT_INSTALLATION_ID_PATH", str(PROJECT_ROOT / "var" / "installation-identity.json")
    )
    environment.setdefault(
        "AGENT_CHAT_SESSION_DB_PATH", str(PROJECT_ROOT / "var" / "chat-sessions.sqlite3")
    )
    environment.setdefault("AGENT_AUDIT_DB", str(PROJECT_ROOT / "var" / "audit.sqlite3"))
    environment.setdefault("AGENT_CODEX_HOME", str(PROJECT_ROOT / "var" / "codex-home"))

    # The model defaults mirror the single-department deployment contract.
    environment.setdefault("AGENT_CHAT_MODEL", "gpt-6")
    environment.setdefault("AGENT_CHAT_REASONING_EFFORT", "low")
    environment.setdefault("AGENT_MODEL_TURN_TIMEOUT_SECONDS", "180")
    environment.setdefault("AGENT_MODEL_RETRY_TIMEOUT_SECONDS", "90")
    environment.setdefault("AGENT_CHAT_MODEL_PROVIDER", "agent_gateway")
    environment.setdefault("AGENT_CHAT_MODEL_BASE_URL", "http://192.168.110.9:8088")
    environment.setdefault("AGENT_CODEX_MODEL", "gpt-6")
    environment.setdefault("AGENT_CODEX_MODEL_PROVIDER", "agent_gateway")
    environment.setdefault("AGENT_CODEX_REASONING_EFFORT", "low")
    environment.setdefault("AGENT_CODEX_BASE_URL", "http://192.168.110.9:8088")

    # Keep an explicit deployment mode authoritative. For an unconfigured
    # local UI install, the application default is mock mode; once Doris
    # credentials/mode are supplied, the same launcher uses Doris directly.
    if "DORIS_EXECUTION_MODE" not in environment:
        has_doris_config = all(
            environment.get(key, "").strip()
            for key in ("DORIS_HOST", "DORIS_USER", "DORIS_PASSWORD")
        )
        environment["DORIS_EXECUTION_MODE"] = "doris" if has_doris_config else "mock"

    environment["AGENT_HOST"] = "127.0.0.1"
    environment["AGENT_PORT"] = str(backend_port)
    network_mode = environment.get("AGENT_CHAT_NETWORK_MODE", "loopback").strip().casefold()
    if network_mode not in {"loopback", "lan"}:
        network_mode = "loopback"
    environment["AGENT_CHAT_NETWORK_MODE"] = network_mode
    environment["AGENT_CHAT_HOST"] = (
        environment.get("AGENT_CHAT_HOST", "0.0.0.0")
        if network_mode == "lan"
        else "127.0.0.1"
    )
    environment["AGENT_CHAT_PORT"] = str(chat_port)
    environment["AGENT_CHAT_BACKEND_URL"] = f"http://127.0.0.1:{backend_port}"
    return environment, backend_port, chat_port


def _open_chat(chat_port: int) -> None:
    url = f"http://127.0.0.1:{chat_port}/"
    try:
        if hasattr(os, "startfile"):
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            webbrowser.open(url)
    except OSError as exc:
        _log(f"unable to open browser url={url!r} error={type(exc).__name__}")


def main(*, open_browser: bool = True) -> int:
    try:
        environment, backend_port, chat_port = _configure_environment()
        _start_supervisor(environment)
        deadline = time.monotonic() + STARTUP_HEALTH_WAIT_SECONDS
        while time.monotonic() < deadline:
            if _service_is_ready(backend_port) and _service_is_ready(
                chat_port, environment.get("AGENT_CHAT_HOST", "127.0.0.1")
            ):
                break
            time.sleep(0.25)
        if not (
            _service_is_ready(backend_port)
            and _service_is_ready(chat_port, environment.get("AGENT_CHAT_HOST", "127.0.0.1"))
        ):
            _log("hidden launch health wait expired; opening browser for diagnostics")
        if open_browser:
            _open_chat(chat_port)
        _log(f"hidden launch complete chat_url=http://127.0.0.1:{chat_port}/")
        return 0
    except Exception as exc:  # pragma: no cover - pythonw has no console
        _log(f"hidden launch failed error={type(exc).__name__}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start the local query stack without a console window")
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.supervise:
        raise SystemExit(_run_supervisor())
    raise SystemExit(main(open_browser=not arguments.no_browser))
