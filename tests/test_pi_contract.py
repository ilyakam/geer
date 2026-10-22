from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from geer.assets import Workspace
from geer.pi_acp import PiProcess, _mcp_extension
from geer.pi_distribution import PI_VERSION
from geer.runtime import pi_arguments, pi_environment


def test_cached_native_pi_dynamic_mcp_tools_survive_resume(tmp_path, monkeypatch):
    binary = Path(__file__).resolve().parents[1] / "build/pi-runtime/pi/pi"
    if not binary.is_file():
        pytest.skip("cached pinned Pi runtime is unavailable")

    home = tmp_path / "home"
    cwd = tmp_path / "repo"
    workspace = Workspace(cwd, home / "runtime")
    session = workspace.pi_config / "session"
    for directory in (home, cwd, workspace.pi_config, session):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(workspace.pi_config))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    manifest = {"model_id": "geer-contract", "build": {"display_name": "Synthetic model"}}
    schema = {
        "type": "object", "properties": {"text": {"type": "string"}},
        "required": ["text"], "additionalProperties": False,
    }
    tool_name = "mcp__contract__echo"
    completions = []
    mcp_requests = []

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, value=None, *, content_type="application/json"):
            body = b"" if value is None else (
                value if isinstance(value, bytes) else json.dumps(value).encode()
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # This stateless MCP fixture has no server-initiated event stream.
            self.respond(405)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/mcp":
                mcp_requests.append((request, self.headers.get("Authorization")))
                method = request["method"]
                if "id" not in request:
                    self.respond(202)
                    return
                if method == "initialize":
                    result = {
                        "protocolVersion": request["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "contract-fixture", "version": "1.0.0"},
                    }
                elif method == "tools/list":
                    result = {"tools": [{
                        "name": "echo", "description": "Echo synthetic text", "inputSchema": schema,
                    }]}
                elif method == "tools/call":
                    result = {"content": [{
                        "type": "text", "text": "echo:" + request["params"]["arguments"]["text"],
                    }], "isError": False}
                else:
                    self.respond(200, {"jsonrpc": "2.0", "id": request["id"], "error": {
                        "code": -32601, "message": "Unknown fixture method",
                    }})
                    return
                self.respond(200, {"jsonrpc": "2.0", "id": request["id"], "result": result})
                return
            if self.path != "/v1/chat/completions":
                self.respond(404)
                return
            completions.append(request)
            messages = request["messages"]
            user = next(item for item in reversed(messages) if item["role"] == "user")
            text = user["content"] if isinstance(user["content"], str) else "".join(
                item.get("text", "") for item in user["content"]
            )
            invoke_tool = messages[-1]["role"] != "tool"
            delta = {"role": "assistant", "content": "Synthetic MCP turn complete"}
            if invoke_tool:
                delta = {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": f"fixture-{len(completions)}", "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps({"text": text})},
                }]}
            chunk = {
                "id": "fixture", "object": "chat.completion.chunk", "created": 0,
                "model": manifest["model_id"], "choices": [{
                    "index": 0, "delta": delta,
                    "finish_reason": "tool_calls" if invoke_tool else "stop",
                }], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
            self.respond(200, body, content_type="text/event-stream")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    processes = []
    serving.start()
    try:
        settings = {"enableInstallTelemetry": False, "enableAnalytics": False}
        models = {"providers": {"geer": {
            "api": "openai-completions", "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
            "apiKey": "${GEER_API_KEY}", "models": [{
                "id": manifest["model_id"], "name": "Synthetic model", "reasoning": False,
                "input": ["text"], "contextWindow": 65_536, "maxTokens": 4096,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            }],
        }}}
        for name, value in (("settings.json", settings), ("models.json", models)):
            path = workspace.pi_config / name
            path.touch(mode=0o600)
            path.write_text(json.dumps(value))
        session_path = session / "conversation.jsonl"
        session_path.touch(mode=0o600)
        history = None
        saved_bytes = None
        for phase in ("before resume", "after resume"):
            environment = pi_environment(workspace, "synthetic-key", inherited={
                "HOME": str(home), "PATH": "/usr/bin:/bin",
                "GEER_FIXTURE_HEADER": phase,
            })
            if history is None:
                version = subprocess.run(
                    [str(binary), "--version"], env=environment, cwd=cwd,
                    capture_output=True, text=True, check=True, timeout=10,
                )
                assert version.stdout.strip() == PI_VERSION
            extension = _mcp_extension(session, [{
                "name": "contract", "type": "http",
                "url": f"http://127.0.0.1:{server.server_port}/mcp",
                "headers": [{"name": "Authorization", "value": "Bearer ${GEER_FIXTURE_HEADER}"}],
            }])
            assert extension is not None
            assert extension.stat().st_mode & 0o777 == 0o600
            arguments = pi_arguments(workspace, [
                "--mode", "rpc", "--no-context-files", "--session-dir", str(session),
                "--session", str(session_path), "--extension", str(extension),
            ], manifest)
            events = queue.Queue()
            process = PiProcess([str(binary), *arguments], environment, cwd, events.put)
            processes.append(process)
            state = process.request("get_state", timeout=10)
            assert state["model"]["id"] == manifest["model_id"]
            assert Path(state["sessionFile"]) == session_path
            if history is not None:
                assert process.request("get_messages")["messages"] == history
                assert session_path.read_bytes() == saved_bytes
            process.request("prompt", timeout=15, message=phase)
            turn_events = []
            deadline = time.monotonic() + 15
            while True:
                event = events.get(timeout=max(0.001, deadline - time.monotonic()))
                turn_events.append(event)
                assert event["type"] != "geer_transport_error", event
                if event["type"] == "agent_settled":
                    break
                assert time.monotonic() < deadline, turn_events
            tool_results = [event for event in turn_events if event["type"] == "tool_execution_end"]
            assert len(tool_results) == 1, turn_events
            assert tool_results[0]["toolName"] == tool_name
            assert tool_results[0]["isError"] is False
            assert tool_results[0]["result"]["content"] == [
                {"type": "text", "text": f"echo:{phase}"},
            ]
            history = process.request("get_messages")["messages"]
            assert history[-1]["stopReason"] == "stop"
            process.close()
            assert process.process.poll() is not None
            saved_bytes = session_path.read_bytes()

        assert len(completions) == 4
        for index, phase in enumerate(("before resume", "after resume")):
            initial, continuation = completions[index * 2:index * 2 + 2]
            tool = next(tool["function"] for tool in initial["tools"]
                        if tool["function"]["name"] == tool_name)
            assert tool["parameters"] == schema
            assert continuation["messages"][-1]["role"] == "tool"
            assert f"echo:{phase}" in json.dumps(continuation["messages"][-1]["content"])
            authenticated = [request for request, header in mcp_requests
                             if header == f"Bearer {phase}"]
            assert [request["method"] for request in authenticated] == [
                "initialize", "notifications/initialized", "tools/list", "tools/call",
            ]
            assert authenticated[-1]["params"]["name"] == "echo"
            assert authenticated[-1]["params"]["arguments"] == {"text": phase}
        assert "echo:before resume" in json.dumps(completions[2]["messages"])
        assert all(header in {"Bearer before resume", "Bearer after resume"}
                   for _, header in mcp_requests)
    finally:
        try:
            for process in processes:
                process.close()
        finally:
            server.shutdown()
            server.server_close()
            serving.join(timeout=3)
        assert all(process.process.poll() is not None for process in processes)
        assert not serving.is_alive()
