from __future__ import annotations

import inspect
import json
import socket
from pathlib import Path

import pytest

from geer.assets import AssetError, Workspace, model_alias
from geer.cli import parser
from geer.hardware import GIB
from geer.prompt import geer_system_prompt
from geer.retrieval import RETRIEVAL_SYSTEM_PROMPT, retrieval_mcp_config
from geer.runtime import (
    CACHE_MAX_SIZE,
    CLAUDE_INSTALL_URL,
    HOT_CACHE_MAX_SIZE,
    OMLX_ARCHIVE_URL,
    OMLX_OVERRIDES,
    OMLX_REVISION,
    STARTUP_TIMEOUT,
    _available_port,
    _install_runtime_components,
    _replace_runtime_prefix,
    cache_directory,
    claude_arguments,
    claude_environment,
    compatible_claude,
    default_port,
    ensure_api_key,
    ensure_model_settings,
    launch_claude,
    launcher_status,
    run_server,
    serve_command,
    snapshot_runtime,
    start_server,
    stop_server,
)


def test_runtime_prefix_replacement_updates_venv_and_python_config(
    tmp_path: Path,
) -> None:
    venv_config = tmp_path / "omlx/pyvenv.cfg"
    python_config = tmp_path / "python/lib/python3.13/_sysconfigdata_test.py"
    venv_config.parent.mkdir(parents=True)
    python_config.parent.mkdir(parents=True)
    venv_config.write_text("home = __PREFIX__/bin\n")
    python_config.write_text("prefix = '__PREFIX__'\n")

    _replace_runtime_prefix(tmp_path, "__PREFIX__", "/opt/geer/python")

    assert venv_config.read_text() == "home = /opt/geer/python/bin\n"
    assert python_config.read_text() == "prefix = '/opt/geer/python'\n"


