from __future__ import annotations

import builtins
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .assets import Workspace
from .hardware import memory_bytes
from .models import install_model, model_plan
from .retrieval import retrieval_canary
from .runtime import (
    MIN_CLAUDE_VERSION,
    bootstrap_runtime,
    canary,
    start_server,
)
from .setup_protocol import JsonSetupFrontend
from .t3 import T3_MINIMUM_VERSION, configure_t3

CLAUDE_INSTALL_COMMAND = "curl -fsSL https://claude.ai/install.sh | bash"
T3_RELEASE_URL = "https://github.com/pingdotgg/t3code/releases"
T3_APP_CANDIDATES = (
    Path("/Applications/T3 Code.app"),
    Path("/Applications/T3 Code (Alpha).app"),
    Path("/Applications/T3 Code (Nightly).app"),
    Path("/Applications/T3.app"),
    Path("~/Applications/T3 Code.app").expanduser(),
    Path("~/Applications/T3 Code (Alpha).app").expanduser(),
    Path("~/Applications/T3 Code (Nightly).app").expanduser(),
)


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
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(state, dict) and state.get("status") == "ready"


@dataclass(frozen=True)
class Prerequisite:
    found: bool
    version: str | None = None
    path: Path | None = None
    settings_ready: bool = False
    running: bool = False


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


def _version_tuple(value: str | None) -> tuple[int, int, int] | None:
    if value is None:
        return None
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", value)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def find_claude() -> Prerequisite:
    candidates: list[Path] = []
    found = shutil.which("claude")
    if found:
        candidates.append(Path(found))
    candidates.extend(
        (
            Path("~/.local/bin/claude").expanduser(),
            Path("/usr/local/bin/claude"),
            Path("/opt/homebrew/bin/claude"),
        )
    )
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        version = _version(candidate)
        parsed = _version_tuple(version)
        if parsed is not None and parsed >= MIN_CLAUDE_VERSION:
            return Prerequisite(True, version, candidate)
    return Prerequisite(False)


def _t3_running(app: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "pgrep",
                "-u",
                str(os.getuid()),
                "-f",
                str(app / "Contents" / "MacOS"),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def find_t3() -> Prerequisite:
    discovered: list[tuple[bool, tuple[int, int, int], str, Path]] = []
    for app in T3_APP_CANDIDATES:
        info = app / "Contents" / "Info.plist"
        if not info.is_file():
            continue
        try:
            with info.open("rb") as stream:
                metadata = plistlib.load(stream)
        except (OSError, plistlib.InvalidFileException):
            continue
        version = str(metadata.get("CFBundleShortVersionString", "")) or None
        parsed = _version_tuple(version)
        if parsed is None or parsed < _version_tuple(T3_MINIMUM_VERSION):
            continue
        discovered.append((_t3_running(app), parsed, version or "", app))
    if discovered:
        running, _, version, app = max(
            discovered,
            key=lambda candidate: (candidate[0], candidate[1]),
        )
        settings = Path.home() / ".t3/userdata/settings.json"
        return Prerequisite(
            True,
            version,
            app,
            settings_ready=settings.parent.is_dir(),
            running=running,
        )
    return Prerequisite(False)


def prerequisite_snapshot() -> dict[str, Prerequisite]:
    return {"claude": find_claude(), "t3": find_t3()}


class PrerequisiteMonitor:
    def __init__(self, interval: float = 5) -> None:
        self.interval = interval
        self.latest = prerequisite_snapshot()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, Prerequisite]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1)
        self.latest = prerequisite_snapshot()
        return self.latest

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.latest = prerequisite_snapshot()


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
    prerequisites: dict[str, Prerequisite],
    plan: dict[str, Any],
    *,
    total_memory_bytes: int | None = None,
) -> None:
    print("Welcome to Geer.\n")
    print(
        "Geer runs a coding model locally on your Mac.\n"
        "T3 Code and Claude Code are used to interact with it.\n"
    )
    apple = platform.system() == "Darwin" and platform.machine() == "arm64"
    print(f"{'✓' if apple else '×'} Apple Silicon detected")
    installed_memory = memory_bytes() if total_memory_bytes is None else total_memory_bytes
    print(f"✓ {human_memory(installed_memory)} unified memory")
    for key, label in (("claude", "Claude Code"), ("t3", "T3 Code")):
        item = prerequisites[key]
        if item.found:
            print(f"✓ {label} {item.version} found")
            if key == "t3" and not item.settings_ready:
                print("× T3 Code has not been initialized")
        else:
            print(f"× {label} not found")
    print("\nRecommended model:")
    print(f"  {plan['display_name']}\n")
    print(f"Model download:         {plan['download_human']}")
    print(f"Runtime download:       {plan['runtime_download_human']}")
    print(f"Runtime after install:  {plan['runtime_installed_human']}")
    print(f"Temporary setup space:  {plan['temporary_human']}")
    print(f"Free space required:    {plan['required_free_human']}")
    if "context_window_human" in plan:
        print(f"Context window:         {plan['context_window_human']}")
    if "kv_cache" in plan:
        print(f"KV cache:               {plan['kv_cache']}")
    print("Location:               ~/.geer\n")


