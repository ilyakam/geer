from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .assets import Workspace
from .onboarding import SetupError, confirm
from .runtime import stop_server
from .t3 import remove_t3

INSTALL_PATHS = (
    Path("/usr/local/bin/geer"),
    Path("/usr/local/bin/geer-claude"),
    Path("/Library/Application Support/Geer"),
)
PACKAGE_IDENTIFIER = "com.ilyakam.geer"


def _server_running(workspace: Workspace) -> bool:
    try:
        pid = int(workspace.server_pid.read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        result = subprocess.run(
            ["/usr/bin/du", "-sk", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return int(result.stdout.split()[0]) * 1024
    except (
        OSError,
        ValueError,
        IndexError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        total = 0
        for root, directories, files in os.walk(path, followlinks=False):
            for name in (*directories, *files):
                try:
                    total += (Path(root) / name).lstat().st_size
                except OSError:
                    continue
        return total


def _human_gb(value: int) -> str:
    return f"{value / 1000**3:.1f} GB"


def _remove_installation(paths: tuple[Path, ...]) -> None:
    existing = tuple(path for path in paths if path.exists() or path.is_symlink())
    try:
        receipt = subprocess.run(
            ["/usr/sbin/pkgutil", "--pkg-info", PACKAGE_IDENTIFIER],
            check=False,
            capture_output=True,
            text=True,
        )
        commands: list[str] = []
        if existing:
            commands.append(
                shlex.join(["/bin/rm", "-rf", *(str(path) for path in existing)])
            )
        if receipt.returncode == 0:
            commands.append(
                shlex.join(
                    ["/usr/sbin/pkgutil", "--forget", PACKAGE_IDENTIFIER]
                )
            )
        if not commands:
            return
        shell_command = " && ".join(commands)
        authorization = (
            f"do shell script {json.dumps(shell_command)} "
            "with administrator privileges"
        )
        subprocess.run(
            ["/usr/bin/osascript", "-e", authorization],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SetupError(f"could not remove the Geer installation: {error}") from error


def uninstall(
    workspace: Workspace,
    *,
    assume_yes: bool = False,
) -> dict[str, Any]:
    running = _server_running(workspace)
    print(f"Geer is {'currently running' if running else 'not currently running'}\n")
    print(
        "Uninstalling Geer will stop the local server, remove its T3 Code "
        "integration, and delete the geer CLI.\n"
    )
    if not confirm(
        "Continue?",
        default_yes=False,
        assume_yes=assume_yes,
    ):
        print("\nNothing was removed.")
        return {"status": "cancelled"}

    print("\nUninstalling Geer")
    stopped = stop_server(workspace)
    if stopped.get("stopped"):
        print("  ✓ Stopped the Geer server")
    else:
        print("  ✓ Geer server was already stopped")

    settings = Path("~/.t3/userdata/settings.json").expanduser()
    integration = remove_t3(settings)
    if integration["removed"]:
        print("  ✓ Removed Geer from T3 Code")
    else:
        print("  ✓ No Geer T3 Code integration was installed")

    retained_bytes = directory_size(workspace.runtime)
    _remove_installation(INSTALL_PATHS)
    print("  ✓ Deleted the Geer CLI and application files")

    print(
        "\nRun `rm -rf ~/.geer` to delete all caches, models, and configuration.\n"
        f"This operation would free up an additional {_human_gb(retained_bytes)}."
    )
    return {
        "status": "uninstalled",
        "server_stopped": bool(stopped.get("stopped")),
        "t3_removed": bool(integration["removed"]),
        "retained_path": str(workspace.runtime),
        "retained_bytes": retained_bytes,
    }
