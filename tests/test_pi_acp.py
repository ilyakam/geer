from __future__ import annotations

import io
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from geer.assets import AssetError, Workspace
from geer.pi_acp import (
    AcpBridge,
    AcpError,
    PiProcess,
    PiRpcTimeout,
    _mcp_extension,
    _prompt_text,
    _t3_skill_extension,
    inspect_skills,
    model_state,
    models_output,
    run_acp,
)

MODEL = {
    "provider": "geer",
    "id": "geer-local",
    "name": "Geer Ornith (6-bit MLX)",
    "contextWindow": 65_536,
}


# T3 Code 0.0.44's RuntimeInstructions.ts appends this after its runtime_info.
T3_RUNTIME_SUFFIX = (
    ". No need to mention this otherwise. You can embed images and videos in your "
    "response using Markdown with absolute file paths.</runtime_info>\n\n"
    "<pull_request_linking>\n"
    "When the t3-code MCP server exposes link_pull_request, you must use it to register "
    "every pull request you create or work on for this thread. Call link_pull_request "
    "with the full PR URL immediately after creating a PR or starting work on an "
    "existing PR. For a stack, call it for every layer, not just the current branch "
    "or the top PR. This applies when creating or updating PRs through gh, gh stack, "
    "another CLI, or the host API: those operations do not register the PRs with this "
    "thread. Linking an already-linked PR is safe. Before finishing PR work, call "
    "list_thread_pull_requests and link any PR from your work that is missing. Do not "
    "link unrelated PRs mentioned only as background. If a linking call fails, report "
    "that failure instead of claiming the PR is linked.\n"
    "</pull_request_linking>"
)


def t3_runtime_info(*, harness="Grok", details=", as geer-local with off reasoning effort"):
    return (
        "<runtime_info>In case you're asked: you are running in T3 Code through the "
        f"{harness} harness{details}{T3_RUNTIME_SUFFIX}"
    )


def test_provider_updater_explains_pinned_harness_without_side_effects(tmp_path: Path) -> None:
    with pytest.raises(AssetError, match="Geer manages its pinned Pi harness.*geer setup"):
        run_acp(Workspace(tmp_path), ["update"])
    assert list(tmp_path.iterdir()) == []


class FakePi:
    """A Pi-side protocol fixture; no inference or real repository changes."""

    def __init__(self, argv, environment, cwd, on_event):
        self.argv, self.environment, self.cwd = argv, environment, cwd
        self.on_event = on_event
        self.calls = []
        self.prompt_received = threading.Event()
        self.closed = False
        self.history = Path(argv[argv.index("--session-dir") + 1])
        self.path = self.history / "pi-session.jsonl"
        if "--session" in argv:
            self.path = Path(argv[argv.index("--session") + 1])
        if self.path.is_file() and self.path.stat().st_size == 0:
            self.path.write_text('{"type":"session"}\n')
        entries = self.path.read_text().splitlines() if self.path.is_file() else []
        self.messages = [
            entry["message"] for entry in map(json.loads, entries) if entry.get("type") == "message"
        ]

    def append_message(self, message):
        self.messages.append(message)
        if not self.path.exists():
            self.path.write_text('{"type":"session"}\n')
        with self.path.open("a") as stream:
            stream.write(json.dumps({"type": "message", "message": message}) + "\n")

    def emit(self, event):
        if event.get("type") == "message_end":
            self.append_message(event["message"])
        self.on_event(event)

    def request(self, command, *, timeout=None, dispatched=None, **fields):
        self.calls.append((command, fields))
        if dispatched is not None:
            dispatched.set()
        if command == "get_state":
            return {
                "model": MODEL,
                "sessionFile": str(self.path),
                "sessionId": "native-pi-id",
                "isStreaming": False,
                "isCompacting": False,
            }
        if command == "get_available_models":
            return {"models": [MODEL, {"provider": "unrelated", "id": "remote"}]}
        if command == "get_commands":
            return {"commands": [{"name": "skill:example", "description": "Example skill"}]}
        if command == "get_messages":
            return {"messages": self.messages}
        if command == "get_session_stats":
            return {
                "tokens": {"input": 25, "output": 9},
                "contextUsage": {"tokens": 34, "contextWindow": 65_536},
            }
        if command == "prompt":
            self.append_message({"role": "user", "content": fields["message"]})
            self.prompt_received.set()
            return {"disposition": "started"}
        if command == "abort":
            self.emit(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "stopReason": "aborted",
                    },
                }
            )
            self.emit({"type": "agent_settled"})
        return {}

    def send(self, record):
        self.calls.append(("send", record))

    def close(self):
        self.closed = True


@pytest.fixture
def rig(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "geer")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    manifest = {
        "model_id": "geer-local",
        "context_length": 262_144,
        "build": {"display_name": MODEL["name"]},
    }
    monkeypatch.setattr("geer.pi_acp.verify_assets", lambda _: manifest)
    monkeypatch.setattr(
        "geer.pi_acp.select_hardware_profile",
        lambda **_: SimpleNamespace(max_context_window=65_536),
    )
    preparations = []

    def prepare(workspace, arguments, *, ensure_server, enable_tools):
        preparations.append({"args": arguments, "tools": enable_tools, "start": ensure_server})
        return ["pi-fixture", *arguments], {"GEER_TEST": "1"}

    monkeypatch.setattr("geer.runtime.prepare_pi", prepare)
    processes = []

    def factory(*args):
        process = FakePi(*args)
        processes.append(process)
        return process

    bridges = []

    def make_bridge(full_access=True, name="t3-code"):
        output = io.BytesIO()
        bridge = AcpBridge(
            workspace, full_access=full_access, output=output, process_factory=factory
        )
        bridge.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientInfo": {"name": name},
                },
            }
        )
        bridges.append(bridge)
        return bridge

    yield SimpleNamespace(
        workspace=workspace,
        cwd=cwd,
        processes=processes,
        make_bridge=make_bridge,
        preparations=preparations,
    )
    for bridge in bridges:
        bridge.close()