def print_missing(prerequisites: dict[str, Prerequisite]) -> None:
    if not prerequisites["t3"].found:
        print(
            "\nT3 Code is not installed. To install it:\n\n"
            f"  1. Open {T3_RELEASE_URL}\n"
            "  2. Download and install the latest `-arm64.dmg` file\n"
            "  3. Run T3 Code once to create its settings\n"
            "  4. Quit T3 Code\n\n"
            "You can do this while the model downloads."
        )
    elif not prerequisites["t3"].settings_ready:
        print(
            "\nT3 Code is installed but has not been initialized.\n\n"
            "  1. Run T3 Code once\n"
            "  2. Quit T3 Code\n\n"
            "You can do this while the model downloads."
        )
    if not prerequisites["claude"].found:
        print(
            "\nClaude Code was not found. To install it, run:\n\n"
            f"  {CLAUDE_INSTALL_COMMAND}\n\n"
            "You can install it in another Terminal window while the model downloads."
        )


def _quit_t3(app: Path) -> None:
    name = app.stem
    try:
        subprocess.run(
            ["osascript", "-e", f'tell application "{name}" to quit'],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise SetupError(f"could not quit T3 Code: {error}") from error
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not _t3_running(app):
            return
        time.sleep(0.25)
    raise SetupError("T3 Code did not quit within 15 seconds")


def _open_t3(app: Path) -> None:
    subprocess.run(["open", str(app)], check=False)


def _print_integration_plan(prerequisites: dict[str, Prerequisite], model: str) -> None:
    print("\nGeer found:")
    print(f"  ✓ Claude Code {prerequisites['claude'].version}")
    print(f"  ✓ T3 Code {prerequisites['t3'].version}")
    print(
        "\nGeer can add itself to T3 Code.\n\n"
        "Geer will:\n"
        '  • Add a provider named "Geer"\n'
        f'  • Add "{model}"\n'
        "  • Route that provider through your installed Claude Code\n"
        "  • Start the local Geer engine when the provider is used\n\n"
        "Files updated:\n"
        "  ~/.t3/userdata/settings.json\n"
        "  ~/.t3/userdata/client-settings.json\n\n"
        "Existing files will be backed up before they are changed.\n"
    )


def add_t3_integration(
    workspace: Workspace,
    *,
    assume_yes: bool = False,
    plan_only: bool = False,
) -> dict[str, Any]:
    prerequisites = prerequisite_snapshot()
    claude = prerequisites["claude"]
    t3 = prerequisites["t3"]
    if not claude.found:
        raise SetupError(
            f"Claude Code was not found. Install it by running:\n\n  {CLAUDE_INSTALL_COMMAND}"
        )
    if not t3.found:
        raise SetupError(
            "T3 Code is not installed. Install the latest -arm64.dmg from "
            f"{T3_RELEASE_URL} and retry."
        )
    if not t3.settings_ready:
        raise SetupError(
            "T3 Code is installed but has not been initialized. "
            "Run T3 Code once, quit it, and retry."
        )
    plan = model_plan(workspace)
    _print_integration_plan(prerequisites, plan["display_name"])
    settings = Path("~/.t3/userdata/settings.json").expanduser()
    preview = configure_t3(workspace, settings, dry_run=True)
    if plan_only:
        return preview
    if not confirm(
        "Add Geer to T3 Code?",
        default_yes=True,
        assume_yes=assume_yes,
    ):
        return {"applied": False, "status": "declined"}
    reopen = False
    if t3.running:
        print(
            "\nT3 Code must be closed while its settings are updated.\n"
            "Geer will reopen it when finished.\n"
        )
        if not confirm(
            "Quit T3 Code and continue?",
            default_yes=True,
            assume_yes=assume_yes,
        ):
            return {"applied": False, "status": "declined"}
        _quit_t3(t3.path or Path())
        print("\n  ✓ Quit T3 Code")
        reopen = True
    print("\nConfiguring T3 Code")
    result = configure_t3(workspace, settings)
    print("  ✓ Updated T3 Code settings safely")
    print("  ✓ Added the Geer provider")
    print("  ✓ Added the Geer model")
    print("  ✓ Verified the resulting configuration")
    if reopen and t3.path is not None:
        _open_t3(t3.path)
        print("  ✓ Reopened T3 Code")
    return result


def setup(
    workspace: Workspace,
    *,
    assume_yes: bool = False,
    skip_t3: bool = False,
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
            skip_t3=skip_t3,
            high_performance=high_performance,
            plan_only=plan_only,
        )
    finally:
        _ACTIVE_FRONTEND = previous_frontend


def _setup(
    workspace: Workspace,
    *,
    assume_yes: bool,
    skip_t3: bool,
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
    automatic_upgrade = plan.get("active_model_installed") and not plan[
        "reusable_active_model"
    ]
    if automatic_upgrade:
        print(
            "\nAn older Geer model is installed. Geer will upgrade it to the "
            f"latest {plan['display_name']} automatically."
        )
    elif not confirm("Continue?", default_yes=True, assume_yes=assume_yes):
        return {"status": "cancelled"}

    _phase("runtime", "Preparing Geer runtime", 1, 5)
    print("\nPreparing Geer")
    bootstrap_runtime(workspace)
    print("  ✓ Installed the Geer runtime")
    _probe_mlx(workspace)
    print("  ✓ Verified MLX acceleration")
    print_missing(initial)

    monitor = PrerequisiteMonitor()
    monitor.start()
    try:
        _phase("model", "Preparing the local model", 2, 5)
        print(f"\nPreparing model\n  {plan['display_name']}")
        installed = install_model(
            workspace,
            high_performance=high_performance,
            total_memory_bytes=total_memory,
        )
    finally:
        final = monitor.stop()
    activation = installed.get("installed", {}).get("activation", {})
    if activation.get("reused"):
        print("  ✓ Reused the verified active model")
    else:
        print("  ✓ Downloaded and verified model files")
    print(f"  ✓ Activated model at {plan['active_link']}")

    _phase("retrieval", "Preparing repository retrieval", 3, 5)
    print("\nPreparing repository retrieval")
    retrieval_canary(workspace)
    print("  ✓ Installed Semble")
    print("  ✓ Verified local search")

    _phase("integrations", "Checking integrations", 4, 5)
    print("\nChecking integrations again")
    _print_check("Claude Code", final["claude"])
    _print_check("T3 Code", final["t3"])
    if final["t3"].found:
        print(f"  {'✓' if final['t3'].settings_ready else '×'} T3 Code settings found")

    configured = False
    reopen_t3 = False
    if not skip_t3 and final["claude"].found and final["t3"].found:
        if not final["t3"].settings_ready:
            print(
                "\nT3 Code has not created its settings yet. Run it once, quit it,\n"
                "then finish the integration with:\n\n"
                "  geer integration add t3"
            )
        else:
            _print_integration_plan(final, plan["display_name"])
            if confirm(
                "Add Geer to T3 Code?",
                default_yes=True,
                assume_yes=assume_yes,
            ):
                if final["t3"].running:
                    print(
                        "\nT3 Code must be closed while its settings are updated.\n"
                        "Geer will reopen it when finished.\n"
                    )
                    if confirm(
                        "Quit T3 Code and continue?",
                        default_yes=True,
                        assume_yes=assume_yes,
                    ):
                        _quit_t3(final["t3"].path or Path())
                        print("\n  ✓ Quit T3 Code")
                        reopen_t3 = True
                    else:
                        print(
                            "\nNo T3 Code settings were changed.\n\n"
                            "To add the integration later, quit T3 Code and run:\n\n"
                            "  geer integration add t3"
                        )
                if not final["t3"].running or reopen_t3:
                    print("\nConfiguring T3 Code")
                    result = configure_t3(
                        workspace,
                        Path("~/.t3/userdata/settings.json").expanduser(),
                    )
                    print("  ✓ Updated T3 Code settings safely")
                    print("  ✓ Added the Geer provider")
                    print("  ✓ Added the Geer model")
                    print("  ✓ Verified the resulting configuration")
                    configured = bool(result["applied"])
            else:
                print(
                    "\nNo T3 Code settings were changed.\n\n"
                    "To add the integration later, run:\n\n"
                    "  geer integration add t3"
                )
    elif not skip_t3:
        missing = [
            label
            for key, label in (("claude", "Claude Code"), ("t3", "T3 Code"))
            if not final[key].found
        ]
        print("\nGeer's local model is ready, but these integrations are missing:\n")
        for label in missing:
            print(f"  × {label}")
        print("\nYou can finish the integration later by running:\n\n  geer integration add t3")

    _phase("validation", "Testing local inference", 5, 5)
    print("\nTesting Geer")
    started = start_server(workspace)
    print("  ✓ Started the local engine")
    print("  ✓ Loaded the model")
    canary(
        workspace,
        str(started["endpoint"]),
        "Reply with exactly: GEER_LOCAL_OK",
        32,
        600,
    )
    print("  ✓ Generated a local test response")
    print("  ✓ Verified repository retrieval")
    if reopen_t3 and final["t3"].path is not None:
        _open_t3(final["t3"].path)
        print("  ✓ Reopened T3 Code")

    print(f"\nGeer is ready.\n\nModel:\n  {plan['display_name']}")
    if configured:
        print(f"\nIn T3 Code, select:\n  Geer → {plan['display_name']}")
    else:
        print("\nTo add T3 Code later, run:\n\n  geer integration add t3")
    print("\nRun `geer` at any time to see its status.")
    _mark_ready(workspace, plan["display_name"], configured)
    return {
        "status": "ready",
        "model": plan["display_name"],
        "installed": installed,
        "server": started,
        "t3_configured": configured,
    }


def _mark_ready(workspace: Workspace, model: str, t3_configured: bool) -> None:
    workspace.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = {
        "schema_version": 1,
        "status": "ready",
        "completed_at": datetime.now(UTC).isoformat(),
        "model": model,
        "t3_configured": t3_configured,
    }
    temporary = workspace.setup_state.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, workspace.setup_state)


def _print_check(label: str, item: Prerequisite) -> None:
    if item.found:
        print(f"  ✓ {label} {item.version} found")
    else:
        print(f"  × {label} not found")


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