def test_runtime_install_preserves_models_and_cache(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    model = workspace.models / "candidates/test/model.safetensors"
    model.parent.mkdir(parents=True)
    model.write_text("weights")
    archive = workspace.cache / "runtime.tar.gz"
    archive.parent.mkdir(parents=True)
    archive.write_text("cached")
    for name in ("omlx", "python"):
        component = workspace.runtime / name
        component.mkdir()
        (component / "old").write_text("old")
    (workspace.runtime / "runtime.json").write_text('{"version": "old"}')

    staged = tmp_path.with_name(f"{tmp_path.name}.runtime.installing")
    for name in ("omlx", "python"):
        component = staged / name
        component.mkdir(parents=True)
        (component / "new").write_text("new")
    (staged / "runtime.json").write_text('{"version": "new"}')

    _install_runtime_components(staged, workspace.runtime)

    assert model.read_text() == "weights"
    assert archive.read_text() == "cached"
    assert (workspace.runtime / "omlx/new").read_text() == "new"
    assert (workspace.runtime / "python/new").read_text() == "new"
    assert json.loads((workspace.runtime / "runtime.json").read_text()) == {"version": "new"}


def test_claude_environment_routes_every_model_tier_locally() -> None:
    environment = claude_environment(
        "http://127.0.0.1:8765",
        "geer-local",
        "local-secret",
        Path("/tmp/geer-claude"),
        262_144,
    )

    assert environment["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8765"
    assert environment["ANTHROPIC_API_KEY"] == "local-secret"
    assert "ANTHROPIC_AUTH_TOKEN" not in environment
    assert environment["ANTHROPIC_MODEL"] == "geer-local"
    assert {
        environment["ANTHROPIC_DEFAULT_OPUS_MODEL"],
        environment["ANTHROPIC_DEFAULT_SONNET_MODEL"],
        environment["ANTHROPIC_DEFAULT_HAIKU_MODEL"],
    } == {"geer-local"}
    assert environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert environment["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "262144"
    assert environment["ENABLE_TOOL_SEARCH"] == "false"
    assert environment["CLAUDE_CONFIG_DIR"] == "/tmp/geer-claude"
    assert len(OMLX_REVISION) == 40
    assert OMLX_REVISION in OMLX_ARCHIVE_URL
    assert "git+" not in OMLX_ARCHIVE_URL
    assert len(OMLX_OVERRIDES) == 4
    assert all("git+" not in override for override in OMLX_OVERRIDES)
    assert all(override.endswith(".tar.gz") for override in OMLX_OVERRIDES)


def test_default_port_is_stable_and_separate_for_local_accounts() -> None:
    assert default_port(501) == 8765
    assert default_port(502) == 8766


def test_available_port_falls_back_when_preferred_port_is_occupied() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        occupied = listener.getsockname()[1]

        selected = _available_port("127.0.0.1", occupied)

    assert selected != occupied
    assert 1 <= selected <= 65535


def test_api_key_is_private_and_stable(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    first = ensure_api_key(workspace)
    second = ensure_api_key(workspace)

    assert first == second
    assert len(first) >= 32
    assert workspace.api_key.stat().st_mode & 0o777 == 0o600


def test_claude_arguments_use_minimal_explicit_configuration() -> None:
    workspace = Workspace(Path("/tmp/geer-test"))
    arguments = claude_arguments(workspace, ["--print", "hello"])
    assert arguments[:2] == [
        "--system-prompt",
        geer_system_prompt(),
    ]
    assert arguments[2:4] == ["--strict-mcp-config", "--mcp-config"]
    assert json.loads(arguments[4]) == retrieval_mcp_config(workspace)
    assert arguments[5:7] == ["--append-system-prompt", RETRIEVAL_SYSTEM_PROMPT]
    assert arguments[-2:] == ["--print", "hello"]
    assert claude_arguments(workspace, ["--version"]) == ["--version"]


def test_claude_parser_leaves_cli_flags_for_claude_code() -> None:
    options, remaining = parser().parse_known_args(["claude", "-p", "hello"])

    assert options.command == "claude"
    assert remaining == ["-p", "hello"]


def test_launch_parser_leaves_cli_flags_for_t3() -> None:
    options, remaining = parser().parse_known_args(["launch", "--resume", "thread"])

    assert options.command == "launch"
    assert remaining == ["--resume", "thread"]


def test_missing_claude_is_actionable_without_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("geer.runtime.shutil.which", lambda name: None)

    with pytest.raises(AssetError, match="official instructions") as error:
        compatible_claude()

    assert CLAUDE_INSTALL_URL in str(error.value)


def test_serve_command_refuses_non_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("geer.runtime.verify_assets", lambda workspace: {})
    monkeypatch.setattr(
        "geer.runtime.omlx_command",
        lambda workspace: "/usr/local/bin/omlx",
    )

    with pytest.raises(AssetError, match="loopback"):
        serve_command(Workspace(Path("/tmp/geer-test")), "0.0.0.0", 8765)


def test_serve_command_isolates_state_and_disables_external_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("geer.runtime.verify_assets", lambda workspace: {})
    monkeypatch.setattr(
        "geer.runtime.omlx_command",
        lambda workspace: "/usr/local/bin/omlx",
    )
    workspace = Workspace(Path("/tmp/geer-test"))

    command = serve_command(workspace)

    assert command[0:2] == ["/usr/local/bin/omlx", "serve"]
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert (
        command[command.index("--model-dir") + 1]
        == "/tmp/geer-test/.geer/models/geer-local"
    )
    assert command[command.index("--base-path") + 1] == "/tmp/geer-test/.geer/omlx-state"
    cache = command[command.index("--paged-ssd-cache-dir") + 1]
    assert cache.startswith("/tmp/geer-test/.geer/caches/")
    assert command[command.index("--paged-ssd-cache-max-size") + 1] == CACHE_MAX_SIZE
    assert command[command.index("--hot-cache-max-size") + 1] == HOT_CACHE_MAX_SIZE
    assert "--no-cache" not in command
    assert "--no-hf-cache" in command


def test_cache_directory_is_stable_and_model_isolated(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    manifest = {
        "model_id": "geer-local",
        "architecture": ["Test"],
        "model_type": "test",
        "quantization": {"bits": 4},
        "files": [{"name": "weights", "bytes": 7, "sha256": "abc"}],
    }

    first = cache_directory(workspace, manifest)
    second = cache_directory(workspace, manifest)
    changed = cache_directory(
        workspace,
        {
            **manifest,
            "files": [{"name": "weights", "bytes": 7, "sha256": "def"}],
        },
    )

    assert first == second
    assert changed != first
    record = json.loads((workspace.state / "cache.json").read_text())
    assert record["directory"] == str(changed)
    assert record["omlx_revision"] == OMLX_REVISION
    assert (workspace.state / "cache.json").stat().st_mode & 0o777 == 0o600


def test_model_settings_pin_only_the_geer_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    (workspace.models / "geer-local").mkdir(parents=True)
    workspace.omlx_state.mkdir(parents=True)
    workspace.omlx_model_settings.write_text(
        json.dumps(
            {
                "version": 1,
                "models": {"other": {"is_pinned": False, "temperature": 0.2}},
            }
        )
    )
    monkeypatch.setattr(
        "geer.runtime.verify_assets",
        lambda value: {
            "model_id": "geer-local",
            "context_length": 262_144,
            "quantization": {"bits": 6},
        },
    )

    result = ensure_model_settings(workspace, total_memory_bytes=64 * GIB)

    assert "other" not in result["models"]
    assert result["models"]["geer-local"]["is_pinned"] is True
    assert result["models"]["geer-local"]["model_alias"] == model_alias(
        {"quantization": {"bits": 6}}
    )
    assert result["models"]["geer-local"]["is_default"] is True
    assert workspace.omlx_model_settings.stat().st_mode & 0o777 == 0o600


def test_model_settings_apply_context_and_bf16_kv_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    (workspace.models / "geer-local").mkdir(parents=True)
    monkeypatch.setattr(
        "geer.runtime.verify_assets",
        lambda value: {
            "model_id": "geer-local",
            "context_length": 262_144,
            "quantization": {"bits": 4, "high_bits": 8},
        },
    )

    result = ensure_model_settings(workspace, total_memory_bytes=48 * GIB)

    settings = result["models"]["geer-local"]
    assert settings["max_context_window"] == 131_072
    assert settings["turboquant_kv_enabled"] is False


def test_server_interrupt_exits_without_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("geer.runtime.serve_command", lambda *args: ["omlx", "serve"])
    monkeypatch.setattr("geer.runtime.ensure_model_settings", lambda workspace: {})

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("geer.runtime.subprocess.run", interrupt)

    assert run_server(Workspace(Path("/tmp/geer-test"))) == 130


def test_start_server_allows_long_model_loads() -> None:
    default = inspect.signature(start_server).parameters["timeout"].default

    assert default == STARTUP_TIMEOUT == 600


def test_start_server_reuses_managed_server_with_expected_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.state.mkdir(parents=True)
    workspace.server_pid.write_text("42\n")
    manifest = {
        "model_id": "geer-local",
        "build": {"display_name": "Geer Test Model"},
    }
    monkeypatch.setattr("geer.runtime.verify_assets", lambda value: manifest)
    monkeypatch.setattr(
        "geer.runtime._managed_server_process",
        lambda value, pid, port: pid == 42 and port == 8765,
    )

    def fake_get_json(
        url: str,
        timeout: float = 5,
        api_key: str | None = None,
    ):
        assert api_key
        if url.endswith("/health"):
            return {"status": "healthy", "default_model": "geer-local"}
        return {"data": [{"id": "Geer Test Model"}]}

    monkeypatch.setattr("geer.runtime.get_json", fake_get_json)

    assert start_server(workspace) == {
        "server": "online",
        "endpoint": "http://127.0.0.1:8765",
        "port": 8765,
        "pid": 42,
        "started": False,
    }


def test_start_server_persists_automatic_fallback_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    manifest = {
        "model_id": "geer-local",
        "build": {"display_name": "Geer Test Model"},
    }
    monkeypatch.setattr("geer.runtime.verify_assets", lambda value: manifest)
    monkeypatch.setattr("geer.runtime._available_port", lambda host, port: 9123)
    monkeypatch.setattr(
        "geer.runtime._managed_server_process",
        lambda value, pid, port: pid == 42 and port == 9123,
    )
    monkeypatch.setattr(
        "geer.runtime.serve_command",
        lambda *args: ["omlx", "serve"],
    )
    monkeypatch.setattr("geer.runtime.ensure_model_settings", lambda value: {})

    class RunningProcess:
        pid = 42
        returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(
        "geer.runtime.subprocess.Popen",
        lambda *args, **kwargs: RunningProcess(),
    )

    def fake_get_json(
        url: str,
        timeout: float = 5,
        api_key: str | None = None,
    ):
        assert api_key
        if ":8765/" in url:
            raise AssetError("offline")
        if url.endswith("/health"):
            return {"status": "healthy", "default_model": "geer-local"}
        return {"data": [{"id": "Geer Test Model"}]}

    monkeypatch.setattr("geer.runtime.get_json", fake_get_json)

    result = start_server(workspace)

    assert result["endpoint"] == "http://127.0.0.1:9123"
    assert result["port"] == 9123
    assert result["started"] is True
    endpoint_record = json.loads(workspace.server_endpoint.read_text())
    assert endpoint_record["port"] == 9123
    assert endpoint_record["endpoint"] == "http://127.0.0.1:9123"
    assert workspace.server_endpoint.stat().st_mode & 0o777 == 0o600


def test_launch_claude_records_selected_endpoint_before_exec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    manifest = {
        "model_id": "geer-local",
        "context_length": 262_144,
        "build": {"display_name": "Geer Test Model"},
    }
    monkeypatch.setattr("geer.runtime.verify_assets", lambda value: manifest)
    monkeypatch.setattr(
        "geer.runtime.start_server",
        lambda *args, **kwargs: {
            "server": "online",
            "endpoint": "http://127.0.0.1:9123",
        },
    )
    monkeypatch.setattr("geer.runtime.compatible_claude", lambda: "/bin/claude")
    monkeypatch.setattr(
        "geer.runtime.claude_arguments",
        lambda workspace, arguments, **kwargs: arguments,
    )

    def stop_before_exec(command, arguments, environment):
        assert environment["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "262144"
        raise RuntimeError("exec reached")

    monkeypatch.setattr("geer.runtime.os.execvpe", stop_before_exec)

    with pytest.raises(RuntimeError, match="exec reached"):
        launch_claude(workspace, ["--print", "hello"], ensure_server=True)

    record = launcher_status(workspace)
    assert record is not None
    assert record["status"] == "ready"
    assert record["endpoint"] == "http://127.0.0.1:9123"
    assert workspace.launcher_log.stat().st_mode & 0o777 == 0o600


def test_start_server_rejects_unmanaged_healthy_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.state.mkdir(parents=True)
    workspace.server_pid.write_text("42\n")
    monkeypatch.setattr(
        "geer.runtime.verify_assets",
        lambda value: {"model_id": "geer-local"},
    )
    monkeypatch.setattr(
        "geer.runtime.get_json",
        lambda url, timeout=5, api_key=None: {
            "status": "healthy",
            "default_model": "geer-local",
        },
    )
    monkeypatch.setattr(
        "geer.runtime._managed_server_process",
        lambda value, pid, port: False,
    )

    with pytest.raises(AssetError, match="matching Geer ownership record"):
        start_server(workspace, port=8765)


def test_stop_server_recovers_endpoint_owner_from_stale_pid_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.state.mkdir(parents=True)
    workspace.server_pid.write_text("99\n")
    workspace.server_endpoint.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "host": "127.0.0.1",
                "port": 9123,
                "pid": 42,
                "endpoint": "http://127.0.0.1:9123",
            }
        )
    )
    monkeypatch.setattr(
        "geer.runtime._managed_server_process",
        lambda value, pid, port: pid == 42 and port == 9123,
    )
    signals: list[tuple[int, int]] = []

    def fake_kill(pid: int, signal_number: int) -> None:
        if signal_number == 0 and signals:
            raise ProcessLookupError
        signals.append((pid, signal_number))

    monkeypatch.setattr("geer.runtime.os.kill", fake_kill)

    result = stop_server(workspace)

    assert result == {
        "server": "stopped",
        "pid": 42,
        "stopped": True,
    }
    assert signals[0][0] == 42


def test_start_server_rejects_wrong_model_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.state.mkdir(parents=True)
    workspace.server_pid.write_text("42\n")
    monkeypatch.setattr(
        "geer.runtime.verify_assets",
        lambda value: {"model_id": "geer-local"},
    )
    monkeypatch.setattr(
        "geer.runtime.get_json",
        lambda url, timeout=5, api_key=None: {
            "status": "healthy",
            "default_model": "another-model",
        },
    )
    monkeypatch.setattr(
        "geer.runtime._managed_server_process",
        lambda value, pid, port: True,
    )

    with pytest.raises(AssetError, match="does not match Geer model"):
        start_server(workspace, port=8765)


def test_start_server_surfaces_log_tail_when_process_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.state.mkdir(parents=True)
    monkeypatch.setattr(
        "geer.runtime.verify_assets",
        lambda value: {"model_id": "geer-local"},
    )
    monkeypatch.setattr(
        "geer.runtime.get_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssetError("offline")),
    )
    monkeypatch.setattr(
        "geer.runtime.serve_command",
        lambda *args: ["omlx", "serve"],
    )
    monkeypatch.setattr("geer.runtime.ensure_model_settings", lambda value: {})

    class ExitedProcess:
        pid = 42
        returncode = 9

        def poll(self):
            return self.returncode

    def exited_process(*args, **kwargs):
        kwargs["stdout"].write(b"loading model\nfatal startup detail\n")
        return ExitedProcess()

    monkeypatch.setattr("geer.runtime.subprocess.Popen", exited_process)

    with pytest.raises(AssetError, match="fatal startup detail"):
        start_server(workspace)


def test_runtime_snapshot_keeps_metrics_without_payloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "geer.runtime.get_json",
        lambda url, api_key=None: {
            "models_loaded": 1,
            "default_model": "geer-local",
            "total_requests": 2,
            "total_prompt_tokens": 100,
            "secret_key": "do-not-record",
        },
    )
    workspace = Workspace(tmp_path)

    record = snapshot_runtime(workspace, "http://127.0.0.1:8765")

    assert record["total_requests"] == 2
    assert record["total_prompt_tokens"] == 100
    assert record["models_loaded"] == 1
    assert record["default_model"] == "geer-local"
    assert "secret_key" not in record
    assert "do-not-record" not in workspace.runtime_metrics.read_text()
    assert workspace.runtime_metrics.stat().st_mode & 0o777 == 0o600