def records(bridge):
    return [json.loads(line) for line in bridge.output.getvalue().split(b"\n") if line]


def result(bridge, identifier):
    return next((item for item in records(bridge) if item.get("id") == identifier), None)


def wait_result(bridge, identifier):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        found = result(bridge, identifier)
        if found is not None:
            return found
        time.sleep(0.005)
    pytest.fail(f"ACP request {identifier} did not complete")


def call(bridge, identifier, method, **params):
    bridge.handle({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})


def open_session(rig, bridge, **params):
    call(bridge, 2, "session/new", cwd=str(rig.cwd), mcpServers=[], **params)
    reply = result(bridge, 2)
    assert "error" not in reply, reply
    return reply["result"]["sessionId"]


def test_probes_advertise_selected_model_without_starting_runtime(rig, capsys):
    bridge = rig.make_bridge()
    initialized = result(bridge, 1)["result"]
    assert initialized["agentInfo"]["name"] == "geer-pi"
    assert initialized["_meta"]["modelState"] == model_state(rig.workspace)
    assert (
        initialized["_meta"]["modelState"]["availableModels"][0]["_meta"]["contextWindow"] == 65_536
    )
    assert rig.preparations == []
    assert not rig.workspace.runtime.exists()
    assert "logged in" not in models_output(rig.workspace).lower()
    assert run_acp(rig.workspace, ["--version"]) == 0
    assert "0.99.1" in capsys.readouterr().out


def test_approval_required_chat_fails_before_any_pi_or_file_mutation(rig):
    bridge = rig.make_bridge(full_access=False)
    call(bridge, 2, "session/new", cwd=str(rig.cwd), mcpServers=[])
    assert "Full access" in result(bridge, 2)["error"]["message"]
    assert rig.preparations == []
    assert not rig.workspace.runtime.exists()


def test_title_generation_disables_every_tool_without_trusting_bare_chats(rig):
    bridge = rig.make_bridge(full_access=False, name="t3-code-git-text")
    session_id = open_session(rig, bridge)
    assert rig.preparations[0]["tools"] is False
    assert "--extension" not in rig.preparations[0]["args"]
    assert result(bridge, 2)["result"]["modes"]["currentModeId"] == "text-only"
    call(bridge, 3, "session/set_mode", sessionId=session_id, modeId="full-access")
    assert "error" in result(bridge, 3)


@pytest.mark.parametrize("client_name", ["t3-code", "t3-code-git-text", "other-client"])
def test_t3_skill_input_extension_is_private_and_only_loaded_for_interactive_t3(rig, client_name):
    bridge = rig.make_bridge(name=client_name)
    session_id = open_session(rig, bridge)
    extension = rig.workspace.runtime / "pi" / "acp" / session_id / "t3-skills.mjs"

    if client_name == "t3-code":
        assert extension.is_file()
        assert extension.stat().st_mode & 0o777 == 0o600
        arguments = rig.preparations[0]["args"]
        assert arguments[arguments.index("--extension") + 1] == str(extension)
    else:
        assert not extension.exists()
        assert "--extension" not in rig.preparations[0]["args"]


def test_t3_skill_read_error_fails_handled_turn_without_waiting_for_inference(rig, monkeypatch):
    bridge = rig.make_bridge()
    session_id = open_session(rig, bridge)
    process = rig.processes[0]
    original_request = process.request
    failure = "Geer skill selection failed: $manual at /original/SKILL.md: ENOENT"

    def request(command, **fields):
        if command == "prompt":
            if fields.get("dispatched") is not None:
                fields["dispatched"].set()
            process.emit({
                "type": "extension_ui_request", "method": "notify",
                "notifyType": "error", "message": failure,
            })
            return {"disposition": "handled"}
        return original_request(command, **fields)

    monkeypatch.setattr(process, "request", request)
    call(bridge, 3, "session/prompt", sessionId=session_id,
         prompt=[{"type": "text", "text": "$manual Task"}])

    assert wait_result(bridge, 3)["error"]["message"] == failure
    assert not any(command == "prompt" for command, _ in process.calls)


