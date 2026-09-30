from __future__ import annotations

import builtins
import json
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .assets import AssetError, Workspace, find_workspace
from .hardware import memory_bytes
from .models import install_model, model_plan
from .retrieval import retrieval_canary
from .runtime import (
    bootstrap_pi,
    bootstrap_runtime,
    canary,
    compatible_pi,
    start_server,
)
from .setup_protocol import JsonSetupFrontend
from .t3 import (
    T3_MINIMUM_VERSION,
    T3Application,
    configure_t3,
    find_t3,
    running_t3_applications,
    t3_settings_path,
    t3_status,
)
from .t3_distribution import T3DistributionError, install_t3


class SetupError(RuntimeError):
    """Raised when guided setup cannot continue safely."""


def output(value: str = "", **kwargs: Any) -> None:
    frontend = _ACTIVE_FRONTEND
    if frontend is None:
        builtins.print(value, **kwargs)
    else:
        frontend.output_text(value, **kwargs)


_ACTIVE_FRONTEND: JsonSetupFrontend | None = None
print = output


def _phase(identifier: str, title: str, step: int, total: int) -> None:
    if _ACTIVE_FRONTEND is not None:
        _ACTIVE_FRONTEND.emit(
            "phase",
            phase=identifier,
            title=title,
            completed=step,
            total=total,
            determinate=True,
        )


def setup_complete(workspace: Workspace) -> bool:
    try:
        state = json.loads(workspace.setup_state.read_text())
        compatible_pi(workspace)
    except (AssetError, OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(state, dict)
        and state.get("status") == "ready"
        and state.get("harness") == "pi"
        and state.get("t3_configured") is True
        and t3_status(workspace).get("ready") is True
    )


@dataclass(frozen=True)
class Prerequisite:
    found: bool
    version: str | None = None
    path: Path | None = None


def human_memory(value: int) -> str:
    if value <= 0:
        return "unknown"
    gib = value / 1024**3
    return f"{gib:.0f} GB"


