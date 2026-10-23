from __future__ import annotations

import json
import os
import select
import sys
import tomllib
from pathlib import Path

import pytest

from geer.assets import AssetError, Workspace
from geer.pi_native import launch_native_pi, prepare_native_pi


@pytest.fixture
def native_workspace(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "source", tmp_path / "home")
    workspace.root.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("geer.runtime.compatible_pi", lambda workspace: "/bin/pi")
    monkeypatch.setattr("geer.hardware.memory_bytes", lambda: 128 * 1024**3)
    monkeypatch.setattr("geer.runtime.verify_assets", lambda workspace: {
        "model_id": "local-test", "context_length": 65_536,
        "build": {"display_name": "Local test model"},
    })
    monkeypatch.setattr("geer.runtime.server_endpoint", lambda *a, **k: "http://127.0.0.1:1")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-cloud-key")
    monkeypatch.delenv("T3_PI_RUNTIME_MODE", raising=False)
    monkeypatch.delenv("T3_PI_MCP_EXTENSION_PATH", raising=False)
    return workspace


def test_native_discovery_has_real_local_models_and_skills_without_services(
    native_workspace, monkeypatch,
):
    workspace = native_workspace
    skill = workspace.runtime / "skills" / "test-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: test-skill\ndescription: Test skill\n---\nTest")
    monkeypatch.setattr("geer.runtime.start_server", lambda *a, **k: pytest.fail("inference"))
    monkeypatch.setenv("T3_MCP_BEARER_TOKEN", "stale-session-token")

    command, environment = prepare_native_pi(workspace, ["--mode", "rpc", "--no-session"])

    assert "--extension" not in command
    assert "--no-tools" in command
    assert command[command.index("--skill") + 1] == str(workspace.runtime / "skills")
    assert "OPENAI_API_KEY" not in environment
    assert "T3_MCP_BEARER_TOKEN" not in environment
    assert environment["PI_CODING_AGENT_DIR"] == str(workspace.pi_config)
    models = json.loads((workspace.pi_config / "models.json").read_text())
    assert models["providers"]["geer"]["models"][0]["id"] == "local-test"
    assert not (workspace.pi_config / "sessions").exists()


@pytest.mark.parametrize(
    "family",
    ("sage-a1.5", "ornith-1.5"),
)
@pytest.mark.parametrize(
    ("variant", "memory_gib", "context_window"),
    (("6bit", 128, 262_144), ("4-8bit", 32, 65_536)),
)
def test_family_discovery_preserves_image_inputs_and_configured_context_without_inference(
    native_workspace,
    monkeypatch,
    variant,
    memory_gib,
    context_window,
    family,
):
    path = Path(__file__).parents[1] / "model-recipes" / f"{family}-35b-{variant}.toml"
    recipe = tomllib.loads(path.read_text())
    manifest = {
        "model_id": "geer-local",
        "context_length": 262_144,
        "input_modalities": ["text", "image"],
        "build": {"display_name": recipe["display_name"], "recipe": recipe["id"]},
    }
    monkeypatch.setattr("geer.runtime.verify_assets", lambda workspace: manifest)
    monkeypatch.setattr("geer.hardware.memory_bytes", lambda: memory_gib * 1024**3)
    monkeypatch.setattr("geer.runtime.start_server", lambda *a, **k: pytest.fail("inference"))

    command, _ = prepare_native_pi(native_workspace, ["--mode", "rpc", "--no-session"])

    models = json.loads((native_workspace.pi_config / "models.json").read_text())
    model = models["providers"]["geer"]["models"][0]
    assert model["id"] == "geer-local"
    assert model["name"] == recipe["display_name"]
    assert model["input"] == ["text", "image"]
    assert model["contextWindow"] == context_window
    assert recipe["output"]["preserve_multimodal"] is True
    assert command[command.index("--model") + 1] == "geer-local"
    assert recipe["display_name"] in command[command.index("--append-system-prompt") + 1]


def test_native_background_generation_starts_local_server_without_tools_or_skills(
    native_workspace, monkeypatch,
):
    workspace = native_workspace
    started = []
    monkeypatch.setattr("geer.runtime.start_server", lambda *a, **k: (
        started.append(True) or {"endpoint": "http://127.0.0.1:1"}
    ))
    monkeypatch.setenv("T3_MCP_URL", "http://127.0.0.1:2/mcp")
    monkeypatch.setenv("T3_MCP_BEARER_TOKEN", "stale-session-token")

    command, environment = prepare_native_pi(workspace, [
        "--mode", "rpc", "--no-session", "--skill", "/synthetic/skills",
        "--no-extensions", "--no-tools",
    ])

    assert started == [True]
    assert "--no-tools" in command and "--no-skills" in command
    assert "--extension" not in command and "--skill" not in command
    assert "T3_MCP_URL" not in environment and "T3_MCP_BEARER_TOKEN" not in environment