def test_cached_native_pi_expands_t3_manual_selections_from_original_sources(tmp_path: Path):
    binary = Path(__file__).resolve().parents[1] / "build/pi-runtime/pi/pi"
    if not binary.is_file():
        pytest.skip("cached pinned Pi runtime is unavailable")
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "settings.json").write_text(json.dumps({
        "enableInstallTelemetry": False, "enableAnalytics": False,
    }))
    (agent / "models.json").write_text(json.dumps({"providers": {"fixture": {
        "api": "openai-completions", "baseUrl": "http://127.0.0.1:1/v1",
        "apiKey": "unused-offline-fixture",
        "models": [{"id": "fixture", "name": "Fixture", "reasoning": False,
                    "input": ["text"], "contextWindow": 65_536, "maxTokens": 4096,
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}],
    }}}))
    sources = tmp_path / "original-skills"
    originals = {}
    bodies = {}
    for name in ("manual-one", "manual-two"):
        directory = sources / name
        (directory / "references").mkdir(parents=True)
        body = f"{name} instructions. Read references/original.txt."
        path = directory / "SKILL.md"
        path.write_text(f"---\nname: {name}\ndescription: {name}\n"
                        f"disable-model-invocation: true\n---\n{body}\n")
        support = directory / "references/original.txt"
        support.write_text(f"Original {name} supporting content")
        originals.update({path: path.read_bytes(), support: support.read_bytes()})
        bodies[name] = (
            f'<skill name="{name}" location="{path}">\n'
            f"References are relative to {directory}.\n\n{body}\n</skill>"
        )
    extension = _t3_skill_extension(tmp_path, enabled=True)
    capture = tmp_path / "captured.json"
    observer = tmp_path / "observer.mjs"
    observer.write_text(
        'import { writeFileSync } from "node:fs";\n'
        'export default function (pi) { pi.on("input", event => {\n'
        f"writeFileSync({json.dumps(str(capture))}, JSON.stringify(event));\n"
        'return { action: "handled" }; }); }\n'
    )
    arguments = [
        str(binary), "--mode", "rpc", "--offline", "--no-session", "--no-approve",
        "--no-context-files", "--no-tools", "--no-extensions", "--no-skills",
        "--no-prompt-templates", "--no-themes", "--provider", "fixture", "--model", "fixture",
        "--skill", str(sources), "--extension", str(extension), "--extension", str(observer),
    ]
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
        "PI_CODING_AGENT_DIR": str(agent),
        "PI_CODING_AGENT_SESSION_DIR": str(tmp_path / "sessions"),
        "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0",
    }
    events = []
    process = PiProcess(arguments, environment, tmp_path, events.append)
    try:
        commands = process.request("get_commands", timeout=5)["commands"]
        assert {command["name"] for command in commands if command["source"] == "skill"} == {
            "skill:manual-one", "skill:manual-two",
        }
        request = "Do the task; preserve $manual-one in prose.\n```\n$manual-two\n```"
        reply = process.request("prompt", timeout=5, message=f"$manual-one $manual-two {request}")
        assert reply["disposition"] == "handled"
        transformed = json.loads(capture.read_text())["text"]
        assert transformed == f"{bodies['manual-one']}\n\n{bodies['manual-two']}\n\n{request}"
        assert "disable-model-invocation" not in transformed
        for text in [
            "$UNKNOWN literal token", "Explain $manual-one in prose", "`$manual-one` literal",
            "```\n$manual-one\n```", "$manual-one-suffix Task", "/skill:manual-one Native path",
        ]:
            assert process.request("prompt", timeout=5, message=text)["disposition"] == "handled"
            assert json.loads(capture.read_text())["text"] == text
        remainder = "$UNKNOWN is a literal token, followed by $manual-two in prose."
        reply = process.request("prompt", timeout=5, message=f"$manual-one {remainder}")
        assert reply["disposition"] == "handled"
        assert json.loads(capture.read_text())["text"] == f"{bodies['manual-one']}\n\n{remainder}"
        for suffix in ["\n\n    $manual-two is literal code", "\n\n    print(...)"]:
            reply = process.request("prompt", timeout=5, message=f"$manual-one{suffix}")
            assert reply["disposition"] == "handled"
            assert json.loads(capture.read_text())["text"] == f"{bodies['manual-one']}\n\n{suffix}"
        assert all(path.read_bytes() == content for path, content in originals.items())
        previous_capture = capture.read_bytes()
        missing = sources / "manual-two/SKILL.md"
        missing.unlink()
        reply = process.request("prompt", timeout=5, message="$manual-two Task")
        assert reply["disposition"] == "handled"
        assert capture.read_bytes() == previous_capture
        error = next(event for event in events if event.get("notifyType") == "error")
        assert error["method"] == "notify"
        assert "Geer skill selection failed: $manual-two" in error["message"]
        assert str(missing) in error["message"]
    finally:
        process.close()


@pytest.mark.parametrize("text", ["/plan", "/plan inspect this repository"])
def test_plan_command_fails_without_executing_a_full_access_turn(rig, text):
    bridge = rig.make_bridge()
    session_id = open_session(rig, bridge)
    call(bridge, 3, "session/prompt", sessionId=session_id, prompt=[{"type": "text", "text": text}])
    assert "does not support Plan mode" in result(bridge, 3)["error"]["message"]
    assert not any(command == "prompt" for command, _ in rig.processes[0].calls)


@pytest.mark.parametrize(
    "details",
    [
        "",
        ", as geer-local",
        ", as model-with-Grok with off reasoning effort",
        " with low reasoning effort",
    ],
)
def test_corrects_t3_generated_runtime_harness_without_changing_user_content(rig, details):
    bridge = rig.make_bridge()
    session_id = open_session(rig, bridge)
    process = rig.processes[0]
    example = t3_runtime_info()
    user_text = f"Explain this runtime_info example without modifying it:\n{example}"
    prompt = [
        {"type": "text", "text": user_text},
        {"type": "resource", "resource": {"uri": "file:///example.md", "text": example}},
        {"type": "text", "text": t3_runtime_info(details=details)},
    ]
    original_prompt = json.dumps(prompt)

    call(bridge, 3, "session/prompt", sessionId=session_id, prompt=prompt)

    assert process.prompt_received.wait(1)
    message = next(fields["message"] for command, fields in process.calls if command == "prompt")
    assert message == (
        f"{user_text}\n\nfile:///example.md\n{example}\n\n"
        f"{t3_runtime_info(harness='Pi', details=details)}"
    )
    assert json.dumps(prompt) == original_prompt
    process.emit({"type": "agent_settled"})
    assert wait_result(bridge, 3)["result"]["stopReason"] == "end_turn"


@pytest.mark.parametrize(
    ("client_name", "texts"),
    [
        ("other-client", ["Task", t3_runtime_info()]),
        ("t3-code-git-text", ["Task", t3_runtime_info()]),
        ("t3-code", [t3_runtime_info()]),
        ("t3-code", [f"Quoted example:\n{t3_runtime_info()}"]),
        ("t3-code", [t3_runtime_info(), "Explain the preceding example"]),
        ("t3-code", ["Task", f"```text\n{t3_runtime_info()}\n```"]),
        ("t3-code", ["Task", t3_runtime_info().split("\n\n<pull_request_linking>")[0]]),
        ("t3-code", ["Task", t3_runtime_info(harness="Claude Code")]),
    ],
)
def test_preserves_runtime_info_outside_t3_generated_final_block(client_name, texts):
    blocks = [{"type": "text", "text": text} for text in texts]

    assert _prompt_text(blocks, client_name=client_name) == "\n\n".join(texts)