def _version(command: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(command), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", result.stdout)
    return match.group(1) if match else None


def find_pi() -> Prerequisite:
    try:
        command = Path(compatible_pi(find_workspace()))
    except AssetError:
        return Prerequisite(False)
    return Prerequisite(True, _version(command), command)


def prerequisite_snapshot() -> dict[str, Prerequisite | T3Application]:
    return {"pi": find_pi(), "t3": find_t3()}


def confirm(prompt: str, *, default_yes: bool, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if _ACTIVE_FRONTEND is not None:
        return _ACTIVE_FRONTEND.decide(prompt, default_yes=default_yes)
    if not sys.stdin.isatty():
        raise SetupError("interactive consent is required; rerun with `--yes`")
    suffix = "[Y/n]" if default_yes else "[y/N]"
    answer = input(f"{prompt} {suffix} ").strip().lower()
    if not answer:
        return default_yes
    return answer in {"y", "yes"}


def print_welcome(
    prerequisites: dict[str, Prerequisite | T3Application],
    plan: dict[str, Any],
    *,
    total_memory_bytes: int | None = None,
) -> None:
    print("Welcome to Geer.\n")
    print(
        "Geer runs a coding model locally on your Mac.\n"
        "Pi runs the coding agent, with T3 Code as its desktop interface.\n"
    )
    apple = platform.system() == "Darwin" and platform.machine() == "arm64"
    print(f"{'✓' if apple else '×'} Apple Silicon detected")
    installed_memory = memory_bytes() if total_memory_bytes is None else total_memory_bytes
    print(f"✓ {human_memory(installed_memory)} unified memory")
    for key, label in (("pi", "Pi"), ("t3", "T3 Code")):
        item = prerequisites[key]
        if item.found:
            print(f"✓ {label} {item.version} found")
        elif key == "t3" and item.path is not None:
            print(f"• T3 Code {item.version} needs an update to {T3_MINIMUM_VERSION} or newer")
        else:
            print(f"• {label} will be installed automatically")
    print("\nRecommended model:")
    print(f"  {plan['display_name']}\n")
    print(f"Model download:         {plan['download_human']}")
    print(f"Runtime download:       {plan['runtime_download_human']}")
    print(f"Runtime after install:  {plan['runtime_installed_human']}")
    if "pi_download_human" in plan:
        print(f"Pi download:            {plan['pi_download_human']}")
        print(f"Pi after install:       {plan['pi_installed_human']}")
    print(f"T3 Code download:       {plan['t3_download_human']}")
    print(f"T3 Code after install:  {plan['t3_installed_human']}")
    print(f"Temporary setup space:  {plan['temporary_human']}")
    print(f"Free space required:    {plan['required_free_human']}")
    if "context_window_human" in plan:
        print(f"Context window:         {plan['context_window_human']}")
    if "kv_cache" in plan:
        print(f"KV cache:               {plan['kv_cache']}")
    print("Model and runtimes:     ~/.geer")
    print(f"T3 Code app:            {prerequisites['t3'].path or '~/Applications/T3 Code.app'}")
    print("Existing compatible installations and settings are reused.\n")


def _quit_t3(app: Path) -> None:
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                "on run argv",
                "-e",
                "tell application (item 1 of argv) to quit",
                "-e",
                "end run",
                str(app),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise SetupError(f"could not quit T3 Code: {error}") from error
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if app not in running_t3_applications():
            return
        time.sleep(0.25)
    raise SetupError("T3 Code did not quit within 15 seconds")


def prepare_t3(workspace: Workspace) -> Path:
    application = find_t3()
    if application.found and application.path is not None:
        print(f"  ✓ Reused T3 Code {application.version} at {application.path}")
        return application.path
    if application.path is not None:
        raise SetupError(
            f"T3 Code {application.version} at {application.path} needs an update "
            f"to {T3_MINIMUM_VERSION} or newer. Update that app and rerun `geer setup`. "
            "No additional T3 Code app was installed."
        )
    destination = Path.home() / "Applications" / "T3 Code.app"
    try:
        app = install_t3(destination, cache=workspace.runtime / "cache" / "downloads")
    except T3DistributionError as error:
        raise SetupError(f"T3 Code is required; installation failed: {error}") from error
    print(f"  ✓ Installed and verified T3 Code at {app}")
    return app


def add_t3_integration(
    workspace: Workspace,
    *,
    assume_yes: bool = False,
    plan_only: bool = False,
) -> dict[str, Any]:
    if not find_pi().found:
        raise SetupError("Pi is not installed. Run `geer setup` to install the pinned harness.")
    if not find_t3().found:
        raise SetupError("A compatible T3 Code app is required. Run `geer setup` to prepare it.")
    settings = t3_settings_path()
    preview = configure_t3(workspace, settings, dry_run=True)
    if plan_only:
        return preview
    running = running_t3_applications() if preview["changed"] else []
    if running:
        print(
            "\nT3 Code must be closed while its settings are updated.\n"
            "Save your work before continuing.\n"
        )
        if assume_yes:
            raise SetupError(
                "Quit T3 Code, then rerun `geer setup --yes`. "
                "No T3 settings were changed; downloaded files are retained."
            )
        if not confirm(
            "Quit T3 Code and continue?",
            default_yes=True,
            assume_yes=False,
        ):
            raise SetupError(
                "T3 Code configuration is required. Quit T3 Code and rerun `geer setup`. "
                "No T3 settings were changed; downloaded files are retained."
            )
        for application in running:
            _quit_t3(application)
        print("\n  ✓ Quit T3 Code")
        if running_t3_applications():
            raise SetupError("T3 Code is still running; quit it and rerun `geer setup`.")
    print("\nConfiguring T3 Code")
    result = configure_t3(workspace, settings)
    status = t3_status(workspace, settings)
    if not status.get("ready"):
        raise SetupError(f"T3 Code configuration failed: {status.get('error', 'not ready')}")
    print("  ✓ Configured the Geer provider and local model")
    print("  ✓ Verified the resulting configuration")
    return {**result, "t3_application_path": status["application_path"]}


def setup(
    workspace: Workspace,
    *,
    assume_yes: bool = False,
    high_performance: bool = False,
    plan_only: bool = False,
    frontend: JsonSetupFrontend | None = None,
) -> dict[str, Any]:
    global _ACTIVE_FRONTEND
    previous_frontend = _ACTIVE_FRONTEND
    _ACTIVE_FRONTEND = frontend
    try:
        return _setup(
            workspace,
            assume_yes=assume_yes,
            high_performance=high_performance,
            plan_only=plan_only,
        )
    finally:
        _ACTIVE_FRONTEND = previous_frontend


def _setup(
    workspace: Workspace,
    *,
    assume_yes: bool,
    high_performance: bool,
    plan_only: bool,
) -> dict[str, Any]:
    total_memory = memory_bytes()
    plan = model_plan(workspace, total_memory_bytes=total_memory)
    initial = prerequisite_snapshot()
    print_welcome(initial, plan, total_memory_bytes=total_memory)
    if _ACTIVE_FRONTEND is not None:
        _ACTIVE_FRONTEND.emit(
            "plan",
            plan=plan,
            prerequisites=initial,
            retained_model=plan["reusable_active_model"],
        )
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SetupError("Geer currently requires an Apple Silicon Mac")
    if plan_only:
        return {"status": "planned", "plan": plan}
    if not confirm("Continue?", default_yes=True, assume_yes=assume_yes):
        return {"status": "cancelled"}

    _write_setup_state(workspace, status="setting_up", t3_configured=False)
    try:
        return _prepare_and_validate(workspace, plan, total_memory, assume_yes, high_performance)
    except (AssetError, SetupError, OSError) as error:
        _write_setup_state(workspace, status="failed", t3_configured=False, error=str(error))
        if isinstance(error, OSError):
            raise SetupError(f"Setup could not finish: {error}") from error
        raise


def _prepare_and_validate(
    workspace: Workspace,
    plan: dict[str, Any],
    total_memory: int,
    assume_yes: bool,
    high_performance: bool,
) -> dict[str, Any]:
    _phase("runtime", "Preparing Geer, Pi, and T3 Code", 1, 5)
    print("\nPreparing Geer, Pi, and T3 Code")
    bootstrap_runtime(workspace)
    print("  ✓ Installed the Geer runtime")
    _probe_mlx(workspace)
    print("  ✓ Verified MLX acceleration")
    bootstrap_pi(workspace)
    print("  ✓ Installed and verified the Pi harness")
    prepare_t3(workspace)

    _phase("model", "Preparing the local model", 2, 5)
    print(f"\nPreparing model\n  {plan['display_name']}")
    installed = install_model(
        workspace,
        high_performance=high_performance,
        total_memory_bytes=total_memory,
    )
    activation = installed.get("installed", {}).get("activation", {})
    if activation.get("reused"):
        print("  ✓ Reused the verified active model")
    else:
        print("  ✓ Downloaded and verified model files")
    print(f"  ✓ Activated model at {plan['active_link']}")

    _phase("retrieval", "Preparing repository retrieval", 3, 5)
    print("\nPreparing repository retrieval")
    retrieval_canary(workspace)
    print("  ✓ Installed Semble and verified local search")

    # Validate inference before replacing a retained legacy provider binding.
    _phase("validation", "Testing local inference", 4, 5)
    print("\nTesting Geer")
    started = start_server(workspace)
    print("  ✓ Started the local engine and loaded the model")
    canary(
        workspace,
        str(started["endpoint"]),
        "Reply with exactly: GEER_LOCAL_OK",
        32,
        600,
    )
    print("  ✓ Generated a local test response")

    _phase("integrations", "Configuring T3 Code", 5, 5)
    integration = add_t3_integration(workspace, assume_yes=assume_yes)
    application_path = integration["t3_application_path"]
    _write_setup_state(
        workspace,
        status="ready",
        model=plan["display_name"],
        t3_configured=True,
        t3_application_path=application_path,
        completed_at=datetime.now(UTC).isoformat(),
    )
    print(f"\nGeer is ready.\n\nOpen T3 Code: {application_path}")
    print(
        f"Open a project and select Geer → {plan['display_name']} with Full access.\n"
        "On first launch, continue through T3 Code's welcome screens.\n"
        "Run `geer` at any time to see its status."
    )
    return {
        "status": "ready",
        "model": plan["display_name"],
        "installed": installed,
        "server": started,
        "t3_configured": True,
        "t3_application_path": application_path,
    }


def _write_setup_state(workspace: Workspace, **values: Any) -> None:
    workspace.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = {
        "schema_version": 1,
        "harness": "pi",
        "updated_at": datetime.now(UTC).isoformat(),
        **values,
    }
    temporary = workspace.setup_state.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, workspace.setup_state)


def _probe_mlx(workspace: Workspace) -> None:
    python = workspace.runtime / "omlx" / "bin" / "python"
    if not python.is_file():
        raise SetupError(f"the Geer MLX runtime is missing: {python}")
    program = (
        "import mlx.core as mx;"
        "assert mx.metal.is_available();"
        "assert mx.sum(mx.array([1,2,3])).item()==6"
    )
    try:
        subprocess.run(
            [str(python), "-c", program],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise SetupError(f"MLX acceleration check failed: {error}") from error
