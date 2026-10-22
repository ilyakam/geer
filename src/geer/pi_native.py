"""Launch pinned Pi directly for T3's native RPC driver.

T3 owns RPC, permissions, cancellation, and continuation. Geer supplies the
local model, private configuration, retrieval, and shared skill paths.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NoReturn

from .assets import AssetError, Workspace
from .runtime import prepare_pi
from .skills import skill_paths

_T3_SESSION_ENV = (
    "T3_MCP_URL", "T3_MCP_BEARER_TOKEN", "T3_PI_MCP_EXTENSION_PATH", "T3_PI_RUNTIME_MODE",
)


def prepare_native_pi(
    workspace: Workspace, arguments: list[str], *, port: int | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Match T3's discovery, background helper, and interactive launch contracts."""
    if arguments in (["--version"], ["-v"]):
        return prepare_pi(workspace, arguments)
    if arguments[:2] != ["--mode", "rpc"]:
        raise AssetError("Expected native Pi arguments: --mode rpc")

    ephemeral = "--no-session" in arguments
    background = ephemeral and "--no-tools" in arguments and "--no-extensions" in arguments
    interactive = not ephemeral
    if background:
        # T3 strips explicit extensions and tool selections for helpers. Skills
        # are also unnecessary for naming/content generation, including paths
        # supplied through the provider's launchArgs.
        filtered: list[str] = []
        skip = False
        for argument in arguments:
            if skip:
                skip = False
            elif argument == "--skill":
                skip = True
            elif not argument.startswith("--skill="):
                filtered.append(argument)
        arguments = filtered
    if interactive:
        if os.environ.get("T3_PI_RUNTIME_MODE") != "full-access":
            raise AssetError(
                "Geer native Pi chats require T3 Full access mode. "
                "Select Full access before starting this chat."
            )
        extension = os.environ.get("T3_PI_MCP_EXTENSION_PATH")
        if (
            not extension
            or not Path(extension).is_file()
            or not any(
                argument in {"--extension", "-e"} and arguments[index + 1:index + 2] == [extension]
                for index, argument in enumerate(arguments)
            )
        ):
            raise AssetError("T3's native Pi permission extension is missing; restart T3 Code")

    command, environment = prepare_pi(
        workspace, arguments, ensure_server=interactive or background,
        port=port, enable_tools=interactive,
    )
    if not interactive:
        for name in _T3_SESSION_ENV:
            environment.pop(name, None)
    if ephemeral and not background:
        # Discovery reads models and commands without starting inference or MCP.
        # Explicit skill paths still work with Pi's automatic discovery disabled.
        for path in skill_paths(workspace):
            command.extend(["--skill", str(path)])
    return command, environment


def launch_native_pi(
    workspace: Workspace, arguments: list[str], *, port: int | None = None,
) -> NoReturn:
    command, environment = prepare_native_pi(workspace, arguments, port=port)
    os.umask(0o077)
    os.execvpe(command[0], command, environment)