def test_streams_text_thinking_tools_and_waits_for_pi_settled(rig):
    bridge = rig.make_bridge()
    session_id = open_session(rig, bridge)
    process = rig.processes[0]
    call(
        bridge, 3, "session/prompt", sessionId=session_id, prompt=[{"type": "text", "text": "Task"}]
    )
    assert process.prompt_received.wait(1)
    process.emit({"type": "message_start", "message": {"role": "assistant"}})
    process.emit(
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "thinking_delta",
                "contentIndex": 0,
                "delta": "Consider it",
            },
        }
    )
    process.emit(
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "contentIndex": 1,
                "delta": "Hello ",
            },
        }
    )
    process.emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
                "content": [
                    {"type": "thinking", "thinking": "Consider it"},
                    {"type": "text", "text": "Hello world"},
                ],
            },
        }
    )
    process.emit(
        {
            "type": "tool_execution_start",
            "toolCallId": "read-1",
            "toolName": "read",
            "args": {"path": "README.md"},
        }
    )
    process.emit(
        {
            "type": "tool_execution_end",
            "toolCallId": "read-1",
            "toolName": "read",
            "result": {"content": [{"type": "text", "text": "File contents"}]},
            "isError": False,
        }
    )
    process.emit({"type": "agent_end", "messages": [], "willRetry": False})
    assert result(bridge, 3) is None
    process.emit({"type": "agent_settled"})
    reply = wait_result(bridge, 3)
    assert reply["result"]["stopReason"] == "end_turn"
    assert reply["result"]["_meta"]["geerPiUsage"]["tokens"]["input"] == 25
    updates = [
        item["params"]["update"]
        for item in records(bridge)
        if item.get("method") == "session/update"
    ]
    assert (
        "".join(
            item["content"]["text"]
            for item in updates
            if item["sessionUpdate"] == "agent_message_chunk"
        )
        == "Hello world"
    )
    tools = [item for item in updates if item["sessionUpdate"].startswith("tool_call")]
    assert tools[0]["locations"] == [{"path": str(rig.cwd / "README.md")}]
    assert tools[1]["status"] == "completed"
    assert tools[1]["content"][0]["content"]["text"] == "File contents"
    assert any(item["sessionUpdate"] == "usage_update" and item["used"] == 34 for item in updates)


def test_final_model_failure_is_an_error_and_retry_can_recover(rig):
    bridge = rig.make_bridge()
    session_id = open_session(rig, bridge)
    process = rig.processes[0]
    call(
        bridge, 3, "session/prompt", sessionId=session_id, prompt=[{"type": "text", "text": "Task"}]
    )
    assert process.prompt_received.wait(1)
    process.emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "errorMessage": "Model unavailable",
            },
        }
    )
    process.emit({"type": "agent_end", "willRetry": True, "messages": []})
    assert result(bridge, 3) is None
    process.emit({"type": "message_start", "message": {"role": "assistant"}})
    process.emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Recovered"}],
                "stopReason": "stop",
            },
        }
    )
    process.emit({"type": "agent_settled"})
    assert wait_result(bridge, 3)["result"]["stopReason"] == "end_turn"
    process.prompt_received.clear()
    call(
        bridge,
        4,
        "session/prompt",
        sessionId=session_id,
        prompt=[{"type": "text", "text": "Again"}],
    )
    assert process.prompt_received.wait(1)
    process.emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "errorMessage": "Final failure",
            },
        }
    )
    process.emit({"type": "agent_settled"})
    assert wait_result(bridge, 4)["error"]["message"] == "Final failure"


def test_cancel_stays_responsive_and_clears_queue_before_abort(rig):
    bridge = rig.make_bridge()
    session_id = open_session(rig, bridge)
    process = rig.processes[0]
    call(
        bridge, 3, "session/prompt", sessionId=session_id, prompt=[{"type": "text", "text": "Wait"}]
    )
    assert process.prompt_received.wait(1)
    bridge.handle(
        {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}}
    )
    assert wait_result(bridge, 3)["result"]["stopReason"] == "cancelled"
    commands = [command for command, _ in process.calls]
    assert commands.index("clear_queue") < commands.index("abort")


def test_cancel_during_prompt_preflight_waits_for_the_acknowledged_run_to_stop(rig, monkeypatch):
    bridge = rig.make_bridge()
    session_id = open_session(rig, bridge)
    process = rig.processes[0]
    original_request = process.request
    preflight = threading.Event()
    acknowledge = threading.Event()
    aborting_run = threading.Event()
    stopped = threading.Event()
    prompt_count = 0
    abort_count = 0

    def request(command, **fields):
        nonlocal prompt_count, abort_count
        if command == "prompt":
            prompt_count += 1
            if prompt_count == 1:
                process.calls.append((command, fields))
                fields["dispatched"].set()
                preflight.set()
                assert acknowledge.wait(3)
                process.append_message({"role": "user", "content": fields["message"]})
                return {"disposition": "started"}
        if command == "abort":
            abort_count += 1
            if abort_count == 1:
                # Native Pi is idle during preflight; this abort cannot stop the future run.
                process.calls.append((command, fields))
                return {}
            aborting_run.set()
            assert stopped.wait(3)
        return original_request(command, **fields)

    monkeypatch.setattr(process, "request", request)
    call(bridge, 3, "session/prompt", sessionId=session_id,
         prompt=[{"type": "text", "text": "Delayed first turn"}])
    assert preflight.wait(1)
    call(bridge, None, "session/cancel", sessionId=session_id)
    assert abort_count == 1
    assert result(bridge, 3) is None
    # Even a stale settlement cannot release this turn before the late abort completes.
    process.emit({"type": "agent_settled"})
    acknowledge.set()
    assert aborting_run.wait(1)
    call(bridge, 4, "session/prompt", sessionId=session_id,
         prompt=[{"type": "text", "text": "Too soon"}])
    assert "already running" in result(bridge, 4)["error"]["message"]
    assert result(bridge, 3) is None
    stopped.set()
    assert wait_result(bridge, 3)["result"]["stopReason"] == "cancelled"
    assert [command for command, _ in process.calls if command in {"clear_queue", "abort"}] == [
        "clear_queue", "abort", "clear_queue", "abort",
    ]
    call(bridge, 5, "session/prompt", sessionId=session_id,
         prompt=[{"type": "text", "text": "Clean next turn"}])
    assert process.prompt_received.wait(1)
    process.emit({"type": "agent_settled"})
    assert wait_result(bridge, 5)["result"]["stopReason"] == "end_turn"
    assert abort_count == 2


