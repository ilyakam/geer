from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .assets import (
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_SOURCE,
    AssetError,
    find_workspace,
    initialize_assets,
)
from .hardware import UnsupportedHardwareError
from .models import (
    install_model,
    list_models,
    remove_model,
    require_model_name,
    use_model,
)
from .onboarding import SetupError, add_t3_integration, setup, setup_complete
from .retrieval import retrieval_canary
from .runtime import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    bootstrap_runtime,
    canary,
    doctor,
    launch_claude,
    record_launcher_status,
    run_server,
    runtime_status,
    server_endpoint,
    snapshot_runtime,
    start_server,
    stop_server,
)
from .setup_protocol import ProtocolError, run_json_setup
from .t3 import remove_t3
from .uninstall import uninstall

ROOT_ALIASES = {
    "install": "setup",
    "remove": "uninstall",
    "delete": "uninstall",
    "health": "status",
    "fix": "doctor",
    "usage": "stats",
    "start": "server start",
    "stop": "server stop",
    "t3-install": "integration add t3",
    "t3-uninstall": "integration remove t3",
}
GROUP_ALIASES = {
    "model": {
        "show": "list",
        "install": "add",
        "setup": "add",
        "set": "use",
        "delete": "remove",
    },
    "server": {
        "up": "start",
        "down": "stop",
        "reboot": "restart",
        "health": "status",
    },
    "integration": {
        "show": "list",
        "install": "add",
        "setup": "add",
        "delete": "remove",
    },
}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="geer",
        description="Run a private coding model locally on Apple Silicon.",
    )
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = root.add_subparsers(
        dest="command",
        metavar="{setup,status,doctor,stats,model,server,integration,uninstall}",
    )

    setup_command = commands.add_parser("setup", help="set up Geer on this Mac")
    setup_command.add_argument("--plan", action="store_true")
    setup_command.add_argument("--yes", action="store_true")
    setup_command.add_argument("--skip-t3", action="store_true")
    setup_command.add_argument("--high-performance-download", action="store_true")
    setup_command.add_argument(
        "--frontend",
        choices=("json-v1",),
        help=argparse.SUPPRESS,
    )

    commands.add_parser("status", help="show whether Geer is ready")
    commands.add_parser("doctor", help="verify Geer")
    commands.add_parser("stats", help="show local usage metrics")

    model = commands.add_parser("model", help="manage local models")
    model_commands = model.add_subparsers(dest="model_command", required=True)
    model_commands.add_parser("list", help="list available and installed models")
    model_add = model_commands.add_parser("add", help="download and install a model")
    model_add.add_argument("model")
    model_add.add_argument("--high-performance-download", action="store_true")
    model_use = model_commands.add_parser("use", help="select the active model")
    model_use.add_argument("model")
    model_remove = model_commands.add_parser("remove", help="remove an installed model")
    model_remove.add_argument("model")
    model_remove.add_argument("--yes", action="store_true")

    server = commands.add_parser("server", help="control the local engine")
    server_commands = server.add_subparsers(dest="server_command", required=True)
    for name in ("start", "restart"):
        command = server_commands.add_parser(name, help=f"{name} the local engine")
        _add_endpoint_arguments(command, include_host=True)
    server_stop = server_commands.add_parser("stop", help="stop the local engine")
    server_stop.add_argument("--port", type=int)
    server_status = server_commands.add_parser("status", help="show engine status")
    server_status.add_argument("--port", type=int)
    server_logs = server_commands.add_parser("logs", help="show the engine log")
    server_logs.add_argument("--lines", type=int, default=50)

    integration = commands.add_parser("integration", help="manage app integrations")
    integration_commands = integration.add_subparsers(
        dest="integration_command",
        required=True,
    )
    integration_commands.add_parser("list", help="list supported integrations")
    integration_add = integration_commands.add_parser("add", help="add an integration")
    integration_add.add_argument("integration", choices=("t3",))
    integration_add.add_argument("--plan", action="store_true")
    integration_add.add_argument("--yes", action="store_true")
    integration_remove = integration_commands.add_parser(
        "remove",
        help="remove an integration",
    )
    integration_remove.add_argument("integration", choices=("t3",))
    integration_remove.add_argument("--plan", action="store_true")
    integration_remove.add_argument("--yes", action="store_true")

    uninstall_command = commands.add_parser("uninstall", help="uninstall Geer from this Mac")
    uninstall_command.add_argument("--yes", action="store_true")

    # Advanced commands retained for development and driver integration.
    assets = commands.add_parser("assets", help=argparse.SUPPRESS)
    assets.add_argument("--source", type=Path, default=DEFAULT_MODEL_SOURCE)
    assets.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    commands.add_parser("runtime", help=argparse.SUPPRESS)
    serve = commands.add_parser("serve", help=argparse.SUPPRESS)
    _add_endpoint_arguments(serve, include_host=True)
    probe = commands.add_parser("canary", help=argparse.SUPPRESS)
    _add_endpoint_arguments(probe)
    probe.add_argument("--prompt", default="Reply with exactly: GEER_LOCAL_OK")
    probe.add_argument("--max-tokens", type=int, default=32)
    probe.add_argument("--timeout", type=float, default=600)
    commands.add_parser("retrieval-canary", help=argparse.SUPPRESS)
    claude = commands.add_parser("claude", help=argparse.SUPPRESS)
    _add_endpoint_arguments(claude)
    launch = commands.add_parser("launch", help=argparse.SUPPRESS)
    _add_endpoint_arguments(launch)
    public = {
        "setup",
        "uninstall",
        "status",
        "doctor",
        "stats",
        "model",
        "server",
        "integration",
    }
    commands._choices_actions = [  # type: ignore[attr-defined]
        choice
        for choice in commands._choices_actions  # type: ignore[attr-defined]
        if choice.dest in public
    ]
    return root


