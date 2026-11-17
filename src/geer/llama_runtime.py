"""Managed llama.cpp serving for the experimental full-vision Qwen profile."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .assets import AssetError, Workspace, verify_assets
from .hardware import select_hardware_profile
from .llama_distribution import (
    LLAMA_REVISION,
    LLAMA_VERSION,
    archive_path,
    install_llama,
    installed,
    llama_distribution_manifest,
    runtime_directory,
    verify_archive,
)

GENERATION_RESERVE = 4096
SWAP_ALLOWANCE_BYTES = 1024**3
POLL_SECONDS = 0.5


def is_llama(manifest: dict[str, Any]) -> bool:
    return manifest.get("format") == "gguf" or manifest.get("backend") == "llama.cpp"


def dependency_plan(workspace: Workspace) -> dict[str, Any]:
    manifest = llama_distribution_manifest(workspace)
    reusable = installed(workspace)
    cached = False
    try:
        verify_archive(archive_path(workspace), manifest)
        cached = True
    except (OSError, AssetError):
        pass
    download = 0 if reusable or cached else manifest["archive"]["bytes"]
    return {
        "runtime_download_bytes": download,
        "runtime_required_free_bytes": (0 if reusable else download + manifest["extracted_bytes"]),
        "runtime_reusable": reusable,
        "runtime_installed_bytes": manifest["extracted_bytes"],
    }


def bootstrap_runtime(workspace: Workspace) -> dict[str, str]:
    return {
        "llama_cpp": str(install_llama(workspace)),
        "version": LLAMA_VERSION,
        "revision": LLAMA_REVISION,
    }


def command(workspace: Workspace) -> str:
    if not installed(workspace):
        raise AssetError("llama.cpp is not installed or failed verification; run `geer setup`")
    return str(runtime_directory(workspace) / "llama-server")


def ensure_model_settings(
    workspace: Workspace,
    *,
    total_memory_bytes: int | None = None,
) -> dict[str, Any]:
    manifest = verify_assets(workspace)
    profile = select_hardware_profile(
        total_memory_bytes,
        family="qwen",
        native_context_window=int(manifest["context_length"]),
    )
    # llama.cpp settings are arguments, so activation needs no persistent sidecar.
    return {
        "backend": "llama.cpp",
        "max_context_window": profile.max_context_window,
        "generation_reserve": GENERATION_RESERVE,
    }


def serve_command(workspace: Workspace, host: str, port: int) -> list[str]:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise AssetError("the local server must bind to loopback")
    if type(port) is not int or not 1 <= port <= 65535:
        raise AssetError("port must be between 1 and 65535")
    manifest = verify_assets(workspace)
    settings = ensure_model_settings(workspace)
    model_root = Path(manifest["linked_model"])
    names = {entry["name"] for entry in manifest["files"]}
    model, projector = manifest["model_file"], manifest["projector_file"]
    if model not in names or projector not in names:
        raise AssetError("Qwen requires both its verified GGUF and full vision projector")
    return [
        command(workspace),
        "--host",
        host,
        "--port",
        str(port),
        "--api-key-file",
        str(workspace.api_key),
        "--alias",
        str(manifest["model_id"]),
        "--model",
        str(model_root / model),
        "--mmproj",
        str(model_root / projector),
        "--ctx-size",
        str(settings["max_context_window"]),
        "--parallel",
        "1",
        "--batch-size",
        "512",
        "--ubatch-size",
        "128",
        "--cache-type-k",
        "q4_0",
        "--cache-type-v",
        "q4_0",
        "--flash-attn",
        "on",
        "--ctx-checkpoints",
        "4",
        "--cache-ram",
        "0",
        "--no-cache-idle-slots",
        "--no-context-shift",
        "--spec-type",
        "none",
        "--gpu-layers",
        "all",
        "--fit",
        "off",
        "--mmproj-offload",
        "--jinja",
        "--reasoning",
        "off",
        "--reasoning-budget",
        "512",
        "--image-min-tokens",
        "1024",
        "--image-max-tokens",
        "4096",
        "--mtmd-batch-max-tokens",
        "1024",
        "--predict",
        str(GENERATION_RESERVE),
        "--no-warmup",
        "--samplers",
        "penalties;top_k;top_p;min_p;temperature",
        "--temp",
        "0.6",
        "--top-p",
        "0.95",
        "--top-k",
        "20",
        "--min-p",
        "0",
        "--presence-penalty",
        "0",
        "--repeat-penalty",
        "1",
        "--repeat-last-n",
        "64",
        "--cache-reuse",
        "0",
        "--cache-prompt",
        "--load-mode",
        "mmap",
        "--lazy-mode",
        "off",
        "--offline",
        "--no-webui",
        "--slots",
    ]


def _record_path(workspace: Workspace) -> Path:
    return workspace.state / "llama-server.json"


def _asset_identity(manifest: dict[str, Any]) -> str:
    value = {
        key: manifest.get(key)
        for key in (
            "source",
            "model_id",
            "model_file",
            "projector_file",
            "files",
        )
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _identity(pid: int) -> str:
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "uid=,stat=,lstart="],
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )
    fields = result.stdout.split(None, 2) if result.returncode == 0 else []
    if len(fields) != 3 or fields[0] != str(os.getuid()) or fields[1].startswith("Z"):
        return ""
    return f"{fields[0]} {fields[2].strip()}"


def owned_record(workspace: Workspace) -> dict[str, Any] | None:
    try:
        path = _record_path(workspace)
        if path.is_symlink():
            raise AssetError("unexpected llama.cpp ownership symlink")
        value = json.loads(path.read_text())
        if (
            type(value["supervisor_pid"]) is not int
            or value["supervisor_pid"] <= 1
            or type(value["pid"]) is not int
            or value["pid"] <= 1
        ):
            return None
        supervisor_owned = (
            bool(value["supervisor_identity"])
            and _identity(value["supervisor_pid"]) == value["supervisor_identity"]
        )
        child_owned = bool(value["identity"]) and _identity(value["pid"]) == value["identity"]
        return value if supervisor_owned or child_owned else None
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None


def _owned_child_process(workspace: Workspace, pid: int, port: int) -> bool:
    record = owned_record(workspace)
    if (
        record is None
        or record.get("pid") != pid
        or record.get("port") != port
        or not record.get("identity")
        or _identity(pid) != record["identity"]
    ):
        return False
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )
    expected = str(runtime_directory(workspace) / "llama-server") + " "
    if result.returncode or not result.stdout.strip().startswith(expected):
        return False
    return True


def managed_server_process(workspace: Workspace, pid: int, port: int) -> bool:
    if not _owned_child_process(workspace, pid, port):
        return False
    listener = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), f"-iTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        check=False,
        timeout=3,
    )
    return listener.returncode == 0


def memory_sample() -> tuple[int, int]:
    result = subprocess.run(
        ["/usr/sbin/sysctl", "kern.memorystatus_vm_pressure_level", "vm.swapusage"],
        capture_output=True,
        text=True,
        check=True,
        timeout=3,
    )
    pressure = re.search(r"pressure_level:\s*(\d+)", result.stdout)
    used = re.search(r"used\s*=\s*([0-9.]+)([MG])", result.stdout)
    if pressure is None or used is None:
        raise AssetError("memory telemetry is unavailable")
    return int(pressure[1]), int(float(used[1]) * 1024 ** (2 if used[2] == "M" else 3))


def memory_guard(sample: tuple[int, int], baseline_swap: int) -> None:
    pressure, swap = sample
    if pressure not in {1, 2}:
        raise AssetError("llama.cpp stopped because memory pressure is critical or unknown")
    if swap > baseline_swap + SWAP_ALLOWANCE_BYTES:
        raise AssetError("llama.cpp stopped because additional used swap exceeded 1 GiB")


def _server_environment() -> dict[str, str]:
    # The offline server and its supervisor need no Pi, MCP, or cloud credentials.
    names = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME"}
    return {name: value for name, value in os.environ.items() if name in names}


def run_server(workspace: Workspace, host: str, port: int) -> int:
    """Supervise the listener, keeping T3/Pi responsible for agent cancellation."""
    from .runtime import _write_private_json, ensure_api_key

    workspace.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = workspace.state / "llama-supervisor.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    child = None
    previous_handlers = {}
    stopped = False

    def stop_requested(*_: Any) -> None:
        nonlocal stopped
        stopped = True

    with os.fdopen(descriptor, "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AssetError("a managed llama.cpp supervisor is already running") from error
        try:
            ensure_api_key(workspace)
            argv = serve_command(workspace, host, port)
            baseline = memory_sample()
            memory_guard(baseline, baseline[1])
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[sig] = signal.signal(sig, stop_requested)
            # Clear a prior diagnostic only when this supervisor owns the lock.
            (workspace.state / "llama-outcome.json").unlink(missing_ok=True)
            child = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=_server_environment(),
            )
            identity = _identity(child.pid)
            if not identity:
                raise AssetError("cannot establish llama.cpp process ownership")
            _write_private_json(
                _record_path(workspace),
                {
                    "pid": child.pid,
                    "identity": identity,
                    "port": port,
                    "supervisor_pid": os.getpid(),
                    "supervisor_identity": _identity(os.getpid()),
                    "model_id": verify_assets(workspace)["model_id"],
                    "asset_identity": _asset_identity(verify_assets(workspace)),
                },
            )
            workspace.server_pid.write_text(f"{child.pid}\n")
            workspace.server_pid.chmod(0o600)
            while not stopped and child.poll() is None:
                memory_guard(memory_sample(), baseline[1])
                time.sleep(POLL_SECONDS)
            return child.returncode if child.returncode is not None else 0
        except (OSError, ValueError, subprocess.SubprocessError, AssetError) as error:
            _write_private_json(workspace.state / "llama-outcome.json", {"reason": str(error)})
            raise AssetError(str(error)) from error
        finally:
            if child is not None:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                else:
                    child.wait()
            _record_path(workspace).unlink(missing_ok=True)
            if child is not None and workspace.server_pid.exists():
                if workspace.server_pid.read_text().strip() == str(child.pid):
                    workspace.server_pid.unlink()
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)


def _launcher(workspace: Workspace, host: str, port: int) -> list[str]:
    executable = (
        [sys.executable]
        if getattr(sys, "frozen", False)
        else [
            sys.executable,
            "-m",
            "geer.cli",
        ]
    )
    return [*executable, "serve", "--host", host, "--port", str(port)]


def start_server(
    workspace: Workspace,
    host: str,
    requested_port: int | None,
    timeout: float,
) -> dict[str, Any]:
    # The caller holds the shared server lock, including while a cold load starts.
    from .runtime import (
        _available_port,
        _server_start_error,
        _write_endpoint,
        configured_port,
        endpoint,
        ensure_api_key,
        get_json,
    )

    manifest = verify_assets(workspace)
    api_key = ensure_api_key(workspace)
    port = requested_port if requested_port is not None else configured_port(workspace)
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise AssetError("the local server must bind to loopback")
    existing = owned_record(workspace)
    if existing is not None:
        if _identity(existing["supervisor_pid"]) != existing["supervisor_identity"]:
            raise AssetError(
                "llama.cpp supervisor was interrupted; stop the backend before retrying"
            )
        if (
            existing["port"] != port
            or existing["model_id"] != manifest["model_id"]
            or existing.get("asset_identity") != _asset_identity(manifest)
        ):
            raise AssetError("stop the existing managed llama.cpp backend before changing models")
        process = None
    else:
        selected = _available_port(host, port)
        if requested_port is not None and selected != port:
            raise AssetError(f"loopback port {port} is already in use")
        port = selected
        environment = _server_environment()
        environment.update(GEER_WORKSPACE=str(workspace.root), GEER_HOME=str(workspace.runtime))
        with workspace.server_log.open("wb", buffering=0) as log:
            process = subprocess.Popen(
                _launcher(workspace, host, port),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                umask=0o077,
            )
        workspace.server_log.chmod(0o600)
    base_url = endpoint(host, port)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                raise _server_start_error(workspace, "llama.cpp supervisor exited during startup")
            if existing is not None and (
                _identity(existing["supervisor_pid"]) != existing["supervisor_identity"]
            ):
                raise _server_start_error(workspace, "llama.cpp supervisor exited during startup")
            record = owned_record(workspace)
            if record is not None and managed_server_process(workspace, record["pid"], port):
                try:
                    health = get_json(f"{base_url}/health", api_key=api_key, timeout=1)
                    models = get_json(f"{base_url}/v1/models", api_key=api_key, timeout=1)
                except AssetError:
                    pass
                else:
                    ids = {row.get("id") for row in models.get("data", []) if isinstance(row, dict)}
                    if health.get("status") != "ok" or ids != {manifest["model_id"]}:
                        raise AssetError(
                            "managed llama.cpp endpoint has an unexpected model identity"
                        )
                    _write_endpoint(workspace, host, port, record["pid"])
                    return {
                        "server": "online",
                        "endpoint": base_url,
                        "port": port,
                        "pid": record["pid"],
                        "started": process is not None,
                    }
            time.sleep(0.25)
        raise _server_start_error(
            workspace, f"llama.cpp did not become healthy within {timeout:g} seconds"
        )
    except BaseException:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                # Leave ownership state intact so stop/recovery can diagnose it.
                raise AssetError(
                    "llama.cpp startup failed and its supervisor did not stop"
                ) from None
        raise


def stop_server(
    workspace: Workspace,
    *,
    port: int | None = None,
    timeout: float = 20,
) -> dict[str, Any] | None:
    record = owned_record(workspace)
    if record is None or (port is not None and record["port"] != port):
        return None
    supervisor = (
        bool(record.get("supervisor_identity"))
        and _identity(record["supervisor_pid"]) == record["supervisor_identity"]
    )
    if supervisor:
        os.kill(record["supervisor_pid"], signal.SIGTERM)
    elif _owned_child_process(workspace, record["pid"], record["port"]):
        # Recover a listener whose supervisor exited without completing cleanup.
        os.kill(record["pid"], signal.SIGTERM)
    else:
        raise AssetError(
            "cannot verify the interrupted llama.cpp listener; ownership state retained"
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if owned_record(workspace) is None and _identity(record["pid"]) != record["identity"]:
            _record_path(workspace).unlink(missing_ok=True)
            return {"server": "stopped", "pid": record["pid"], "stopped": True}
        time.sleep(0.1)
    raise AssetError("llama.cpp supervisor did not finish shutting down")