def test_cached_native_pi_cancel_stops_prompt_after_delayed_preflight(rig, tmp_path, monkeypatch):
    binary = Path(__file__).resolve().parents[1] / "build/pi-runtime/pi/pi"
    if not binary.is_file():
        pytest.skip("Pinned native Pi runtime is not staged")
    agent = tmp_path / "native-agent"
    agent.mkdir(mode=0o700)
    for name, value in [
        ("settings.json", {"enableInstallTelemetry": False, "enableAnalytics": False}),
        ("models.json", {"providers": {"geer": {
            "api": "openai-completions", "baseUrl": "http://127.0.0.1:1/v1",
            "apiKey": "synthetic-unused", "models": [{
                "id": MODEL["id"], "name": MODEL["name"], "reasoning": False,
                "input": ["text"], "contextWindow": MODEL["contextWindow"], "maxTokens": 4096,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            }],
        }}}),
    ]:
        path = agent / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    extension = tmp_path / "native-preflight.mjs"
    release = tmp_path / "release-preflight"
    extension.write_text(
        'import { existsSync } from "node:fs";\n'
        f"const release = {json.dumps(str(release))};\n"
        '''export default function (pi) {
  pi.on("input", event => event.text === "Clean next turn"
    ? { action: "handled" } : { action: "continue" });
  pi.on("before_agent_start", async (_event, ctx) => {
    ctx.ui.notify("FIXTURE_PREFLIGHT_WAIT", "info");
    while (!existsSync(release)) await new Promise(resolve => setTimeout(resolve, 10));
  });
  pi.on("agent_start", async (_event, ctx) => {
    ctx.ui.notify("FIXTURE_AGENT_STARTED", "info");
    await new Promise(resolve => setTimeout(resolve, 100));
    // Keep this regression before inference, including when the bridge is broken.
    ctx.abort();
  });
}
''')
    extension.chmod(0o600)
    preflight = threading.Event()
    milestones = []
    commands = []

    def prepare(workspace, arguments, *, ensure_server, enable_tools):
        assert ensure_server and enable_tools
        argv = [
            str(binary), "--provider", "geer", "--model", MODEL["id"],
            "--offline", "--no-approve", "--no-context-files", "--no-tools",
            "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes",
            "--extension", str(extension), *arguments,
        ]
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
            "PI_CODING_AGENT_DIR": str(agent),
            "PI_CODING_AGENT_SESSION_DIR": str(tmp_path / "native-sessions"),
            "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0",
        }
        return argv, environment

    def factory(argv, environment, cwd, on_event):
        def event(record):
            if record.get("message") == "FIXTURE_PREFLIGHT_WAIT":
                preflight.set()
            elif record.get("message") == "FIXTURE_AGENT_STARTED":
                milestones.append("agent started")
            on_event(record)

        process = PiProcess(argv, environment, cwd, event)
        original_request = process.request

        def request(command, **fields):
            commands.append(command)
            response = original_request(command, **fields)
            if command == "abort":
                milestones.append("abort returned")
            return response

        process.request = request
        return process

    monkeypatch.setattr("geer.runtime.prepare_pi", prepare)
    bridge = rig.make_bridge()
    bridge.process_factory = factory
    session_id = open_session(rig, bridge)
    call(bridge, 3, "session/prompt", sessionId=session_id,
         prompt=[{"type": "text", "text": "Delayed first turn"}])
    assert preflight.wait(3)
    state = bridge.sessions[session_id].rpc("get_state")
    assert not state["isStreaming"] and not state["isCompacting"]
    call(bridge, None, "session/cancel", sessionId=session_id)
    assert milestones == ["abort returned"]
    release.write_text("release")
    assert wait_result(bridge, 3)["result"]["stopReason"] == "cancelled"
    assert milestones == ["abort returned", "agent started", "abort returned"]
    assert [command for command in commands if command in {"clear_queue", "abort"}] == [
        "clear_queue", "abort", "clear_queue", "abort",
    ]
    call(bridge, 4, "session/prompt", sessionId=session_id,
         prompt=[{"type": "text", "text": "Clean next turn"}])
    assert wait_result(bridge, 4)["result"]["stopReason"] == "end_turn"
    assert commands.count("abort") == 2


@pytest.mark.parametrize("command", ["prompt", "compact", "clear_queue", "abort"])
def test_unresolved_state_change_timeout_closes_only_its_session_and_preserves_resume(
    rig, monkeypatch, command,
):
    bridge = rig.make_bridge()
    session_id = open_session(rig, bridge)
    session = bridge.sessions[session_id]
    process = rig.processes[0]
    process.append_message({"role": "user", "content": "Retained history"})
    call(bridge, 20, "session/new", cwd=str(rig.cwd), mcpServers=[])
    other_id = result(bridge, 20)["result"]["sessionId"]
    other_process = rig.processes[1]
    original_request = process.request

    def timeout(requested, **fields):
        if requested == command:
            process.calls.append((requested, fields))
            if fields.get("dispatched") is not None:
                fields["dispatched"].set()
            if requested in {"prompt", "compact"}:
                assert fields["timeout"] == 1800
            raise PiRpcTimeout(command)
        return original_request(requested, **fields)

    monkeypatch.setattr(process, "request", timeout)
    before = process.path.read_bytes()
    text = "/compact" if command == "compact" else "First turn"
    call(bridge, 3, "session/prompt", sessionId=session_id,
         prompt=[{"type": "text", "text": text}])
    if command in {"clear_queue", "abort"}:
        assert process.prompt_received.wait(1)
        before = process.path.read_bytes()
        call(bridge, 4, "session/cancel", sessionId=session_id)
        assert "saved history is preserved" in result(bridge, 4)["error"]["message"]
    failure = wait_result(bridge, 3)["error"]["message"]
    assert f"Pi {command} timed out" in failure
    assert "Reopen the chat to resume" in failure
    assert process.closed and session.lock_file.closed and session.closed
    assert process.path.read_bytes() == before
    assert session_id not in bridge.sessions
    assert other_id in bridge.sessions and not other_process.closed

    call(bridge, 5, "session/load", sessionId=session_id, cwd=str(rig.cwd), mcpServers=[])
    assert "error" not in result(bridge, 5)
    replacement = bridge.sessions[session_id]
    resumed = rig.processes[2]
    assert replacement is not session
    assert resumed.path == process.path and resumed.path.read_bytes() == before
    # A delayed old worker/event cannot retire or change a reopened instance.
    bridge._retire_session(session, PiRpcTimeout("prompt"))
    process.emit({"type": "geer_transport_error", "error": "Old child exited"})
    assert bridge.sessions[session_id] is replacement and not resumed.closed
    call(bridge, 6, "session/prompt", sessionId=session_id,
         prompt=[{"type": "text", "text": "Resumed turn"}])
    assert resumed.prompt_received.wait(1)
    resumed.emit({"type": "agent_settled"})
    assert wait_result(bridge, 6)["result"]["stopReason"] == "end_turn"


