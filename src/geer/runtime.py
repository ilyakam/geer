from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from .assets import DEFAULT_MODEL_ID, AssetError, Workspace, model_alias, verify_assets
from .hardware import select_hardware_profile
from .pi_distribution import PI_REVISION, PI_VERSION, PiDistributionError, install_pi
from .prompt import geer_system_prompt
from .retrieval import (
    RETRIEVAL_SYSTEM_PROMPT,
    retrieval_mcp_config,
    retrieval_status,
)
from .skills import ensure_skills_directory, skill_paths
from .t3 import t3_status

DEFAULT_HOST = "127.0.0.1"


def default_port(uid: int | None = None) -> int:
    """Return a stable loopback port for the current macOS account."""
    return 8264 + ((os.getuid() if uid is None else uid) % 1000)


DEFAULT_PORT = default_port()
STARTUP_TIMEOUT = 600
CACHE_MAX_SIZE = "16GB"
HOT_CACHE_MAX_SIZE = "8GB"
OMLX_VERSION = "0.5.3"
OMLX_REVISION = "31d07a7fdbbb499d4520188fb7e8f7bdbfe4c770"
OMLX_ARCHIVE_URL = (
    f"https://github.com/jundot/omlx/archive/{OMLX_REVISION}.tar.gz"
)
OMLX_SPEC = f"omlx @ {OMLX_ARCHIVE_URL}"
RUNTIME_ARCHIVE_NAME = f"Geer-runtime-omlx-{OMLX_VERSION}-macOS-arm64.tar.gz"
RUNTIME_ARCHIVE_URL = (
    "https://github.com/ilyakam/geer/releases/download/0.1.0/"
    f"{RUNTIME_ARCHIVE_NAME}"
)
RUNTIME_ARCHIVE_BYTES = 318_842_382
RUNTIME_ARCHIVE_SHA256 = (
    "9ebb3c4f3788b7f774e3955fa0014db893f0294d1a2540511dd554f0d2d66922"
)
RUNTIME_INSTALLED_BYTES = 1_043_514_688
OMLX_OVERRIDES = (
    "mlx-lm @ https://github.com/ml-explore/mlx-lm/archive/"
    "ab1806e8f5d6aa035973af194a1b9198ab4754dc.tar.gz",
    "mlx-embeddings @ https://github.com/Blaizzy/mlx-embeddings/archive/"
    "32981fa4e8064ed664b52071789dd18271fe4206.tar.gz",
    "mlx-vlm @ https://github.com/Blaizzy/mlx-vlm/archive/"
    "78b96eb5462141447b9a6b4943ef553891da56dd.tar.gz",
    "dflash-mlx @ https://github.com/bstnxbt/dflash-mlx/archive/"
    "9ca002898b48e14c9727dec17299f497e8467870.tar.gz",
)
RUNTIME_COMPONENTS = ("omlx", "python", "runtime.json")
RUNTIME_PYTHON_PREFIX = "__GEER_RUNTIME_PYTHON_PREFIX__"


def endpoint(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    return f"http://{host}:{port}"


def server_endpoint(
    workspace: Workspace,
    host: str = DEFAULT_HOST,
    port: int | None = None,
) -> str:
    return endpoint(host, port if port is not None else configured_port(workspace))


def configured_port(workspace: Workspace) -> int:
    try:
        record = json.loads(workspace.server_endpoint.read_text())
        port = record["port"]
    except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, TypeError):
        return DEFAULT_PORT
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return DEFAULT_PORT
    return port


def configured_pid(workspace: Workspace) -> int | None:
    try:
        record = json.loads(workspace.server_endpoint.read_text())
        pid = record["pid"]
    except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    if not isinstance(pid, int) or pid <= 0:
        return None
    return pid


def _write_endpoint(workspace: Workspace, host: str, port: int, pid: int) -> None:
    workspace.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace.state.chmod(0o700)
    temporary = workspace.server_endpoint.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "host": host,
                "pid": pid,
                "port": port,
                "endpoint": endpoint(host, port),
                "updated_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    temporary.chmod(0o600)
    os.replace(temporary, workspace.server_endpoint)


def _available_port(host: str, preferred: int) -> int:
    candidates = (preferred, 0)
    for candidate in candidates:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, candidate))
            except OSError:
                continue
            return int(probe.getsockname()[1])
    raise AssetError("could not allocate an available loopback port for Geer")


def _server_lock(workspace: Workspace):
    workspace.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace.state.chmod(0o700)
    lock = workspace.server_lock.open("a+")
    workspace.server_lock.chmod(0o600)
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    return lock