@pytest.mark.parametrize("mode", [None, "approval-required", "auto-accept-edits", "auto"])
def test_native_interactive_requires_full_access_before_preparation(
    native_workspace, monkeypatch, mode,
):
    if mode is not None:
        monkeypatch.setenv("T3_PI_RUNTIME_MODE", mode)
    monkeypatch.setattr("geer.pi_native.prepare_pi", lambda *a, **k: pytest.fail("preparation"))
    with pytest.raises(AssetError, match="Full access"):
        prepare_native_pi(native_workspace, ["--mode", "rpc"])


def test_native_interactive_preserves_t3_extension_and_session_path(
    native_workspace, tmp_path, monkeypatch,
):
    extension = tmp_path / "pi-t3-mcp-extension.ts"
    extension.write_text("export default function () {}")
    monkeypatch.setenv("T3_PI_RUNTIME_MODE", "full-access")
    monkeypatch.setenv("T3_PI_MCP_EXTENSION_PATH", str(extension))
    monkeypatch.setenv("T3_MCP_BEARER_TOKEN", "current-session-token")
    monkeypatch.setattr("geer.runtime.start_server", lambda *a, **k: {
        "endpoint": "http://127.0.0.1:1",
    })
    history = tmp_path / "existing-session.jsonl"
    history.write_text('{"type":"session"}\n')
    arguments = ["--mode", "rpc", "--session", str(history), "--extension", str(extension)]

    command, environment = prepare_native_pi(native_workspace, arguments)

    assert command[-len(arguments):] == arguments
    assert command[command.index("--extension") + 1] == "builtin:mcp"
    assert environment["T3_MCP_BEARER_TOKEN"] == "current-session-token"
    assert "--no-tools" not in command
    assert history.read_text() == '{"type":"session"}\n'


def test_native_full_access_still_requires_t3_permission_extension(native_workspace, monkeypatch):
    monkeypatch.setenv("T3_PI_RUNTIME_MODE", "full-access")
    monkeypatch.setattr("geer.pi_native.prepare_pi", lambda *a, **k: pytest.fail("preparation"))
    with pytest.raises(AssetError, match="permission extension"):
        prepare_native_pi(native_workspace, ["--mode", "rpc"])


def test_native_exec_keeps_rpc_stream_and_process_identity(native_workspace, tmp_path, monkeypatch):
    """Exec the fixture in a child, then check raw RPC and signal ownership."""
    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "print(json.dumps({'pid': os.getpid()}), flush=True)\n"
        "for line in sys.stdin: print(line, end='', flush=True)\n"
    )
    fake_pi.chmod(0o700)
    monkeypatch.setattr("geer.pi_native.prepare_native_pi", lambda *a, **k: (
        [str(fake_pi)], dict(os.environ),
    ))
    # fork carries the isolated monkeypatch, while exec replaces that process.
    read_input, write_input = os.pipe()
    read_output, write_output = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.dup2(read_input, 0)
            os.dup2(write_output, 1)
            for descriptor in (read_input, write_input, read_output, write_output):
                os.close(descriptor)
            launch_native_pi(native_workspace, ["--mode", "rpc"])
        finally:
            os._exit(1)
    os.close(read_input)
    os.close(write_output)
    try:
        with os.fdopen(read_output) as output, os.fdopen(write_input, "w") as incoming:
            assert select.select([output], [], [], 5)[0], "Pi fixture did not start"
            assert json.loads(output.readline())["pid"] == pid
            for request in (
                {"id": "resume", "type": "switch_session", "sessionPath": "/synthetic/a.jsonl"},
                {"id": "cancel", "type": "abort"},
            ):
                incoming.write(json.dumps(request) + "\n")
                incoming.flush()
                assert select.select([output], [], [], 5)[0], "RPC request was buffered"
                assert json.loads(output.readline()) == request
    finally:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        _, status = os.waitpid(pid, 0)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == 15


def test_native_version_uses_pinned_binary_without_configuration(native_workspace, monkeypatch):
    monkeypatch.setattr("geer.runtime.verify_assets", lambda *a: pytest.fail("model probe"))
    command, _ = prepare_native_pi(native_workspace, ["--version"])
    assert command == ["/bin/pi", "--version"]
    assert not native_workspace.runtime.exists()