def test_compaction_cancellation_remains_responsive_and_can_run_the_next_turn(rig, monkeypatch):
    bridge = rig.make_bridge()
    session_id = open_session(rig, bridge)
    process = rig.processes[0]
    original_request = process.request
    compacting = threading.Event()
    aborted = threading.Event()

    def request(command, **fields):
        if command == "compact":
            process.calls.append((command, fields))
            fields["dispatched"].set()
            compacting.set()
            assert aborted.wait(3)
            raise AcpError("Pi compact failed: Compaction cancelled")
        if command == "abort":
            aborted.set()
        return original_request(command, **fields)

    monkeypatch.setattr(process, "request", request)
    call(bridge, 3, "session/prompt", sessionId=session_id,
         prompt=[{"type": "text", "text": "/compact"}])
    assert compacting.wait(1)
    call(bridge, 4, "session/cancel", sessionId=session_id)
    assert result(bridge, 4)["result"] == {}
    assert "Compaction cancelled" in wait_result(bridge, 3)["error"]["message"]
    assert not process.closed and session_id in bridge.sessions
    call(bridge, 5, "session/prompt", sessionId=session_id,
         prompt=[{"type": "text", "text": "Next turn"}])
    assert process.prompt_received.wait(1)
    process.emit({"type": "agent_settled"})
    assert wait_result(bridge, 5)["result"]["stopReason"] == "end_turn"