def ensure_api_key(workspace: Workspace) -> str:
    try:
        key = workspace.api_key.read_text().strip()
    except FileNotFoundError:
        key = ""
    except OSError as error:
        raise AssetError(f"cannot read Geer API key from {workspace.api_key}: {error}") from error
    if key:
        return key

    workspace.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace.state.chmod(0o700)
    key = secrets.token_urlsafe(32)
    temporary = workspace.api_key.with_suffix(".tmp")
    try:
        temporary.write_text(f"{key}\n")
        temporary.chmod(0o600)
        os.replace(temporary, workspace.api_key)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise AssetError(f"cannot create Geer API key at {workspace.api_key}: {error}") from error
    return key


def bootstrap_runtime(workspace: Workspace) -> dict[str, str]:
    command = workspace.runtime / "omlx" / "bin" / "omlx"
    marker = workspace.runtime / "runtime.json"
    if command.is_file() and marker.is_file():
        try:
            state = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            state = {}
        if state.get("version") == OMLX_VERSION:
            return {
                "omlx": str(command),
                "version": OMLX_VERSION,
                "revision": OMLX_REVISION,
            }

    archive = _runtime_archive_path(workspace)
    temporary = workspace.runtime.with_name("runtime.installing")
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True, mode=0o700)
    try:
        _extract_runtime(archive, temporary)
        _replace_runtime_prefix(temporary, RUNTIME_PYTHON_PREFIX, str(temporary / "python"))
        probe = temporary / "omlx/bin/python"
        subprocess.run(
            [str(probe), "-c", "import mlx.core, omlx"],
            check=True,
            capture_output=True,
            text=True,
        )
        marker_data = {
            "version": OMLX_VERSION,
            "revision": OMLX_REVISION,
            "archive_sha256": RUNTIME_ARCHIVE_SHA256,
        }
        (temporary / "runtime.json").write_text(
            json.dumps(marker_data, indent=2, sort_keys=True) + "\n"
        )
        _replace_runtime_prefix(
            temporary,
            str(temporary / "python"),
            str(workspace.runtime / "python"),
        )
        _install_runtime_components(temporary, workspace.runtime)
    except (OSError, subprocess.CalledProcessError, tarfile.TarError) as error:
        shutil.rmtree(temporary, ignore_errors=True)
        raise AssetError(f"failed to install oMLX {OMLX_VERSION}: {error}") from error
    shutil.rmtree(temporary, ignore_errors=True)
    if not command.is_file():
        raise AssetError(f"oMLX installation did not create {command}")
    return {
        "omlx": str(command),
        "version": OMLX_VERSION,
        "revision": OMLX_REVISION,
    }


def _replace_runtime_prefix(root: Path, source: str, replacement: str) -> None:
    paths = [root / "omlx/pyvenv.cfg"]
    paths.extend((root / "python/lib/python3.13").glob("_sysconfigdata*.py"))
    for path in paths:
        text = path.read_text()
        path.write_text(text.replace(source, replacement))


def _install_runtime_components(staged: Path, runtime: Path) -> None:
    for name in RUNTIME_COMPONENTS:
        if not (staged / name).exists():
            raise OSError(f"runtime archive is missing {name}")

    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = runtime.with_name("runtime.previous")
    shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir(parents=True, mode=0o700)
    installed: list[str] = []
    try:
        for name in RUNTIME_COMPONENTS:
            destination = runtime / name
            if destination.exists() or destination.is_symlink():
                os.replace(destination, backup / name)
        for name in RUNTIME_COMPONENTS:
            os.replace(staged / name, runtime / name)
            installed.append(name)
    except OSError:
        for name in installed:
            destination = runtime / name
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            else:
                destination.unlink(missing_ok=True)
        for name in RUNTIME_COMPONENTS:
            saved = backup / name
            if saved.exists() or saved.is_symlink():
                os.replace(saved, runtime / name)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)


def _runtime_archive_path(workspace: Workspace) -> Path:
    configured = os.environ.get("GEER_RUNTIME_ARCHIVE")
    local_candidates = (
        Path(configured).expanduser() if configured else None,
        Path("~/Downloads").expanduser() / RUNTIME_ARCHIVE_NAME,
        workspace.root.parent / RUNTIME_ARCHIVE_NAME,
    )
    for candidate in local_candidates:
        if candidate is not None and candidate.is_file():
            _verify_runtime_archive(candidate)
            print(f"  Using {candidate}")
            return candidate

    workspace.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = workspace.cache / RUNTIME_ARCHIVE_NAME
    if destination.is_file():
        try:
            _verify_runtime_archive(destination)
            return destination
        except AssetError:
            destination.unlink()
    _download_runtime(destination)
    _verify_runtime_archive(destination)
    return destination