def _add_endpoint_arguments(
    command: argparse.ArgumentParser,
    *,
    include_host: bool = False,
) -> None:
    if include_host:
        command.add_argument("--host", default=DEFAULT_HOST)
    command.add_argument("--port", type=int)


def normalize_arguments(arguments: list[str]) -> list[str]:
    if not arguments:
        return arguments
    normalized = list(arguments)
    replacement = ROOT_ALIASES.get(normalized[0])
    if replacement:
        normalized[:1] = replacement.split()
    if len(normalized) >= 2:
        aliases = GROUP_ALIASES.get(normalized[0], {})
        normalized[1] = aliases.get(normalized[1], normalized[1])
    return normalized


def _confirm_remove(name: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise SetupError("interactive consent is required; rerun with `--yes`")
    return input(f"Remove {name} and reclaim its model cache? [y/N] ").strip().lower() in {
        "y",
        "yes",
    }


def _confirm_t3_remove(*, assume_yes: bool, plan_only: bool) -> bool:
    if assume_yes or plan_only:
        return True
    if not sys.stdin.isatty():
        raise SetupError("interactive consent is required; rerun with `--yes` or `--plan`")
    return input("Remove Geer from T3 Code? [y/N] ").strip().lower() in {
        "y",
        "yes",
    }


def _print_status(result: dict[str, Any]) -> None:
    online = result.get("server") == "online"
    print("Geer is ready\n")
    models = result.get("models", {}).get("data", [])
    model = models[0].get("id") if models else result.get("model_id", "unknown")
    print(f"Model      {model}")
    print(f"Server     {'Running' if online else 'Stopped — starts on demand'}")
    retrieval = result.get("retrieval", {})
    print(f"Retrieval  {'Ready' if retrieval.get('provider') == 'semble' else 'Unavailable'}")


def _print_models(result: dict[str, Any]) -> None:
    for model in result["models"]:
        marker = "●" if model["active"] else "○"
        state = "active" if model["active"] else "available"
        print(f"{marker} {model['name']}  {model['download']}  {state}")


def _tail(path: Path, lines: int) -> str:
    if not path.is_file():
        return f"No server log exists yet at {path}"
    return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])