def test_native_rpc_timeout_identifies_the_unacknowledged_command(tmp_path):
    process = PiProcess(
        [sys.executable, "-c", "import sys\nfor line in sys.stdin: pass"],
        {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, tmp_path, lambda _: None,
    )
    try:
        with pytest.raises(PiRpcTimeout, match="Pi prompt timed out") as failure:
            process.request("prompt", timeout=0.05, message="Synthetic timeout")
        assert failure.value.command == "prompt"
    finally:
        process.close()


def test_cancelled_dispatch_deadline_retires_pending_work_before_releasing_history(
    rig, monkeypatch,
):
    bridge = rig.make_bridge()
    session_id = open_session(rig, bridge)
    session = bridge.sessions[session_id]
    process = rig.processes[0]
    before = process.path.read_bytes()
    original_request = process.request
    original_close = process.close
    dispatching = threading.Event()
    stopped = threading.Event()

    def request(command, **fields):
        if command == "prompt":
            dispatching.set()
            assert stopped.wait(3)
            raise AcpError("Child stopped before pipe dispatch finished")
        return original_request(command, **fields)

    def close():
        assert not session.lock_file.closed
        original_close()
        stopped.set()

    monkeypatch.setattr(process, "request", request)
    monkeypatch.setattr(process, "close", close)
    monkeypatch.setattr("geer.pi_acp.RPC_TIMEOUT", 0.01)
    call(bridge, 3, "session/prompt", sessionId=session_id,
         prompt=[{"type": "text", "text": "Blocked dispatch"}])
    assert dispatching.wait(1)
    call(bridge, 4, "session/cancel", sessionId=session_id)
    assert "Pi prompt dispatch timed out" in result(bridge, 4)["error"]["message"]
    assert "saved history is preserved" in wait_result(bridge, 3)["error"]["message"]
    assert process.closed and session.lock_file.closed
    assert session_id not in bridge.sessions
    assert process.path.read_bytes() == before


def test_real_rpc_close_unblocks_an_unread_pipe_without_waiting_for_stdin(tmp_path):
    ready = threading.Event()
    done = threading.Event()
    closed = threading.Event()
    failures = []
    closer = None

    def event(record):
        if record.get("type") == "fixture_ready":
            ready.set()

    process = PiProcess(
        [sys.executable, "-c",
         "import time\nprint('{\"type\":\"fixture_ready\"}', flush=True)\ntime.sleep(60)"],
        {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, tmp_path, event,
    )

    def prompt():
        try:
            process.request("prompt", message="x" * 2_000_000)
        except AcpError as error:
            failures.append(error)
        finally:
            done.set()

    def close():
        try:
            process.close()
        finally:
            closed.set()

    try:
        assert ready.wait(3)
        writer = threading.Thread(target=prompt, daemon=True)
        writer.start()
        deadline = time.monotonic() + 3
        while not process._write_lock.locked() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert process._write_lock.locked() and not done.is_set()
        closer = threading.Thread(target=close, daemon=True)
        closer.start()
        assert closed.wait(3), "Shutdown waited for the unread pipe instead of stopping its child"
        assert process.process.poll() is not None
        assert done.wait(1) and failures
        writer.join(timeout=1)
        assert not writer.is_alive()
    finally:
        if process.process.poll() is None:
            os.killpg(process.process.pid, signal.SIGKILL)
            process.process.wait(timeout=3)
        if closer is not None:
            closer.join(timeout=3)
        process.close()


def test_client_can_send_next_prompt_as_soon_as_completion_arrives(rig, monkeypatch):
    bridge = rig.make_bridge()
    session_id = open_session(rig, bridge)
    process = rig.processes[0]
    original_reply = bridge.reply

    def reply_and_continue(identifier, response):
        original_reply(identifier, response)
        if identifier == 3:
            call(
                bridge,
                4,
                "session/prompt",
                sessionId=session_id,
                prompt=[{"type": "text", "text": "Next"}],
            )

    monkeypatch.setattr(bridge, "reply", reply_and_continue)
    call(
        bridge,
        3,
        "session/prompt",
        sessionId=session_id,
        prompt=[{"type": "text", "text": "First"}],
    )
    assert process.prompt_received.wait(1)
    process.prompt_received.clear()
    process.emit({"type": "agent_settled"})
    assert process.prompt_received.wait(1)
    process.emit({"type": "agent_settled"})
    assert wait_result(bridge, 4)["result"]["stopReason"] == "end_turn"


def test_disconnected_client_releases_an_active_native_session(rig):
    bridge = rig.make_bridge()
    session_id = open_session(rig, bridge)
    process = rig.processes[0]
    call(
        bridge, 3, "session/prompt", sessionId=session_id, prompt=[{"type": "text", "text": "Wait"}]
    )
    assert process.prompt_received.wait(1)
    session = bridge.sessions[session_id]
    bridge.close()
    assert session.prompt_lock.acquire(timeout=1)
    session.prompt_lock.release()
    assert process.closed
    assert session.lock_file.closed


def test_resume_uses_saved_pi_history_and_replays_it(rig):
    first = rig.make_bridge()
    session_id = open_session(rig, first)
    directory = rig.workspace.runtime / "pi" / "acp" / session_id
    process = rig.processes[0]
    history_file = process.path
    call(
        first,
        3,
        "session/prompt",
        sessionId=session_id,
        prompt=[{"type": "text", "text": "Earlier question"}],
    )
    assert process.prompt_received.wait(1)
    process.emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Earlier answer"},
                    {
                        "type": "toolCall",
                        "id": "old-tool",
                        "name": "read",
                        "arguments": {"path": "README.md"},
                    },
                ],
                "stopReason": "stop",
            },
        }
    )
    process.emit(
        {
            "type": "message_end",
            "message": {
                "role": "toolResult",
                "toolCallId": "old-tool",
                "toolName": "read",
                "content": [{"type": "text", "text": "Earlier file contents"}],
                "isError": False,
            },
        }
    )
    process.emit({"type": "agent_settled"})
    assert wait_result(first, 3)["result"]["stopReason"] == "end_turn"
    first.close()
    second = rig.make_bridge()
    call(second, 2, "session/load", sessionId=session_id, cwd=str(rig.cwd), mcpServers=[])
    assert "error" not in result(second, 2)
    argv = rig.processes[1].argv
    assert argv[argv.index("--session") + 1] == str(history_file)
    assert history_file.is_file()
    assert (directory / "session.json").stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    updates = [
        item["params"]["update"]
        for item in records(second)
        if item.get("method") == "session/update"
    ]
    assert any(item["sessionUpdate"] == "user_message_chunk" for item in updates)
    assert any(
        item["sessionUpdate"] == "agent_message_chunk"
        and item["content"]["text"] == "Earlier answer"
        for item in updates
    )
    assert any(item["sessionUpdate"] == "tool_call_update" for item in updates)


def test_cancel_before_first_prompt_can_resume_an_empty_native_session(rig):
    first = rig.make_bridge()
    session_id = open_session(rig, first)
    process = rig.processes[0]
    history_file = process.path
    assert "--session" in process.argv
    assert history_file.is_file()
    assert history_file.stat().st_mode & 0o777 == 0o600
    assert process.messages == []
    first.handle(
        {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": session_id},
        }
    )
    assert not process.prompt_received.is_set()
    first.close()
    second = rig.make_bridge()
    call(second, 2, "session/load", sessionId=session_id, cwd=str(rig.cwd), mcpServers=[])
    assert "error" not in result(second, 2)
    assert rig.processes[1].path == history_file
    assert rig.processes[1].messages == []


def test_missing_saved_conversation_is_not_silently_recreated(rig):
    first = rig.make_bridge()
    session_id = open_session(rig, first)
    process = rig.processes[0]
    process.append_message({"role": "user", "content": "Retained conversation"})
    first.close()
    process.path.unlink()
    second = rig.make_bridge()
    call(second, 2, "session/load", sessionId=session_id, cwd=str(rig.cwd), mcpServers=[])
    assert "missing" in result(second, 2)["error"]["message"]
    assert len(rig.processes) == 1
    assert not process.path.exists()


def test_resume_rejects_concurrent_writer_then_mismatched_workspace(rig, tmp_path):
    first = rig.make_bridge()
    session_id = open_session(rig, first)
    second = rig.make_bridge()
    call(second, 2, "session/load", sessionId=session_id, cwd=str(rig.cwd), mcpServers=[])
    assert "active in another" in result(second, 2)["error"]["message"]
    first.close()
    other = tmp_path / "other"
    other.mkdir()
    call(second, 3, "session/load", sessionId=session_id, cwd=str(other), mcpServers=[])
    assert "does not match" in result(second, 3)["error"]["message"]
    assert len(rig.processes) == 1


def test_session_id_and_saved_file_cannot_escape_private_history(rig, tmp_path):
    bridge = rig.make_bridge()
    call(bridge, 2, "session/load", sessionId="../../elsewhere", cwd=str(rig.cwd), mcpServers=[])
    assert "Invalid Geer session ID" in result(bridge, 2)["error"]["message"]
    bridge2 = rig.make_bridge()
    session_id = open_session(rig, bridge2)
    bridge2.close()
    outside = tmp_path / "unrelated-session.jsonl"
    outside.write_text("preserve")
    path = rig.workspace.runtime / "pi" / "acp" / session_id / "session.json"
    saved = json.loads(path.read_text())
    saved["session_file"] = str(outside)
    path.write_text(json.dumps(saved))
    call(bridge, 3, "session/load", sessionId=session_id, cwd=str(rig.cwd), mcpServers=[])
    assert "outside Geer storage" in result(bridge, 3)["error"]["message"]
    assert outside.read_text() == "preserve"