def _download_runtime(destination: Path) -> None:
    print(f"  Downloading runtime ({_human_bytes(RUNTIME_ARCHIVE_BYTES)})")
    temporary = destination.with_suffix(destination.suffix + ".partial")
    temporary.unlink(missing_ok=True)
    started = time.monotonic()
    downloaded = 0
    try:
        with urllib.request.urlopen(RUNTIME_ARCHIVE_URL) as response:
            total = int(response.headers.get("Content-Length") or RUNTIME_ARCHIVE_BYTES)
            with temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    downloaded += len(chunk)
                    elapsed = max(time.monotonic() - started, 0.001)
                    speed = downloaded / elapsed
                    remaining = max(total - downloaded, 0)
                    eta = remaining / speed if speed else 0
                    percent = downloaded / total * 100 if total else 0
                    print(
                        "\r  "
                        f"{percent:5.1f}%  {_human_bytes(downloaded)}/"
                        f"{_human_bytes(total)}  {_human_bytes(int(speed))}/s  "
                        f"ETA {_human_duration(eta)}",
                        end="",
                        flush=True,
                    )
        print()
        os.replace(temporary, destination)
    except (OSError, urllib.error.URLError) as error:
        temporary.unlink(missing_ok=True)
        raise AssetError(f"failed to download the Geer runtime: {error}") from error