def main(arguments: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if arguments is None else arguments)
    json_output = "--json" in raw
    raw = [argument for argument in raw if argument != "--json"]
    normalized = normalize_arguments(raw)
    root = parser()
    options, remaining = root.parse_known_args(normalized)
    workspace = None
    if remaining and options.command not in {"claude", "launch"}:
        root.error(f"unrecognized arguments: {' '.join(remaining)}")
    try:
        workspace = find_workspace()
        if options.command is None:
            if setup_complete(workspace):
                result = runtime_status(workspace, server_endpoint(workspace))
                _print_status(result)
                return 0
            setup(workspace)
            return 0
        if options.command == "setup":
            arguments = {
                "assume_yes": options.yes,
                "skip_t3": options.skip_t3,
                "high_performance": options.high_performance_download,
                "plan_only": options.plan,
            }
            if options.frontend == "json-v1":
                run_json_setup(
                    lambda frontend: setup(
                        workspace,
                        frontend=frontend,
                        **arguments,
                    )
                )
            else:
                setup(workspace, **arguments)
            return 0
        if options.command == "uninstall":
            uninstall(workspace, assume_yes=options.yes)
            return 0
        if options.command == "status":
            result = runtime_status(workspace, server_endpoint(workspace))
            if not json_output:
                _print_status(result)
                return 0
        elif options.command == "doctor":
            result = doctor(workspace, server_endpoint(workspace))
        elif options.command == "stats":
            result = snapshot_runtime(workspace, server_endpoint(workspace))
        elif options.command == "model":
            if options.model_command == "list":
                result = list_models(workspace)
                if not json_output:
                    _print_models(result)
                    return 0
            elif options.model_command == "add":
                require_model_name(options.model)
                result = install_model(
                    workspace,
                    high_performance=options.high_performance_download,
                )
            elif options.model_command == "use":
                result = use_model(workspace, options.model)
            elif options.model_command == "remove":
                if not _confirm_remove(options.model, options.yes):
                    print("Nothing was removed.")
                    return 0
                stop_server(workspace)
                result = remove_model(workspace, options.model)
            else:
                raise AssertionError(options.model_command)
        elif options.command == "server":
            if options.server_command == "start":
                result = start_server(workspace, options.host, options.port)
            elif options.server_command == "stop":
                result = stop_server(workspace, options.port)
            elif options.server_command == "restart":
                stop_server(workspace, options.port)
                result = start_server(workspace, options.host, options.port)
            elif options.server_command == "status":
                result = runtime_status(
                    workspace,
                    server_endpoint(workspace, port=options.port),
                )
                if not json_output:
                    print("Running" if result["server"] == "online" else "Stopped")
                    return 0
            elif options.server_command == "logs":
                print(_tail(workspace.server_log, options.lines))
                return 0
            else:
                raise AssertionError(options.server_command)
        elif options.command == "integration":
            if options.integration_command == "list":
                result = {
                    "integrations": [
                        {
                            "id": "t3",
                            "name": "T3 Code",
                            "configured": _t3_configured(),
                        }
                    ]
                }
                if not json_output:
                    state = "configured" if result["integrations"][0]["configured"] else "available"
                    print(f"T3 Code  {state}")
                    return 0
            elif options.integration_command == "add":
                result = add_t3_integration(
                    workspace,
                    assume_yes=options.yes,
                    plan_only=options.plan,
                )
            elif options.integration_command == "remove":
                settings = Path("~/.t3/userdata/settings.json").expanduser()
                if not _confirm_t3_remove(
                    assume_yes=options.yes,
                    plan_only=options.plan,
                ):
                    print("No T3 Code settings were changed.")
                    return 0
                result = remove_t3(settings, dry_run=options.plan)
            else:
                raise AssertionError(options.integration_command)
        elif options.command == "assets":
            result = initialize_assets(workspace, options.source, options.model_id)
        elif options.command == "runtime":
            result = bootstrap_runtime(workspace)
        elif options.command == "serve":
            return run_server(
                workspace,
                options.host,
                options.port if options.port is not None else DEFAULT_PORT,
            )
        elif options.command == "canary":
            result = canary(
                workspace,
                server_endpoint(workspace, port=options.port),
                options.prompt,
                options.max_tokens,
                options.timeout,
            )
        elif options.command == "retrieval-canary":
            result = retrieval_canary(workspace)
        elif options.command == "claude":
            launch_claude(workspace, remaining, port=options.port)
        elif options.command == "launch":
            launch_claude(
                workspace,
                remaining,
                ensure_server=True,
                port=options.port,
            )
        else:
            raise AssertionError(f"unhandled command: {options.command}")
    except (AssetError, ProtocolError, SetupError, UnsupportedHardwareError) as error:
        if getattr(options, "command", None) == "launch" and workspace is not None:
            try:
                record_launcher_status(
                    workspace,
                    "error",
                    endpoint_url=server_endpoint(
                        workspace,
                        port=getattr(options, "port", None),
                    ),
                    detail=str(error),
                )
            except OSError:
                pass
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _t3_configured() -> bool:
    settings = Path("~/.t3/userdata/settings.json").expanduser()
    try:
        value = json.loads(settings.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return "claudeGeer" in value.get("providerInstances", {})


if __name__ == "__main__":
    raise SystemExit(main())
