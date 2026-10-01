from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .assets import AssetError, Workspace

_INSPECTION_TIMEOUT = 3.0
_INSPECTION_ID = "geer-skills"


def ensure_skills_directory(workspace: Workspace) -> Path:
    """Keep Geer's own skills in a real private directory without migrating other roots."""
    directory = workspace.runtime / "skills"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise AssetError(f"refusing to replace unexpected Geer skills path: {directory}")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def _home_path(value: str, home: Path) -> Path:
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return Path(value)


def _existing_directory(path: Path) -> Path | None:
    try:
        directory = path.resolve(strict=True)
        return directory if directory.is_dir() else None
    except (OSError, RuntimeError):
        return None


def skill_paths(
    workspace: Workspace,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[Path]:
    """Reference existing user skills in order; never copy or change another harness."""
    home = Path.home() if home is None else home
    environ = os.environ if environ is None else environ
    shared = home / ".agents" / "skills"
    legacy = workspace.pi_config / "skills"
    pi_agent = _home_path(environ.get("PI_CODING_AGENT_DIR") or "~/.pi/agent", home)
    config_home = _home_path(environ.get("XDG_CONFIG_HOME") or "~/.config", home)

    candidates = [workspace.runtime / "skills"]
    # An old shared-skills link belongs at the shared root's normal priority.
    # Other legacy directories and references remain available in place.
    legacy_directory = _existing_directory(legacy)
    if legacy_directory is not None and legacy_directory != _existing_directory(shared):
        candidates.append(legacy)
    candidates.extend([
        shared,
        pi_agent / "skills",
        home / ".claude" / "skills",
        home / ".codex" / "skills",
        config_home / "opencode" / "skills",
        home / ".cursor" / "skills",
    ])

    paths: list[Path] = []
    for candidate in candidates:
        directory = _existing_directory(candidate)
        if directory is not None and directory not in paths:
            paths.append(directory)
    return paths


def inspect_skill_commands(
    pi_command: str | Path,
    paths: Iterable[Path],
    environment: Mapping[str, str],
    *,
    timeout: float = _INSPECTION_TIMEOUT,
) -> list[dict[str, Any]]:
    """Ask pinned Pi for its skill catalog without inference or user configuration writes."""
    deadline = time.monotonic() + timeout
    executable = str(pi_command)
    if isinstance(pi_command, Path) or os.sep in executable:
        executable = str(Path(executable).expanduser().resolve())
    with tempfile.TemporaryDirectory(prefix="geer-skills-") as temporary:
        root = Path(temporary).resolve()
        agent = root / "agent"
        agent.mkdir(mode=0o700)
        settings = {"enableInstallTelemetry": False, "enableAnalytics": False}
        models = {"providers": {"geer-skills-probe": {
            "api": "openai-completions",
            "baseUrl": "http://127.0.0.1:1/v1",
            "apiKey": "metadata-only-unused",
            "models": [{
                "id": "metadata-only",
                "name": "Metadata only",
                "reasoning": False,
                "input": ["text"],
                "contextWindow": 65_536,
                "maxTokens": 4096,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            }],
        }}}
        for name, value in (("settings.json", settings), ("models.json", models)):
            path = agent / name
            path.write_text(json.dumps(value), encoding="utf-8")
            path.chmod(0o600)

        isolated = dict(environment)
        isolated.update({
            "HOME": str(root),
            "PI_CODING_AGENT_DIR": str(agent),
            "PI_CODING_AGENT_SESSION_DIR": str(root / "sessions"),
            "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
        })
        arguments = [
            executable, "--mode", "rpc", "--offline", "--no-session",
            "--no-approve", "--no-context-files", "--no-tools", "--no-extensions",
            "--no-skills", "--no-prompt-templates", "--no-themes",
            "--provider", "geer-skills-probe", "--model", "metadata-only",
        ]
        for path in paths:
            arguments.extend(["--skill", str(path)])
        request = json.dumps({"id": _INSPECTION_ID, "type": "get_commands"}) + "\n"
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(arguments, timeout)
            result = subprocess.run(
                arguments,
                input=request,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=root,
                env=isolated,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired as error:
            raise AssetError(
                "Pi skill inspection timed out; retry the T3 provider check"
            ) from error
        except (OSError, UnicodeError) as error:
            raise AssetError(f"cannot inspect Pi skills at {pi_command}: {error}") from error

    if result.returncode != 0:
        detail = result.stderr.strip()[-1000:] or "no diagnostics"
        raise AssetError(f"Pi skill inspection failed (exit {result.returncode}): {detail}")
    if result.stderr:
        sys.stderr.write(result.stderr)

    response: dict[str, Any] | None = None
    try:
        for line in result.stdout.splitlines():
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("expected RPC objects")
            if record.get("id") == _INSPECTION_ID:
                if response is not None:
                    raise ValueError("duplicate inspection response")
                response = record
    except (json.JSONDecodeError, ValueError) as error:
        raise AssetError("Pi skill inspection returned invalid RPC JSON") from error
    if (
        response is None
        or response.get("type") != "response"
        or response.get("command") != "get_commands"
    ):
        raise AssetError("Pi skill inspection returned no get_commands response")
    if response.get("success") is not True:
        raise AssetError(f"Pi skill inspection rejected get_commands: {response.get('error')}")
    data = response.get("data")
    commands = data.get("commands") if isinstance(data, dict) else None
    if not isinstance(commands, list) or any(not isinstance(item, dict) for item in commands):
        raise AssetError("Pi skill inspection returned an invalid command catalog")
    skills = [command for command in commands if command.get("source") == "skill"]
    for command in skills:
        name = command.get("name")
        source = command.get("sourceInfo")
        path = source.get("path") if isinstance(source, dict) else None
        if (
            not isinstance(name, str)
            or not name.startswith("skill:")
            or not name[6:]
            or not isinstance(path, str)
            or not path
            or not isinstance(command.get("description"), str)
        ):
            raise AssetError("Pi skill inspection returned invalid skill metadata")
    if time.monotonic() >= deadline:
        raise AssetError("Pi skill inspection timed out; retry the T3 provider check")
    return skills