def _verify_runtime_archive(path: Path) -> None:
    if RUNTIME_ARCHIVE_BYTES <= 0 or not RUNTIME_ARCHIVE_SHA256:
        raise AssetError("the Geer runtime manifest is incomplete")
    if path.stat().st_size != RUNTIME_ARCHIVE_BYTES:
        raise AssetError(f"runtime archive has the wrong size: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != RUNTIME_ARCHIVE_SHA256:
        raise AssetError(f"runtime archive failed checksum verification: {path}")


def _extract_runtime(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as bundle:
        root = destination.resolve()
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise AssetError("runtime archive contains an unsafe path")
        bundle.extractall(destination, filter="data")


def _human_bytes(value: int) -> str:
    if value <= 0:
        return "unknown size"
    return f"{value / 1024**2:.1f} MiB"


def _human_duration(seconds: float) -> str:
    rounded = max(int(seconds + 0.5), 0)
    minutes, remainder = divmod(rounded, 60)
    return f"{minutes}:{remainder:02d}"


def bundled_command(workspace: Workspace, name: str) -> str | None:
    candidate = workspace.root / "vendor" / "bin" / name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def omlx_command(workspace: Workspace) -> str:
    isolated = workspace.runtime / "omlx" / "bin" / "omlx"
    if isolated.is_file():
        return str(isolated)
    command = shutil.which("omlx")
    if command is None:
        raise AssetError("oMLX is not installed; run `geer runtime`")
    return command


def serve_command(
    workspace: Workspace,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> list[str]:
    manifest = verify_assets(workspace)
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise AssetError("the proof-of-concept server must bind to loopback")
    if not 1 <= port <= 65535:
        raise AssetError("port must be between 1 and 65535")
    model_root = Path(
        str(
            manifest.get(
                "linked_model",
                workspace.models / str(manifest.get("model_id", DEFAULT_MODEL_ID)),
            )
        )
    )
    return [
        omlx_command(workspace),
        "serve",
        "--model-dir",
        str(model_root),
        "--host",
        host,
        "--port",
        str(port),
        "--base-path",
        str(workspace.omlx_state),
        "--paged-ssd-cache-dir",
        str(cache_directory(workspace, manifest)),
        "--paged-ssd-cache-max-size",
        CACHE_MAX_SIZE,
        "--hot-cache-max-size",
        HOT_CACHE_MAX_SIZE,
        "--max-concurrent-requests",
        "1",
        "--memory-guard",
        "safe",
        "--no-hf-cache",
    ]


def run_server(
    workspace: Workspace,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> int:
    ensure_model_settings(workspace)
    command = serve_command(workspace, host, port)
    state = workspace.omlx_state
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    try:
        return subprocess.run(
            command,
            check=False,
            env=_server_environment(workspace),
            umask=0o077,
        ).returncode
    except KeyboardInterrupt:
        return 130


def start_server(
    workspace: Workspace,
    host: str = DEFAULT_HOST,
    port: int | None = None,
    timeout: float = STARTUP_TIMEOUT,
) -> dict[str, Any]:
    with _server_lock(workspace):
        return _start_server_locked(workspace, host, port, timeout)


def _start_server_locked(
    workspace: Workspace,
    host: str,
    requested_port: int | None,
    timeout: float,
) -> dict[str, Any]:
    manifest = verify_assets(workspace)
    api_key = ensure_api_key(workspace)
    automatic = requested_port is None
    candidate = configured_port(workspace) if automatic else requested_port

    for _attempt in range(4):
        base_url = endpoint(host, candidate)
        try:
            health = get_json(f"{base_url}/health", api_key=api_key)
        except AssetError:
            health = None
        if health is not None:
            try:
                pid = _read_managed_server_pid(workspace, candidate)
            except AssetError:
                if not automatic:
                    raise
            else:
                _validate_server_identity(
                    workspace,
                    base_url,
                    candidate,
                    manifest,
                    pid,
                    health,
                    api_key,
                )
                _write_endpoint(workspace, host, candidate, pid)
                return {
                    "server": "online",
                    "endpoint": base_url,
                    "port": candidate,
                    "pid": pid,
                    "started": False,
                }

        process = _existing_managed_process(workspace, candidate)
        started = process is None
        if process is None:
            allocated = _available_port(host, candidate)
            if allocated != candidate:
                if not automatic:
                    raise AssetError(
                        f"loopback port {candidate} is already in use; "
                        "omit `--port` to let Geer select another port"
                    )
                candidate = allocated
                base_url = endpoint(host, candidate)
            command = serve_command(workspace, host, candidate)
            ensure_model_settings(workspace)
            workspace.state.mkdir(parents=True, exist_ok=True, mode=0o700)
            workspace.state.chmod(0o700)
            with workspace.server_log.open("wb", buffering=0) as log:
                process = subprocess.Popen(
                    command,
                    env=_server_environment(workspace),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    umask=0o077,
                )
            workspace.server_log.chmod(0o600)
            workspace.server_pid.write_text(f"{process.pid}\n")
            workspace.server_pid.chmod(0o600)
            _write_endpoint(workspace, host, candidate, process.pid)

        deadline = time.monotonic() + timeout
        last_error = "server did not answer"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                workspace.server_pid.unlink(missing_ok=True)
                detail = _log_tail(workspace.server_log)
                if automatic and "Address already in use" in detail:
                    workspace.server_endpoint.unlink(missing_ok=True)
                    candidate = _available_port(host, 0)
                    break
                raise _server_start_error(
                    workspace,
                    f"Geer server exited with status {process.returncode}",
                )
            try:
                health = get_json(f"{base_url}/health", timeout=1, api_key=api_key)
                _validate_server_identity(
                    workspace,
                    base_url,
                    candidate,
                    manifest,
                    process.pid,
                    health,
                    api_key,
                )
            except AssetError as error:
                last_error = str(error)
                time.sleep(0.25)
            else:
                _write_endpoint(workspace, host, candidate, process.pid)
                return {
                    "server": "online",
                    "endpoint": base_url,
                    "port": candidate,
                    "pid": process.pid,
                    "started": started,
                    **({"log": str(workspace.server_log)} if started else {}),
                }
        else:
            if started:
                process.terminate()
            workspace.server_pid.unlink(missing_ok=True)
            raise _server_start_error(
                workspace,
                f"Geer server did not become healthy within {timeout:g} seconds: "
                f"{last_error}",
            )

    raise AssetError("could not start Geer after selecting four loopback ports")


def _existing_managed_process(
    workspace: Workspace,
    port: int,
) -> _PidProcess | subprocess.Popen[bytes] | None:
    for pid in _recorded_server_pids(workspace):
        if _managed_server_process(workspace, pid, port):
            return _PidProcess(pid)
    workspace.server_pid.unlink(missing_ok=True)
    return None


class _PidProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        os.kill(self.pid, signal.SIGTERM)


def _server_environment(workspace: Workspace) -> dict[str, str]:
    environment = os.environ.copy()
    environment["OMLX_API_KEY"] = ensure_api_key(workspace)
    return environment


def _read_managed_server_pid(workspace: Workspace, port: int) -> int:
    for pid in _recorded_server_pids(workspace):
        if _managed_server_process(workspace, pid, port):
            return pid
    raise AssetError(
        f"endpoint {endpoint(port=port)} is online without a matching "
        "Geer ownership record"
    )


def _recorded_server_pids(workspace: Workspace) -> tuple[int, ...]:
    candidates: list[int] = []
    try:
        candidates.append(int(workspace.server_pid.read_text().strip()))
    except (OSError, ValueError):
        pass
    endpoint_pid = configured_pid(workspace)
    if endpoint_pid is not None and endpoint_pid not in candidates:
        candidates.append(endpoint_pid)
    return tuple(pid for pid in candidates if pid > 0)


def _validate_server_identity(
    workspace: Workspace,
    base_url: str,
    port: int,
    manifest: dict[str, Any],
    pid: int,
    health: dict[str, Any],
    api_key: str,
) -> None:
    if not _managed_server_process(workspace, pid, port):
        raise AssetError(f"Geer PID {pid} is not the managed listener on port {port}")

    expected = {str(manifest["model_id"]), model_alias(manifest)}
    default_model = health.get("default_model")
    if default_model not in expected:
        raise AssetError(
            f"endpoint default model {default_model!r} does not match Geer model"
        )

    models = get_json(f"{base_url}/v1/models", timeout=2, api_key=api_key)
    data = models.get("data")
    if not isinstance(data, list):
        raise AssetError("endpoint model catalog is not a list")
    model_ids = {
        item.get("id")
        for item in data
        if isinstance(item, dict)
    }
    if not expected & model_ids:
        raise AssetError("endpoint model catalog does not contain the Geer model")


def _server_start_error(workspace: Workspace, message: str) -> AssetError:
    tail = _log_tail(workspace.server_log)
    if tail:
        return AssetError(
            f"{message}\nLast lines from {workspace.server_log}:\n{tail}"
        )
    return AssetError(f"{message}; see {workspace.server_log}")


def _log_tail(path: Path, *, lines: int = 20, bytes_limit: int = 16_384) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - bytes_limit))
            content = stream.read().decode(errors="replace")
    except OSError:
        return ""
    return "\n".join(content.splitlines()[-lines:])


def stop_server(
    workspace: Workspace,
    port: int | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    selected_port = configured_port(workspace) if port is None else port
    pid = next(
        (
            candidate
            for candidate in _recorded_server_pids(workspace)
            if _managed_server_process(workspace, candidate, selected_port)
        ),
        None,
    )
    if pid is None:
        workspace.server_pid.unlink(missing_ok=True)
        return {"server": "stopped", "stopped": False}

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            workspace.server_pid.unlink(missing_ok=True)
            return {"server": "stopped", "pid": pid, "stopped": True}
        time.sleep(0.1)
    raise AssetError(f"Geer server {pid} did not stop within {timeout:g} seconds")


def _managed_server_process(workspace: Workspace, pid: int, port: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "uid=,command="],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        return False
    output = result.stdout.strip()
    uid, separator, command = output.partition(" ")
    if not separator or not uid.isdigit() or int(uid) != os.getuid():
        return False
    command = command.strip()
    expected = str(workspace.runtime / "omlx" / "bin" / "omlx")
    recognized = (
        command == "omlx-server"
        or (command.startswith(expected) and " serve " in f" {command} ")
    )
    if not recognized:
        return False
    try:
        listener = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(pid), f"-iTCP:{port}", "-sTCP:LISTEN"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return True
    return listener.returncode == 0


def get_json(
    url: str,
    timeout: float = 5,
    api_key: str | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as error:
        raise AssetError(f"request failed for {url}: {error}") from error
    if not isinstance(result, dict):
        raise AssetError(f"expected a JSON object from {url}")
    return result


def post_json(
    url: str,
    payload: dict[str, Any],
    timeout: float,
    api_key: str | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            **(
                {"Authorization": f"Bearer {api_key}"}
                if api_key is not None
                else {}
            ),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise AssetError(f"request failed for {url}: HTTP {error.code}: {detail}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise AssetError(f"request failed for {url}: {error}") from error
    if not isinstance(result, dict):
        raise AssetError(f"expected a JSON object from {url}")
    return result


def runtime_status(
    workspace: Workspace,
    base_url: str,
    *,
    verify_hashes: bool = False,
) -> dict[str, Any]:
    manifest = verify_assets(workspace, verify_hashes=verify_hashes)
    cache = cache_directory(workspace, manifest)
    status: dict[str, Any] = {
        "assets": "ok",
        "model_id": manifest["model_id"],
        "omlx": omlx_command(workspace),
        "endpoint": base_url,
        "cache": {
            "directory": str(cache),
            "ssd_max_size": CACHE_MAX_SIZE,
            "hot_max_size": HOT_CACHE_MAX_SIZE,
        },
        "pi": {
            "config_directory": str(workspace.pi_config),
            "required_version": PI_VERSION,
            "required_revision": PI_REVISION,
        },
        "retrieval": retrieval_status(workspace),
        "t3": t3_status(workspace),
    }
    try:
        status["pi"]["executable"] = compatible_pi(workspace)
    except AssetError as error:
        status["pi"].update({"status": "unavailable", "error": str(error)})
    else:
        status["pi"].update({"status": "ready", "version": PI_VERSION})
    status["ready"] = status["pi"]["status"] == "ready" and status["t3"]["ready"] is True
    launcher = launcher_status(workspace)
    if launcher is not None:
        status["launcher"] = launcher
    try:
        api_key = ensure_api_key(workspace)
        status["health"] = get_json(f"{base_url}/health", api_key=api_key)
        status["models"] = get_json(f"{base_url}/v1/models", api_key=api_key)
    except AssetError as error:
        status["server"] = "offline"
        status["server_error"] = str(error)
    else:
        status["server"] = "online"
    return status


def doctor(workspace: Workspace, base_url: str) -> dict[str, Any]:
    return runtime_status(workspace, base_url, verify_hashes=True)


def snapshot_runtime(workspace: Workspace, base_url: str) -> dict[str, Any]:
    status = get_json(
        f"{base_url}/api/status",
        api_key=ensure_api_key(workspace),
    )
    fields = (
        "version",
        "uptime_seconds",
        "models_loaded",
        "models_loading",
        "default_model",
        "total_requests",
        "total_prompt_tokens",
        "total_completion_tokens",
        "total_cached_tokens",
        "cache_efficiency",
        "avg_prefill_tps",
        "avg_generation_tps",
        "model_memory_used",
        "model_memory_max",
    )
    record = {
        "captured_at": datetime.now(UTC).isoformat(),
        "endpoint": base_url,
        **{field: status.get(field) for field in fields},
    }
    _append_json_line(workspace.runtime_metrics, record)
    return record


def _completion_usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    fields = {
        "prompt_tokens", "completion_tokens", "total_tokens",
        "cached_tokens", "input_tokens", "output_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens",
    }
    usage = {
        name: count for name, count in value.items()
        if name in fields and type(count) is int and count >= 0
    }
    for name in ("prompt_tokens_details", "completion_tokens_details"):
        details = value.get(name)
        if isinstance(details, dict):
            counters = {
                key: count for key, count in details.items()
                if key in {
                    "cached_tokens", "reasoning_tokens", "audio_tokens",
                    "accepted_prediction_tokens", "rejected_prediction_tokens",
                } and type(count) is int and count >= 0
            }
            if counters:
                usage[name] = counters
    return usage


def canary(
    workspace: Workspace,
    base_url: str,
    prompt: str,
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    manifest = verify_assets(workspace)
    model = str(manifest["model_id"])
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "user", "content": prompt}],
    }
    started = time.perf_counter()
    created_at = datetime.now(UTC).isoformat()
    try:
        response = post_json(
            f"{base_url}/v1/chat/completions",
            payload,
            timeout,
            api_key=ensure_api_key(workspace),
        )
    except AssetError as error:
        _append_request(
            workspace,
            {
                "created_at": created_at,
                "model": model,
                "status": "error",
                "latency_seconds": round(time.perf_counter() - started, 6),
                "error_type": type(error).__name__,
            },
        )
        raise

    record = {
        "created_at": created_at,
        "model": model,
        "resolved_model": response.get("model"),
        "status": "ok",
        "latency_seconds": round(time.perf_counter() - started, 6),
        "usage": _completion_usage(response.get("usage")),
        "stop_reason": next(
            (
                choice.get("finish_reason")
                for choice in response.get("choices", [])
                if isinstance(choice, dict)
            ),
            None,
        ),
    }
    _append_request(workspace, record)
    return {"response": response, "metrics": record}


def pi_environment(
    workspace: Workspace,
    api_key: str | None = None,
    *,
    inherited: dict[str, str] | None = None,
) -> dict[str, str]:
    """Keep the local Pi process separate from ambient provider credentials."""
    source = os.environ if inherited is None else inherited
    prefixes = (
        "ANTHROPIC_", "CLAUDE_", "OPENAI_", "AZURE_", "AWS_", "GOOGLE_",
        "GCLOUD_", "COPILOT_", "HF_", "HUGGINGFACE_", "PI_",
    )
    environment = {
        name: value
        for name, value in source.items()
        if not name.startswith(prefixes)
        and not name.endswith(("_API_KEY", "_AUTH_TOKEN", "_OAUTH_TOKEN"))
        and name != "GEER_API_KEY"
    }
    environment.update({
        "PI_CODING_AGENT_DIR": str(workspace.pi_config),
        "PI_CODING_AGENT_SESSION_DIR": str(workspace.pi_config / "sessions"),
        "PI_OFFLINE": "1",
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_TELEMETRY": "0",
    })
    if api_key is not None:
        environment["GEER_API_KEY"] = api_key
    bypass = environment.get("NO_PROXY", environment.get("no_proxy", ""))
    bypass = ",".join(filter(None, (bypass, "127.0.0.1", "localhost", "::1")))
    environment["NO_PROXY"] = bypass
    environment["no_proxy"] = bypass
    return environment


def _pi_candidate(workspace: Workspace) -> str | None:
    override = os.environ.get("GEER_PI_BIN")
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise AssetError(f"GEER_PI_BIN is not an executable: {candidate}")
        return str(candidate)
    for candidate in (
        workspace.runtime / "pi-runtime" / "pi" / "pi",
        workspace.root / "build" / "pi-runtime" / "pi" / "pi",
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("pi")


def compatible_pi(workspace: Workspace, *, timeout: float = 10) -> str:
    """Probe an existing pinned Pi executable without downloading or starting a model."""
    command = _pi_candidate(workspace)
    if command is None:
        raise AssetError(
            f"Pi {PI_VERSION} is missing. Run `geer setup`, or stage the pinned "
            "development runtime with `uv run python tools/install_pi.py`."
        )
    try:
        result = subprocess.run(
            [command, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=pi_environment(workspace),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise AssetError(f"cannot run Pi at {command}: {error}") from error
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout)
    if match is None:
        raise AssetError(f"cannot determine Pi version at {command}")
    version = ".".join(match.groups())
    if version != PI_VERSION:
        raise AssetError(f"Pi {version} is unsupported; Geer requires pinned Pi {PI_VERSION}")
    return command


def bootstrap_pi(workspace: Workspace) -> dict[str, str]:
    """Verify or install the managed runtime during explicit setup."""
    if not os.environ.get("GEER_PI_BIN"):
        try:
            install_pi(workspace.runtime / "pi-runtime")
        except PiDistributionError as error:
            raise AssetError(str(error)) from error
    command = compatible_pi(workspace)
    return {
        "pi": command,
        "version": PI_VERSION,
        "revision": PI_REVISION,
        "config_directory": str(workspace.pi_config),
    }


def _private_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise AssetError(f"refusing to replace unexpected Pi configuration symlink: {path}")
    try:
        value = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError) as error:
        raise AssetError(f"cannot read Pi configuration from {path}: {error}") from error
    if not isinstance(value, dict):
        raise AssetError(f"Pi configuration must contain a JSON object: {path}")
    return value


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", prefix=f".{path.name}.", dir=path.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            temporary.chmod(0o600)
            stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def ensure_pi_config(
    workspace: Workspace,
    base_url: str,
    manifest: dict[str, Any] | None = None,
    *,
    total_memory_bytes: int | None = None,
) -> dict[str, Any]:
    manifest = manifest or verify_assets(workspace)
    config = workspace.pi_config
    if config.is_symlink() or (config.exists() and not config.is_dir()):
        raise AssetError(f"refusing to replace unexpected Pi config path: {config}")
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.chmod(0o700)
    profile = select_hardware_profile(
        total_memory_bytes,
        native_context_window=int(manifest.get("context_length", 262_144)),
    )
    model_id = str(manifest["model_id"])
    models = _private_json_object(config / "models.json")
    providers = models.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise AssetError("Pi models.json providers must contain a JSON object")
    providers["geer"] = {
        "baseUrl": f"{base_url.rstrip('/')}/v1",
        "api": "openai-completions",
        "apiKey": "${GEER_API_KEY}",
        "authHeader": True,
        "models": [{
            "id": model_id,
            "name": model_alias(manifest),
            "reasoning": True,
            "input": ["text"],
            "contextWindow": profile.max_context_window,
            "maxTokens": min(16_384, profile.max_context_window // 4),
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "compat": {
                "supportsStore": False,
                "supportsDeveloperRole": False,
                "supportsReasoningEffort": False,
                "supportsStrictMode": False,
                "maxTokensField": "max_tokens",
                "thinkingFormat": "qwen-chat-template",
            },
        }],
    }
    settings = _private_json_object(config / "settings.json")
    settings.update({
        "defaultProvider": "geer",
        "defaultModel": model_id,
        "defaultThinkingLevel": "off",
        "enabledModels": ["geer/*"],
        "enableInstallTelemetry": False,
        "enableAnalytics": False,
    })
    mcp = _private_json_object(config / "mcp.json")
    servers = mcp.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise AssetError("Pi mcp.json mcpServers must contain a JSON object")
    servers["semble"] = retrieval_mcp_config(workspace)["mcpServers"]["semble"]
    _write_private_json(config / "models.json", models)
    _write_private_json(config / "settings.json", settings)
    _write_private_json(config / "mcp.json", mcp)
    skills_directory = ensure_skills_directory(workspace)
    return {
        "config_directory": str(config),
        "provider": "geer",
        "model_id": model_id,
        "model_name": model_alias(manifest),
        "context_window": profile.max_context_window,
        "skills_directory": str(skills_directory),
    }


def pi_arguments(
    workspace: Workspace,
    arguments: list[str],
    manifest: dict[str, Any] | None = None,
    *,
    enable_tools: bool = True,
) -> list[str]:
    if any(argument in {"--version", "-v", "--help", "-h"} for argument in arguments):
        return arguments
    manifest = manifest or verify_assets(workspace)
    options = [
        "--provider", "geer",
        "--model", str(manifest["model_id"]),
        "--models", "geer/*",
        "--thinking", "off",
        "--no-approve",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
    ]
    system_prompt = geer_system_prompt(model_name=model_alias(manifest))
    if enable_tools:
        options.extend(["--extension", "builtin:mcp"])
        for path in skill_paths(workspace):
            options.extend(["--skill", str(path)])
        system_prompt = f"{system_prompt}\n\n{RETRIEVAL_SYSTEM_PROMPT}"
    else:
        options.append("--no-tools")
    options.extend(["--append-system-prompt", system_prompt])
    return [*options, *arguments]


def prepare_pi(
    workspace: Workspace,
    arguments: list[str],
    *,
    ensure_server: bool = False,
    host: str = DEFAULT_HOST,
    port: int | None = None,
    enable_tools: bool = True,
) -> tuple[list[str], dict[str, str]]:
    command = compatible_pi(workspace)
    if any(argument in {"--version", "-v", "--help", "-h"} for argument in arguments):
        return [command, *arguments], pi_environment(workspace)
    manifest = verify_assets(workspace)
    if ensure_server:
        started = start_server(workspace, host, port)
        base_url = str(started["endpoint"])
    else:
        base_url = server_endpoint(workspace, host, port)
    ensure_pi_config(workspace, base_url, manifest)
    environment = pi_environment(workspace, ensure_api_key(workspace))
    record_launcher_status(
        workspace,
        "ready",
        endpoint_url=base_url,
        detail="Starting Pi with the local Geer provider",
    )
    return [
        command, *pi_arguments(workspace, arguments, manifest, enable_tools=enable_tools),
    ], environment


def launch_pi(
    workspace: Workspace,
    arguments: list[str],
    *,
    ensure_server: bool = False,
    host: str = DEFAULT_HOST,
    port: int | None = None,
    enable_tools: bool = True,
) -> NoReturn:
    command, environment = prepare_pi(
        workspace, arguments, ensure_server=ensure_server, host=host, port=port,
        enable_tools=enable_tools,
    )
    os.umask(0o077)
    os.execvpe(command[0], command, environment)


def record_launcher_status(
    workspace: Workspace,
    status: str,
    *,
    endpoint_url: str | None = None,
    detail: str | None = None,
) -> None:
    record = {
        "schema_version": 1,
        "updated_at": datetime.now(UTC).isoformat(),
        "status": status,
        **({"endpoint": endpoint_url} if endpoint_url else {}),
        **({"detail": detail} if detail else {}),
    }
    workspace.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace.state.chmod(0o700)
    _write_private_json(workspace.launcher_log, record)


def launcher_status(workspace: Workspace) -> dict[str, Any] | None:
    try:
        record = json.loads(workspace.launcher_log.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def cache_directory(
    workspace: Workspace,
    manifest: dict[str, Any] | None = None,
) -> Path:
    manifest = manifest or verify_assets(workspace)
    identity = {
        "schema_version": 1,
        "model_id": manifest.get("model_id"),
        "architecture": manifest.get("architecture"),
        "model_type": manifest.get("model_type"),
        "quantization": manifest.get("quantization"),
        "files": [
            {
                "name": entry.get("name"),
                "bytes": entry.get("bytes"),
                "sha256": entry.get("sha256"),
            }
            for entry in manifest.get("files", [])
            if isinstance(entry, dict)
        ],
        "omlx_revision": OMLX_REVISION,
        "ssd_max_size": CACHE_MAX_SIZE,
        "hot_max_size": HOT_CACHE_MAX_SIZE,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = hashlib.sha256(encoded).hexdigest()
    cache = workspace.runtime / "caches" / fingerprint[:16]
    record = {
        **identity,
        "fingerprint": fingerprint,
        "directory": str(cache),
    }
    workspace.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_manifest = workspace.state / "cache.json"
    temporary = cache_manifest.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, cache_manifest)
    return cache


def ensure_model_settings(
    workspace: Workspace,
    *,
    total_memory_bytes: int | None = None,
) -> dict[str, Any]:
    manifest = verify_assets(workspace)
    model_id = str(manifest["model_id"])
    native_context_window = int(manifest.get("context_length", 262_144))
    hardware_profile = select_hardware_profile(
        total_memory_bytes,
        native_context_window=native_context_window,
    )
    path = workspace.omlx_model_settings
    try:
        current = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, json.JSONDecodeError) as error:
        raise AssetError(f"cannot read oMLX model settings from {path}: {error}") from error
    if not isinstance(current, dict):
        raise AssetError(f"oMLX model settings must contain a JSON object: {path}")
    models = current.setdefault("models", {})
    if not isinstance(models, dict):
        raise AssetError(f"oMLX model settings.models must contain a JSON object: {path}")
    available_models = {
        candidate.name for candidate in workspace.models.iterdir() if candidate.is_dir()
    }
    for unavailable_model in models.keys() - available_models:
        del models[unavailable_model]
    settings = models.setdefault(model_id, {})
    if not isinstance(settings, dict):
        raise AssetError(f"oMLX model settings for {model_id} must contain an object")
    settings["is_pinned"] = True
    settings["is_default"] = True
    settings["model_alias"] = model_alias(manifest)
    settings["max_context_window"] = hardware_profile.max_context_window
    settings["turboquant_kv_enabled"] = False
    current["version"] = 1

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    return current


def _append_request(workspace: Workspace, record: dict[str, Any]) -> None:
    _append_json_line(workspace.requests, record)


def _append_json_line(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
    path.chmod(0o600)
