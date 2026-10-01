from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from geer.assets import AssetError, Workspace
from geer.hardware import GIB
from geer.pi_distribution import PI_REVISION, PI_VERSION
from geer.retrieval import RETRIEVAL_SYSTEM_PROMPT
from geer.runtime import (
    bootstrap_pi,
    canary,
    compatible_pi,
    ensure_pi_config,
    launcher_status,
    pi_arguments,
    pi_environment,
    prepare_pi,
    record_launcher_status,
    runtime_status,
)


def test_pi_environment_isolates_credentials_and_keeps_shell_configuration(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    inherited = {
        "HOME": str(tmp_path / "user"),
        "PATH": "/usr/bin:/bin",
        "EDITOR": "vim",
        "ANTHROPIC_AUTH_TOKEN": "private-anthropic",
        "OPENAI_API_KEY": "private-openai",
        "XAI_API_KEY": "private-xai",
        "AWS_PROFILE": "personal",
        "GOOGLE_APPLICATION_CREDENTIALS": "/private/gcloud.json",
        "HF_TOKEN": "private-huggingface",
        "PI_CODING_AGENT_DIR": "/private/pi",
        "PI_CODING_AGENT_SESSION_DIR": "/private/sessions",
        "PI_TELEMETRY": "1",
        "PI_OFFLINE": "0",
        "GEER_API_KEY": "old-geer",
        "NO_PROXY": "example.local",
    }

    environment = pi_environment(workspace, "local-geer", inherited=inherited)

    assert environment["HOME"] == inherited["HOME"]
    assert environment["PATH"] == inherited["PATH"]
    assert environment["EDITOR"] == "vim"
    assert not any(value.startswith("private-") for value in environment.values())
    assert "AWS_PROFILE" not in environment
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in environment
    assert environment["PI_CODING_AGENT_DIR"] == str(workspace.pi_config)
    assert environment["PI_CODING_AGENT_SESSION_DIR"] == str(workspace.pi_config / "sessions")
    assert environment["GEER_API_KEY"] == "local-geer"
    assert environment["PI_OFFLINE"] == "1"
    assert environment["PI_TELEMETRY"] == "0"
    assert "127.0.0.1" in environment["NO_PROXY"]
    assert "example.local" in environment["NO_PROXY"]
    assert inherited["OPENAI_API_KEY"] == "private-openai"


@pytest.mark.parametrize(
    ("memory_gib", "context_window"),
    [(32, 65_536), (48, 131_072), (64, 262_144)],
)
def test_pi_config_routes_to_authenticated_local_openai_endpoint(
    tmp_path: Path,
    memory_gib: int,
    context_window: int,
) -> None:
    workspace = Workspace(tmp_path)
    manifest = {
        "model_id": "geer-local",
        "context_length": 262_144,
        "build": {"display_name": "Geer Test Model"},
    }
    legacy = workspace.runtime / "claude" / "auth.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"private": "retained"}')

    status = ensure_pi_config(
        workspace,
        "http://127.0.0.1:9123",
        manifest,
        total_memory_bytes=memory_gib * GIB,
    )

    config = json.loads((workspace.pi_config / "models.json").read_text())
    provider = config["providers"]["geer"]
    assert provider["api"] == "openai-completions"
    assert provider["baseUrl"] == "http://127.0.0.1:9123/v1"
    assert provider["apiKey"] == "${GEER_API_KEY}"
    assert provider["authHeader"] is True
    assert provider["models"][0]["id"] == "geer-local"
    assert provider["models"][0]["name"] == "Geer Test Model"
    assert provider["models"][0]["contextWindow"] == context_window
    assert provider["models"][0]["compat"]["thinkingFormat"] == "qwen-chat-template"
    assert status["context_window"] == context_window
    settings = json.loads((workspace.pi_config / "settings.json").read_text())
    assert settings["enableInstallTelemetry"] is False
    assert settings["enableAnalytics"] is False
    mcp = json.loads((workspace.pi_config / "mcp.json").read_text())
    assert mcp["mcpServers"]["semble"]["exposure"] == "direct"
    assert workspace.pi_config.stat().st_mode & 0o777 == 0o700
    assert all(
        (workspace.pi_config / name).stat().st_mode & 0o777 == 0o600
        for name in ("models.json", "settings.json", "mcp.json")
    )
    assert legacy.read_text() == '{"private": "retained"}'
    assert not (workspace.pi_config / "auth.json").exists()


def test_pi_config_preserves_unrelated_settings_and_refuses_symlinked_config(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.pi_config.mkdir(parents=True)
    settings = workspace.pi_config / "settings.json"
    settings.write_text('{"theme": "light"}')
    manifest = {"model_id": "geer-local", "context_length": 65_536}

    ensure_pi_config(workspace, "http://127.0.0.1:9123", manifest, total_memory_bytes=32 * GIB)

    assert json.loads(settings.read_text())["theme"] == "light"
    external = tmp_path / "other-models.json"
    external.write_text('{"private": "preserved"}')
    models = workspace.pi_config / "models.json"
    models.unlink()
    models.symlink_to(external)
    with pytest.raises(AssetError, match="configuration symlink"):
        ensure_pi_config(workspace, "http://127.0.0.1:9123", manifest, total_memory_bytes=32 * GIB)
    assert external.read_text() == '{"private": "preserved"}'


def test_parallel_title_and_chat_preparation_share_configuration_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace(tmp_path)
    manifest = {"model_id": "geer-local", "context_length": 65_536}

    def prepare(number: int) -> None:
        ensure_pi_config(
            workspace,
            "http://127.0.0.1:9123",
            manifest,
            total_memory_bytes=32 * GIB,
        )
        record_launcher_status(workspace, "ready", endpoint_url="http://127.0.0.1:9123")

    with ThreadPoolExecutor(max_workers=4) as threads:
        list(threads.map(prepare, range(12)))

    assert (workspace.runtime / "skills").is_dir()
    assert not (workspace.runtime / "skills").is_symlink()
    assert not (workspace.pi_config / "skills").exists()
    assert json.loads((workspace.pi_config / "models.json").read_text())["providers"]["geer"]
    assert launcher_status(workspace)["status"] == "ready"
    assert not list(workspace.pi_config.glob(".*.tmp"))


def test_pi_arguments_keep_native_protocol_and_explicit_mcp_extension(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    manifest = {"model_id": "geer-local", "build": {"display_name": "Geer Test Model"}}
    extension = tmp_path / "session.mjs"
    arguments = ["--mode", "rpc", "--extension", str(extension)]

    result = pi_arguments(workspace, arguments, manifest)

    assert result[:4] == ["--provider", "geer", "--model", "geer-local"]
    assert "--no-approve" in result
    assert "--no-extensions" in result
    assert result[result.index("--extension") + 1] == "builtin:mcp"
    assert "powered by Geer Test Model" in result[result.index("--append-system-prompt") + 1]
    assert RETRIEVAL_SYSTEM_PROMPT in result[result.index("--append-system-prompt") + 1]
    assert result[-4:] == arguments

    text_only = pi_arguments(workspace, ["--mode", "rpc"], manifest, enable_tools=False)
    assert "--no-tools" in text_only
    assert "--extension" not in text_only
    assert "--skill" not in text_only
    assert RETRIEVAL_SYSTEM_PROMPT not in text_only


def test_chat_borrows_existing_skills_but_text_generation_loads_none(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "repo", tmp_path / ".geer")
    private = workspace.runtime / "skills"
    shared = tmp_path / ".agents/skills"
    claude = tmp_path / ".claude/skills"
    for path in (private, shared, claude):
        path.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    manifest = {"model_id": "geer-local"}

    chat = pi_arguments(workspace, ["--mode", "rpc"], manifest)
    paths = [chat[index + 1] for index, value in enumerate(chat) if value == "--skill"]
    assert paths == [str(private), str(shared), str(claude)]
    assert "--no-skills" in chat
    text = pi_arguments(workspace, ["--mode", "rpc"], manifest, enable_tools=False)
    assert "--skill" not in text


def test_compatible_pi_requires_exact_pinned_version_without_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace(tmp_path)
    command = tmp_path / "pi"
    command.write_text(f"#!/bin/sh\nprintf '%s\\n' '{PI_VERSION}'\n")
    command.chmod(0o755)
    monkeypatch.setenv("GEER_PI_BIN", str(command))
    monkeypatch.setattr("geer.runtime.install_pi", lambda *args: pytest.fail("unexpected download"))

    assert compatible_pi(workspace) == str(command)
    assert not workspace.runtime.exists()
    command.write_text("#!/bin/sh\nprintf '%s\\n' '0.99.0'\n")
    with pytest.raises(AssetError, match="requires pinned Pi"):
        compatible_pi(workspace)
    assert not workspace.runtime.exists()


def test_pi_version_probe_can_respect_t3_inspection_timeout(tmp_path, monkeypatch):
    command = tmp_path / "slow-pi"
    command.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(5)\nprint('0.99.1')\n")
    command.chmod(0o755)
    monkeypatch.setenv("GEER_PI_BIN", str(command))
    started = time.monotonic()

    with pytest.raises(AssetError, match="cannot run Pi"):
        compatible_pi(Workspace(tmp_path), timeout=0.05)

    assert time.monotonic() - started < 1.5


def test_bootstrap_pi_verifies_managed_runtime_on_every_explicit_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace(tmp_path)
    monkeypatch.delenv("GEER_PI_BIN", raising=False)
    installed: list[Path] = []

    def compatible(value: Workspace) -> str:
        if not installed:
            raise AssetError("missing")
        return str(installed[0] / "pi" / "pi")

    monkeypatch.setattr("geer.runtime.compatible_pi", compatible)
    monkeypatch.setattr("geer.runtime.install_pi", lambda path: installed.append(path))

    first = bootstrap_pi(workspace)
    assert installed == [workspace.runtime / "pi-runtime"]
    assert first["version"] == PI_VERSION
    assert first["revision"] == PI_REVISION
    assert bootstrap_pi(workspace) == first
    assert installed == [workspace.runtime / "pi-runtime"] * 2


def test_bootstrap_pi_allows_explicit_development_override_without_installing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace(tmp_path)
    command = tmp_path / "pi"
    command.write_text(f"#!/bin/sh\nprintf '%s\\n' '{PI_VERSION}'\n")
    command.chmod(0o755)
    monkeypatch.setenv("GEER_PI_BIN", str(command))
    monkeypatch.setattr(
        "geer.runtime.install_pi",
        lambda *args: pytest.fail("unexpected installation"),
    )

    result = bootstrap_pi(workspace)

    assert result["pi"] == str(command)
    assert not workspace.runtime.exists()


@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("t3_ready", [True, False])
def test_runtime_status_reports_pi_availability_without_installing_or_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available: bool,
    t3_ready: bool,
) -> None:
    workspace = Workspace(tmp_path)
    monkeypatch.setattr(
        "geer.runtime.verify_assets",
        lambda value, **kwargs: {"model_id": "geer-local"},
    )
    monkeypatch.setattr("geer.runtime.omlx_command", lambda value: "/bin/omlx")
    monkeypatch.setattr("geer.runtime.retrieval_status", lambda value: {"provider": "semble"})
    monkeypatch.setattr("geer.runtime.get_json", lambda *args, **kwargs: {})
    monkeypatch.setattr("geer.runtime.t3_status", lambda value: {"ready": t3_ready})
    for name in ("install_pi", "start_server"):
        monkeypatch.setattr(
            f"geer.runtime.{name}",
            lambda *args, **kwargs: pytest.fail("unexpected startup"),
        )

    def compatible(value: Workspace) -> str:
        if not available:
            raise AssetError("Pi is unavailable")
        return "/bin/pi"

    monkeypatch.setattr("geer.runtime.compatible_pi", compatible)

    status = runtime_status(workspace, "http://127.0.0.1:9123")

    assert "claude" not in status
    assert status["t3"]["ready"] is t3_ready
    assert status["ready"] is (available and t3_ready)
    assert status["pi"]["required_version"] == PI_VERSION
    assert status["pi"]["config_directory"] == str(workspace.pi_config)
    if available:
        assert status["pi"]["status"] == "ready"
        assert status["pi"]["version"] == PI_VERSION
    else:
        assert status["pi"]["status"] == "unavailable"
        assert status["pi"]["error"] == "Pi is unavailable"


@pytest.mark.parametrize("arguments", [["--version"], ["--help"]])
def test_pi_preflight_does_not_touch_models_state_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    workspace = Workspace(tmp_path)
    monkeypatch.setattr("geer.runtime.compatible_pi", lambda value: "/bin/pi")
    for name in ("start_server", "verify_assets", "install_pi", "ensure_pi_config"):
        monkeypatch.setattr(
            f"geer.runtime.{name}",
            lambda *args, **kwargs: pytest.fail("preflight mutation"),
        )

    command, environment = prepare_pi(workspace, arguments, ensure_server=True)

    assert command == ["/bin/pi", *arguments]
    assert environment["PI_OFFLINE"] == "1"
    assert not workspace.runtime.exists()


def test_prepare_pi_records_endpoint_and_keeps_api_key_out_of_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace(tmp_path)
    manifest = {"model_id": "geer-local", "context_length": 262_144}
    monkeypatch.setattr("geer.runtime.compatible_pi", lambda value: "/bin/pi")
    monkeypatch.setattr("geer.runtime.verify_assets", lambda value: manifest)
    monkeypatch.setattr(
        "geer.runtime.start_server",
        lambda *args, **kwargs: {"endpoint": "http://127.0.0.1:9123"},
    )
    configured: list[str] = []
    monkeypatch.setattr(
        "geer.runtime.ensure_pi_config",
        lambda workspace, base_url, manifest: configured.append(base_url),
    )

    command, environment = prepare_pi(workspace, ["--mode", "rpc"], ensure_server=True)

    assert command[0] == "/bin/pi"
    assert command[-2:] == ["--mode", "rpc"]
    assert configured == ["http://127.0.0.1:9123"]
    assert environment["GEER_API_KEY"] not in command
    assert environment["GEER_API_KEY"] == workspace.api_key.read_text().strip()
    status = launcher_status(workspace)
    assert status is not None
    assert status["endpoint"] == "http://127.0.0.1:9123"
    assert environment["GEER_API_KEY"] not in workspace.launcher_log.read_text()


def test_openai_canary_records_usage_without_prompt_or_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace(tmp_path)
    monkeypatch.setattr("geer.runtime.verify_assets", lambda value: {"model_id": "geer-local"})

    def post(url: str, payload: dict, timeout: float, api_key: str) -> dict:
        assert url.endswith("/v1/chat/completions")
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        return {
            "model": "geer-local",
            "choices": [{"message": {"content": "private-completion"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
                "private": "private-completion",
                "completion_tokens_details": {"reasoning_tokens": 2, "text": "private-completion"},
            },
        }

    monkeypatch.setattr("geer.runtime.post_json", post)
    result = canary(workspace, "http://127.0.0.1:9123", "private-prompt", 64, 10)
    assert result["metrics"]["stop_reason"] == "stop"
    assert result["metrics"]["usage"]["total_tokens"] == 30
    assert result["metrics"]["usage"]["completion_tokens_details"] == {"reasoning_tokens": 2}
    assert "private-prompt" not in workspace.requests.read_text()
    assert "private-completion" not in workspace.requests.read_text()

    def fail(*args, **kwargs):
        raise AssetError("server echoed private-prompt")

    monkeypatch.setattr("geer.runtime.post_json", fail)
    with pytest.raises(AssetError, match="echoed"):
        canary(workspace, "http://127.0.0.1:9123", "private-prompt", 64, 10)
    assert "private-prompt" not in workspace.requests.read_text()