def test_mcp_client_token_is_private_session_data_and_semble_is_preserved(rig):
    bridge = rig.make_bridge()
    client = {
        "type": "http",
        "name": "t3-code",
        "url": "http://127.0.0.1:1234/mcp",
        "headers": [{"name": "Authorization", "value": "Bearer test-private-token"}],
    }
    call(bridge, 2, "session/new", cwd=str(rig.cwd), mcpServers=[client])
    session_id = result(bridge, 2)["result"]["sessionId"]
    directory = rig.workspace.runtime / "pi" / "acp" / session_id
    extension = directory / "client-mcp.mjs"
    assert "pi.registerMcpServer" in extension.read_text()
    assert "test-private-token" in extension.read_text()
    assert extension.stat().st_mode & 0o777 == 0o600
    assert "test-private-token" not in bridge.output.getvalue().decode()
    assert "test-private-token" not in (directory / "session.json").read_text()
    assert "--extension" in rig.preparations[0]["args"]
    with pytest.raises(AcpError, match="reserved"):
        _mcp_extension(directory, [{**client, "name": "semble"}])
    assert _mcp_extension(directory, []) is None
    assert not extension.exists()


def test_images_and_uninstalled_model_fail_without_sending_to_pi(rig):
    bridge = rig.make_bridge()
    session_id = open_session(rig, bridge)
    call(
        bridge,
        3,
        "session/prompt",
        sessionId=session_id,
        prompt=[
            {"type": "image", "mimeType": "image/png", "data": "example"},
        ],
    )
    assert "accepts text" in result(bridge, 3)["error"]["message"]
    call(bridge, 4, "session/set_model", sessionId=session_id, modelId="unrelated-cloud-model")
    assert "Only the installed" in result(bridge, 4)["error"]["message"]
    assert not any(command in {"prompt", "set_model"} for command, _ in rig.processes[0].calls)


@pytest.mark.parametrize("metadata", [1, "invalid", ["invalid"], None])
def test_invalid_model_metadata_does_not_close_or_mutate_the_session(rig, metadata):
    bridge = rig.make_bridge()
    session_id = open_session(rig, bridge)
    process = rig.processes[0]
    call(
        bridge,
        3,
        "session/set_model",
        sessionId=session_id,
        modelId="geer-local",
        _meta=metadata,
    )
    assert result(bridge, 3)["error"]["code"] == -32602
    assert "must be an object" in result(bridge, 3)["error"]["message"]
    assert not any(command == "set_model" for command, _ in process.calls)
    call(
        bridge,
        4,
        "session/set_model",
        sessionId=session_id,
        modelId="geer-local",
        _meta={"reasoningEffort": "off"},
    )
    assert result(bridge, 4)["result"] == {}
    call(
        bridge,
        5,
        "session/prompt",
        sessionId=session_id,
        prompt=[{"type": "text", "text": "Continue"}],
    )
    assert process.prompt_received.wait(1)
    process.emit({"type": "agent_settled"})
    assert wait_result(bridge, 5)["result"]["stopReason"] == "end_turn"
    assert not process.closed


def test_skills_probe_uses_native_inventory_before_pi_config_exists(rig, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    skill = tmp_path / ".agents/skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("An existing skill whose metadata Pi parses.")
    monkeypatch.setattr("geer.runtime.compatible_pi", lambda value, **kwargs: "pinned-pi")
    monkeypatch.setattr(
        "geer.runtime.start_server", lambda *args, **kwargs: pytest.fail("inference started")
    )

    def native_inventory(command, paths, environment, *, timeout):
        assert command == "pinned-pi"
        assert paths == [skill.parents[1]]
        assert environment["PI_OFFLINE"] == "1"
        assert 0 < timeout <= 3
        return [{
            "name": "skill:example",
            "description": "A useful example skill.",
            "source": "skill",
            "sourceInfo": {"path": str(skill), "scope": "temporary", "source": "cli"},
        }]

    monkeypatch.setattr("geer.pi_acp.inspect_skill_commands", native_inventory)
    assert inspect_skills(rig.workspace) == {
        "skills": [
            {
                "name": "example",
                "description": "A useful example skill.",
                "userInvocable": True,
                "source": {"path": str(skill), "type": "user"},
            }
        ]
    }
    assert not rig.workspace.runtime.exists()


def test_real_rpc_transport_preserves_unicode_lines_and_reports_child_failure(tmp_path):
    script = """
import json, sys
for line in sys.stdin.buffer:
    command = json.loads(line)
    if command['type'] == 'crash':
        sys.exit(7)
    if command['type'] == 'malformed':
        print('not JSON', flush=True)
        continue
    print(json.dumps({'type':'message_update','assistantMessageEvent':{
        'type':'text_delta','delta':'one\\u2028two'}}, ensure_ascii=False), flush=True)
    print(json.dumps({'type':'response','id':command['id'],'command':command['type'],
        'success':True,'data':{'value':command['value']}}), flush=True)
"""
    events = []
    process = PiProcess(
        [sys.executable, "-u", "-c", script], os.environ.copy(), tmp_path, events.append
    )
    try:
        assert process.request("echo", value="first") == {"value": "first"}
        assert events[0]["assistantMessageEvent"]["delta"] == "one\u2028two"
        with pytest.raises(AcpError, match="exited"):
            process.request("crash", timeout=2)
    finally:
        process.close()
    malformed = PiProcess(
        [sys.executable, "-u", "-c", script], os.environ.copy(), tmp_path, events.append
    )
    try:
        with pytest.raises(AcpError, match="malformed RPC JSON"):
            malformed.request("malformed", timeout=2)
    finally:
        malformed.close()
