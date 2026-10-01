"""Bounded ACP compatibility for T3's existing Grok transport and Pi RPC.

The transport name does not change the agent: Geer starts only the pinned Pi
runtime and the local model. Metadata probes never start inference. Interactive
sessions require explicit full access; the known T3 text-generation client has
all tools disabled instead. Pi's own JSONL files remain the session authority.
"""

from __future__ import annotations

import fcntl
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from .assets import AssetError, Workspace, model_alias, verify_assets
from .hardware import select_hardware_profile
from .skills import inspect_skill_commands, skill_paths

PROTOCOL_VERSION = 1
RPC_TIMEOUT = 30
PROMPT_TIMEOUT = 1800
FULL_ACCESS_MESSAGE = (
    "Geer Pi currently supports T3 Full access mode only. Approval-required, "
    "auto-accept edits, automatic approval, and plan modes are not supported. "
    "Select Full access before starting this chat."
)


class AcpError(Exception):
    def __init__(self, message: str, code: int = -32603) -> None:
        super().__init__(message)
        self.code = code


class PiRpcTimeout(AcpError):
    def __init__(self, command: str) -> None:
        super().__init__(f"Pi {command} timed out")
        self.command = command


def model_state(workspace: Workspace) -> dict[str, Any]:
    """Read the selected model without downloading weights or starting oMLX."""
    manifest = verify_assets(workspace)
    profile = select_hardware_profile(
        native_context_window=int(manifest.get("context_length", 262_144))
    )
    identifier = str(manifest["model_id"])
    return {
        "currentModelId": identifier,
        "availableModels": [
            {
                "modelId": identifier,
                "name": model_alias(manifest),
                "description": "Local model served by Geer through Pi",
                "_meta": {
                    "contextWindow": profile.max_context_window,
                    "supportsReasoningEffort": False,
                },
            }
        ],
    }


def inspect_skills(workspace: Workspace) -> dict[str, Any]:
    # Pi resolves metadata and invocation rules without inference or MCP startup.
    from .runtime import compatible_pi, pi_environment

    deadline = time.monotonic() + 3.0
    command = compatible_pi(workspace, timeout=1.0)
    commands = inspect_skill_commands(
        command, skill_paths(workspace), pi_environment(workspace),
        timeout=max(0.001, deadline - time.monotonic()),
    )
    return {
        "skills": [
            {
                "name": command["name"].removeprefix("skill:"),
                "description": command.get("description", ""),
                "userInvocable": True,
                "source": {"path": command["sourceInfo"]["path"], "type": "user"},
            }
            for command in commands
        ]
    }


def models_output(workspace: Workspace) -> str:
    model = model_state(workspace)["availableModels"][0]
    # T3 treats "You are logged in" as a Grok account and then reads unrelated
    # Grok credentials to probe billing. Keep this truthful local description.
    return f"Geer local model: {model['name']}\nModel ID: {model['modelId']}\n"


def _private_directory(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise AcpError(f"Refusing unexpected Geer session directory: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _write_private(path: Path, content: str) -> None:
    if path.is_symlink():
        raise AcpError(f"Refusing unexpected Geer session symlink: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _mcp_extension(directory: Path, servers: Any) -> Path | None:
    if not isinstance(servers, list):
        raise AcpError("mcpServers must be an array", -32602)
    definitions: dict[str, Any] = {}
    for server in servers:
        if not isinstance(server, dict):
            raise AcpError("Invalid MCP server", -32602)
        name = server.get("name")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", name)
            or name == "semble"
            or name in definitions
        ):
            raise AcpError("MCP server name is invalid, duplicated, or reserved", -32602)
        if server.get("type") != "http" or not isinstance(server.get("url"), str):
            raise AcpError("Geer's ACP adapter supports HTTP MCP servers only", -32602)
        if not server["url"].startswith(("http://", "https://")):
            raise AcpError("MCP server URL must use HTTP or HTTPS", -32602)
        headers = server.get("headers", [])
        if not isinstance(headers, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("value"), str)
            for item in headers
        ):
            raise AcpError("MCP headers must be name/value pairs", -32602)
        definitions[name] = {
            "url": server["url"],
            "headers": {item["name"]: item["value"] for item in headers},
            "exposure": "direct",
        }
    path = directory / "client-mcp.mjs"
    if not definitions:
        # A previous session's expiring T3 token must not survive a resume that
        # supplies no client server. No global Pi configuration is changed.
        if path.is_symlink():
            raise AcpError("Refusing an unexpected MCP extension symlink")
        path.unlink(missing_ok=True)
        return None
    _write_private(
        path,
        "export default function (pi) {\n"
        f"  const servers = {json.dumps(definitions)};\n"
        "  for (const [name, config] of Object.entries(servers)) {\n"
        "    pi.registerMcpServer(name, config);\n"
        "  }\n"
        "}\n",
    )
    return path


def _t3_skill_extension(directory: Path, *, enabled: bool) -> Path | None:
    path = directory / "t3-skills.mjs"
    if not enabled:
        if path.is_symlink():
            raise AcpError("Refusing an unexpected T3 skill extension symlink")
        path.unlink(missing_ok=True)
        return None
    _write_private(
        path,
        r"""import { readFileSync } from "node:fs";
import { dirname } from "node:path";
import { stripFrontmatter } from "@earendil-works/pi-coding-agent";

export default function (pi) {
  pi.on("input", (event, ctx) => {
    if (event.source !== "rpc" || !event.text.startsWith("$")) {
      return { action: "continue" };
    }
    const skills = new Map(pi.getCommands()
      .filter(command => command.source === "skill" && command.name.startsWith("skill:")
        && typeof command.sourceInfo?.path === "string")
      .map(command => [command.name.slice(6), command]));
    const selected = [];
    let remaining = event.text;
    while (true) {
      const token = /^\$([^\s$]+)(?=\s|$)/.exec(remaining);
      const command = token && skills.get(token[1]);
      if (!command) break;
      selected.push(command);
      remaining = remaining.slice(token[0].length).replace(/^[ \t]+/, "");
    }
    if (!selected.length) return { action: "continue" };
    const blocks = [];
    for (const command of selected) {
      const name = command.name.slice(6);
      const path = command.sourceInfo.path;
      try {
        const body = stripFrontmatter(readFileSync(path, "utf8")).trim();
        blocks.push(`<skill name="${name}" location="${path}">\nReferences are relative to `
          + `${dirname(path)}.\n\n${body}\n</skill>`);
      } catch (error) {
        ctx.ui.notify(
          `Geer skill selection failed: $${name} at ${path}: ${error.message}`, "error");
        return { action: "handled" };
      }
    }
    if (remaining) blocks.push(remaining);
    return { action: "transform", text: blocks.join("\n\n"), images: event.images };
  });
}
""",
    )
    return path


class PiProcess:
    """Pi RPC request correlation with a separate reader for streamed events."""

    def __init__(
        self,
        argv: list[str],
        environment: dict[str, str],
        cwd: Path,
        on_event: Callable[[dict[str, Any]], None],
    ) -> None:
        self._on_event = on_event
        self._pending: dict[str, queue.Queue[Any]] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False
        self._failure: str | None = None
        self.process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            umask=0o077,
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self._stderr = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr.start()

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        # Preserve ordinary Pi diagnostics without writing a second log or
        # contaminating protocol stdout.
        while chunk := self.process.stderr.read1(4096):
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()

    def _fail(self, message: str) -> None:
        with self._lock:
            self._failure = message
            pending = list(self._pending.values())
        for response in pending:
            response.put(AcpError(message))
        if not self._closed:
            self._on_event({"type": "geer_transport_error", "error": message})

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeError):
                    self._fail("Pi emitted malformed RPC JSON")
                    return
                if not isinstance(record, dict):
                    self._fail("Pi emitted an invalid RPC record")
                    return
                if record.get("type") == "response":
                    with self._lock:
                        waiting = self._pending.get(record.get("id"))
                    if waiting is not None:
                        waiting.put(record)
                else:
                    self._on_event(record)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            self._fail(f"Pi RPC read failed: {error}")
            return
        self._fail("Pi RPC process exited before the session closed")

    def send(self, record: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        try:
            with self._write_lock:
                self.process.stdin.write(json.dumps(record).encode() + b"\n")
                self.process.stdin.flush()
        except (OSError, ValueError) as error:
            raise AcpError(f"Cannot send a Pi RPC command: {error}") from error

    def request(
        self,
        command: str,
        *,
        timeout: float = RPC_TIMEOUT,
        dispatched: threading.Event | None = None,
        **fields: Any,
    ) -> Any:
        identifier = uuid.uuid4().hex
        waiting: queue.Queue[Any] = queue.Queue()
        with self._lock:
            if self._failure:
                raise AcpError(self._failure)
            self._pending[identifier] = waiting
        try:
            self.send({"id": identifier, "type": command, **fields})
            if dispatched is not None:
                dispatched.set()
            try:
                result = waiting.get(timeout=timeout)
            except queue.Empty as error:
                raise PiRpcTimeout(command) from error
            if isinstance(result, Exception):
                raise result
            if result.get("success") is not True:
                raise AcpError(f"Pi {command} failed: {result.get('error', 'unknown error')}")
            return result.get("data", {})
        finally:
            if dispatched is not None:
                dispatched.set()
            with self._lock:
                self._pending.pop(identifier, None)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        graceful = self._write_lock.acquire(blocking=False)
        if graceful:
            try:
                if self.process.stdin is not None:
                    self.process.stdin.close()
            except OSError:
                pass
            finally:
                self._write_lock.release()
        try:
            self.process.wait(timeout=3 if graceful else 0)
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.process.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    self.process.wait(timeout=2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        # A blocked writer owns buffered stdin's lock. Stop its child before
        # closing that stream, and never wait indefinitely for the writer.
        if not graceful and self._write_lock.acquire(timeout=1):
            try:
                if self.process.stdin is not None:
                    self.process.stdin.close()
            except OSError:
                pass
            finally:
                self._write_lock.release()
        self._reader.join(timeout=1)
        self._stderr.join(timeout=1)


@dataclass
class Session:
    identifier: str
    cwd: Path
    directory: Path
    lock_file: BinaryIO
    tools: bool
    process: PiProcess | None = None
    models: dict[str, Any] = field(default_factory=dict)
    settled: threading.Event = field(default_factory=threading.Event)
    dispatched: threading.Event = field(default_factory=threading.Event)
    prompt_lock: Any = field(default_factory=threading.Lock)
    lifecycle_lock: Any = field(default_factory=threading.RLock)
    closed: bool = False
    active: bool = False
    cancelled: bool = False
    error: str | None = None
    stop_reason: str = "end_turn"
    streamed: dict[tuple[str, int], str] = field(default_factory=dict)

    def rpc(self, command: str, **fields: Any) -> Any:
        if self.closed:
            raise AcpError(self.error or "Geer session closed")
        if self.process is None:
            raise AcpError("Pi session has not started")
        return self.process.request(command, **fields)

    def save(self) -> None:
        state = self.rpc("get_state")
        path = state.get("sessionFile")
        if not isinstance(path, str):
            raise AcpError("Pi did not provide persistent session storage")
        history = self.directory / "history"
        if not Path(path).resolve().is_relative_to(history.resolve()):
            raise AcpError("Pi session storage is outside its Geer session directory")
        if not Path(path).is_file():
            raise AcpError("Pi did not initialize persistent session storage")
        Path(path).chmod(0o600)
        _write_private(
            self.directory / "session.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "cwd": str(self.cwd),
                    "session_file": path,
                    "tools": self.tools,
                },
                indent=2,
            )
            + "\n",
        )

    def close(self, *, error: str = "Geer session closed") -> None:
        with self.lifecycle_lock:
            if self.closed:
                return
            self.closed = True
            self.error = error
            self.settled.set()
            self.dispatched.set()
            if self.process is not None:
                self.process.close()
            self.lock_file.close()


def _content(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    return [
        {"type": "content", "content": block}
        for block in result.get("content", [])
        if isinstance(block, dict) and block.get("type") in {"text", "image"}
    ]


def _correct_t3_runtime_info(text: str) -> str:
    match = re.fullmatch(
        r"(<runtime_info>In case you're asked: you are running in T3 Code through the )"
        r"Grok"
        r"( harness(?:, as [^<>\r\n]+)?(?: with [^<>\r\n]+ reasoning effort)?"
        r"\. No need to mention this otherwise\. You can embed images and videos "
        r"in your response using Markdown with absolute file paths\.</runtime_info>\n\n"
        r"<pull_request_linking>\nWhen the t3-code MCP server exposes link_pull_request,"
        r"[\s\S]*\n</pull_request_linking>)",
        text,
    )
    return f"{match[1]}Pi{match[2]}" if match else text


def _prompt_text(blocks: Any, *, client_name: str = "") -> str:
    if not isinstance(blocks, list) or not blocks:
        raise AcpError("A non-empty text prompt is required", -32602)
    texts: list[str] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise AcpError("Invalid prompt content", -32602)
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text = block["text"]
            # T3 appends its generated runtime metadata as a separate final block.
            if client_name == "t3-code" and index == len(blocks) - 1 and index > 0:
                text = _correct_t3_runtime_info(text)
            texts.append(text)
        elif block.get("type") == "resource" and isinstance(block.get("resource"), dict):
            resource = block["resource"]
            if not isinstance(resource.get("text"), str):
                raise AcpError("Only text resources are supported by Geer's local model", -32602)
            texts.append(f"{resource.get('uri', 'Attached resource')}\n{resource['text']}")
        elif block.get("type") == "resource_link" and isinstance(block.get("uri"), str):
            texts.append(f"Attached resource: {block.get('name', '')} {block['uri']}")
        else:
            raise AcpError(
                "Geer's current local model accepts text, not image or audio input", -32602
            )
    message = "\n\n".join(texts)
    if not message.strip():
        raise AcpError("A non-empty text prompt is required", -32602)
    return message


class AcpBridge:
    def __init__(
        self,
        workspace: Workspace,
        *,
        full_access: bool,
        output: BinaryIO,
        process_factory: Callable[..., PiProcess] = PiProcess,
    ) -> None:
        self.workspace = workspace
        self.full_access = full_access
        self.output = output
        self.process_factory = process_factory
        self.sessions: dict[str, Session] = {}
        self.client_name = ""
        self.initialized = False
        self._output_lock = threading.Lock()
        self._closing = False

    def emit(self, value: dict[str, Any]) -> None:
        if self._closing:
            return
        with self._output_lock:
            self.output.write(json.dumps({"jsonrpc": "2.0", **value}).encode() + b"\n")
            self.output.flush()

    def reply(self, identifier: Any, result: Any) -> None:
        self.emit({"id": identifier, "result": result})

    def error(self, identifier: Any, error: Exception) -> None:
        self.emit(
            {
                "id": identifier,
                "error": {
                    "code": error.code if isinstance(error, AcpError) else -32603,
                    "message": str(error),
                },
            }
        )

    def update(self, session: Session, update: dict[str, Any]) -> None:
        self.emit(
            {
                "method": "session/update",
                "params": {
                    "sessionId": session.identifier,
                    "update": update,
                },
            }
        )

    def _chunk(self, session: Session, kind: str, text: str) -> None:
        if text:
            self.update(session, {"sessionUpdate": kind, "content": {"type": "text", "text": text}})

    def _event(self, session: Session, event: dict[str, Any]) -> None:
        if session.closed:
            return
        kind = event.get("type")
        if kind == "geer_transport_error":
            session.error = str(event.get("error", "Pi process failed"))
            session.settled.set()
            return
        if kind == "extension_ui_request":
            message = event.get("message")
            if (
                session.active
                and event.get("method") == "notify"
                and event.get("notifyType") == "error"
                and isinstance(message, str)
                and message.startswith("Geer skill selection failed: ")
            ):
                session.error = message
                session.settled.set()
                return
            # Geer does not load arbitrary user extensions. Never silently
            # approve an unexpected extension interaction in a headless client.
            if session.process is not None and event.get("id") is not None:
                session.process.send(
                    {
                        "type": "extension_ui_response",
                        "id": event["id"],
                        "cancelled": True,
                    }
                )
            return
        if not session.active:
            return
        if kind == "message_start" and event.get("message", {}).get("role") == "assistant":
            session.streamed.clear()
            session.error = None
        elif kind == "message_update":
            update = event.get("assistantMessageEvent", {})
            subtype = update.get("type", "")
            category = "thinking" if subtype.startswith("thinking_") else "text"
            channel = "agent_thought_chunk" if category == "thinking" else "agent_message_chunk"
            key = (category, update.get("contentIndex", 0))
            if subtype in {"text_delta", "thinking_delta"}:
                delta = update.get("delta", "")
                if isinstance(delta, str):
                    session.streamed[key] = session.streamed.get(key, "") + delta
                    self._chunk(session, channel, delta)
            elif subtype in {"text_end", "thinking_end"}:
                self._complete_block(session, category, key[1], update.get("content", ""))
        elif kind == "message_end":
            message = event.get("message", {})
            if message.get("role") == "assistant":
                for index, block in enumerate(message.get("content", [])):
                    if block.get("type") in {"text", "thinking"}:
                        category = block["type"]
                        self._complete_block(session, category, index, block.get(category, ""))
                stop = message.get("stopReason")
                if stop == "error":
                    session.error = message.get("errorMessage") or "Pi model request failed"
                elif stop == "aborted":
                    session.cancelled = True
                elif stop == "length":
                    session.stop_reason = "max_tokens"
                else:
                    session.stop_reason = "end_turn"
        elif kind in {"tool_execution_start", "tool_execution_update", "tool_execution_end"}:
            self._tool_event(session, event)
        elif kind == "auto_retry_end" and event.get("success") is False:
            session.error = str(event.get("finalError", "Pi exhausted its retries"))
        elif kind == "agent_settled":
            session.settled.set()

    def _complete_block(self, session: Session, category: str, index: int, content: Any) -> None:
        if not isinstance(content, str):
            return
        key = (category, index)
        previous = session.streamed.get(key, "")
        if content.startswith(previous):
            channel = "agent_thought_chunk" if category == "thinking" else "agent_message_chunk"
            self._chunk(session, channel, content[len(previous) :])
        session.streamed[key] = content

    def _tool_event(self, session: Session, event: dict[str, Any]) -> None:
        identifier = event.get("toolCallId")
        if not isinstance(identifier, str):
            return
        name = str(event.get("toolName", "tool"))
        kind = event["type"]
        if kind == "tool_execution_start":
            arguments = event.get("args", {})
            category = {
                "read": "read",
                "write": "edit",
                "edit": "edit",
                "bash": "execute",
                "grep": "search",
                "find": "search",
                "ls": "search",
            }.get(name, "other")
            title = name
            locations = []
            if isinstance(arguments, dict):
                path = arguments.get("path") or arguments.get("file_path")
                if isinstance(path, str):
                    absolute = Path(path) if Path(path).is_absolute() else session.cwd / path
                    locations = [{"path": str(absolute)}]
                    title = f"{name} {path}"
                elif isinstance(arguments.get("command"), str):
                    title = (arguments["command"].splitlines() or [name])[0][:200]
            self.update(
                session,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": identifier,
                    "title": title,
                    "kind": category,
                    "status": "in_progress",
                    "rawInput": arguments,
                    "locations": locations,
                },
            )
        else:
            result = event.get("result" if kind == "tool_execution_end" else "partialResult", {})
            self.update(
                session,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": identifier,
                    "status": ("failed" if event.get("isError") else "completed")
                    if kind == "tool_execution_end"
                    else "in_progress",
                    "content": _content(result),
                    "rawOutput": result,
                },
            )

    def _open_session(self, params: dict[str, Any], *, load: bool) -> dict[str, Any]:
        tools = self.client_name != "t3-code-git-text"
        if tools and not self.full_access:
            raise AcpError(FULL_ACCESS_MESSAGE, -32602)
        if params.get("additionalDirectories"):
            raise AcpError("Additional working directories are not supported", -32602)
        raw_cwd = params.get("cwd")
        if not isinstance(raw_cwd, str) or not Path(raw_cwd).is_absolute():
            raise AcpError("cwd must be an absolute directory", -32602)
        cwd = Path(raw_cwd).resolve()
        if not cwd.is_dir():
            raise AcpError("The selected workspace directory does not exist", -32602)
        identifier = params.get("sessionId") if load else str(uuid.uuid4())
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", identifier
        ):
            raise AcpError("Invalid Geer session ID", -32602)
        if identifier in self.sessions:
            raise AcpError("This Geer session is already open", -32602)
        _private_directory(self.workspace.runtime / "pi")
        parent = self.workspace.runtime / "pi" / "acp"
        _private_directory(parent)
        directory = parent / identifier
        if load and not directory.is_dir():
            raise AcpError("The saved Geer session could not be found", -32602)
        _private_directory(directory)
        lock_path = directory / "lock"
        if lock_path.is_symlink():
            raise AcpError("Refusing unexpected Geer session lock symlink")
        lock_file = lock_path.open("a+b")
        lock_path.chmod(0o600)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock_file.close()
            raise AcpError("This Geer session is active in another process") from error
        session = Session(identifier, cwd, directory, lock_file, tools)
        try:
            history = directory / "history"
            _private_directory(history)
            arguments = ["--mode", "rpc", "--session-dir", str(history)]
            if load:
                saved = json.loads((directory / "session.json").read_text())
                if saved.get("cwd") != str(cwd) or saved.get("tools") != tools:
                    raise AcpError("Saved session workspace or tool mode does not match", -32602)
                session_file = Path(saved["session_file"])
                if not session_file.is_file() or not session_file.resolve().is_relative_to(
                    history.resolve()
                ):
                    raise AcpError("The saved Pi conversation is missing or outside Geer storage")
            else:
                # Pi leaves new sessions in memory until the first message.
                # An explicit empty file is initialized by Pi itself, allowing
                # a chat cancelled before that message to reopen after restart.
                session_file = history / "conversation.jsonl"
                descriptor = os.open(session_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
            arguments.extend(["--session", str(session_file)])
            extension = _mcp_extension(directory, params.get("mcpServers", []) if tools else [])
            if extension is not None:
                arguments.extend(["--extension", str(extension)])
            skill_extension = _t3_skill_extension(
                directory, enabled=tools and self.client_name == "t3-code"
            )
            if skill_extension is not None:
                arguments.extend(["--extension", str(skill_extension)])
            from .runtime import prepare_pi

            argv, environment = prepare_pi(
                self.workspace, arguments, ensure_server=True, enable_tools=tools
            )
            session.process = self.process_factory(
                argv, environment, cwd, lambda event: self._event(session, event)
            )
            state = session.rpc("get_state")
            available = session.rpc("get_available_models").get("models", [])
            current = state.get("model", {})
            models = [model for model in available if model.get("provider") == "geer"]
            if not models or current.get("provider") != "geer":
                raise AcpError("Pi did not select the local Geer model")
            session.models = {
                "currentModelId": current["id"],
                "availableModels": [
                    {
                        "modelId": model["id"],
                        "name": model.get("name", model["id"]),
                        "_meta": {
                            "contextWindow": model.get("contextWindow"),
                            "supportsReasoningEffort": False,
                        },
                    }
                    for model in models
                ],
            }
            session.save()
            self.sessions[identifier] = session
            if load:
                self._replay(session)
            commands = session.rpc("get_commands").get("commands", [])
            self.update(
                session,
                {
                    "sessionUpdate": "available_commands_update",
                    "availableCommands": [
                        {
                            "name": command["name"],
                            "description": command.get("description") or "Pi command",
                        }
                        for command in commands
                        if isinstance(command.get("name"), str)
                    ]
                    + (
                        [{"name": "compact", "description": "Summarize this conversation"}]
                        if tools
                        else []
                    ),
                },
            )
            result = {"models": session.models, "modes": self._modes(tools)}
            return result if load else {"sessionId": identifier, **result}
        except Exception:
            self.sessions.pop(identifier, None)
            session.close()
            raise

    @staticmethod
    def _modes(tools: bool) -> dict[str, Any]:
        identifier = "full-access" if tools else "text-only"
        return {
            "currentModeId": identifier,
            "availableModes": [
                {
                    "id": identifier,
                    "name": "Full access" if tools else "Text only",
                    "description": "Pi tools use the current account's permissions"
                    if tools
                    else "No tools",
                }
            ],
        }

    def _replay(self, session: Session) -> None:
        for message in session.rpc("get_messages").get("messages", []):
            role = message.get("role")
            content = message.get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for block in content:
                if role == "user" and block.get("type") == "text":
                    self._chunk(session, "user_message_chunk", block.get("text", ""))
                elif role == "assistant" and block.get("type") in {"text", "thinking"}:
                    kind = block["type"]
                    channel = "agent_message_chunk" if kind == "text" else "agent_thought_chunk"
                    self._chunk(session, channel, block.get(kind, ""))
                elif role == "assistant" and block.get("type") == "toolCall":
                    self._tool_event(
                        session,
                        {
                            "type": "tool_execution_start",
                            "toolCallId": block.get("id"),
                            "toolName": block.get("name"),
                            "args": block.get("arguments", {}),
                        },
                    )
            if role == "toolResult":
                self._tool_event(
                    session,
                    {
                        "type": "tool_execution_end",
                        "toolCallId": message.get("toolCallId"),
                        "toolName": message.get("toolName"),
                        "result": {"content": content},
                        "isError": message.get("isError", False),
                    },
                )

    def _prompt(self, identifier: Any, session: Session, text: str) -> None:
        result = None
        failure = None
        try:
            if session.cancelled:
                session.dispatched.set()
            elif text.strip() == "/compact":
                session.rpc("compact", timeout=PROMPT_TIMEOUT, dispatched=session.dispatched)
                self._chunk(session, "agent_message_chunk", "Context compacted.")
            else:
                accepted = session.rpc(
                    "prompt", timeout=PROMPT_TIMEOUT, message=text, dispatched=session.dispatched
                )
                # Pi can acknowledge a prompt after an asynchronous preflight.
                # An earlier abort while Pi was idle does not cancel that prompt.
                if session.cancelled:
                    session.rpc("clear_queue")
                    session.rpc("abort", timeout=15)
                handled = accepted.get("disposition") == "handled"
                state = session.rpc("get_state") if handled else {}
                idle = handled and not state.get("isStreaming") and not state.get("isCompacting")
                if not idle and not session.settled.wait(PROMPT_TIMEOUT):
                    session.rpc("clear_queue")
                    session.rpc("abort", timeout=15)
                    raise AcpError("Pi did not settle before the turn deadline")
            if session.error and not session.cancelled:
                raise AcpError(session.error)
            session.save()
            stats = session.rpc("get_session_stats")
            usage = stats.get("contextUsage") or {}
            if isinstance(usage.get("tokens"), int) and isinstance(usage.get("contextWindow"), int):
                self.update(
                    session,
                    {
                        "sessionUpdate": "usage_update",
                        "used": usage["tokens"],
                        "size": usage["contextWindow"],
                    },
                )
            result = {
                "stopReason": "cancelled" if session.cancelled else session.stop_reason,
                "_meta": {"geerPiUsage": stats},
            }
        except PiRpcTimeout as error:
            failure = self._retire_session(session, error) if error.command in {
                "prompt", "compact", "clear_queue", "abort",
            } else error
        except (AcpError, AssetError, OSError, ValueError, KeyError, TypeError) as error:
            failure = AcpError(session.error) if session.closed and session.error else error
        finally:
            session.active = False
            session.dispatched.set()
            session.prompt_lock.release()
        # The client may immediately dispatch another turn after this reply.
        # Release the previous turn before publishing its completion.
        if failure is not None:
            self.error(identifier, failure)
        else:
            self.reply(identifier, result)

    def _retire_session(self, session: Session, error: PiRpcTimeout) -> AcpError:
        message = (
            f"{error}. Geer closed this Pi session; saved history is preserved. "
            "Reopen the chat to resume."
        )
        with session.lifecycle_lock:
            session.close(error=message)
            if self.sessions.get(session.identifier) is session:
                self.sessions.pop(session.identifier)
            return AcpError(session.error or message)

    def handle(self, request: Any) -> None:
        identifier = request.get("id") if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise AcpError("Invalid JSON-RPC request", -32600)
            method, params = request.get("method"), request.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise AcpError("Invalid JSON-RPC method or parameters", -32600)
            if method == "initialize":
                from .pi_distribution import PI_VERSION

                client = params.get("clientInfo", {})
                self.client_name = client.get("name", "") if isinstance(client, dict) else ""
                self.initialized = True
                self.reply(
                    identifier,
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "agentInfo": {
                            "name": "geer-pi",
                            "title": "Geer (Pi)",
                            "version": PI_VERSION,
                        },
                        "authMethods": [
                            {
                                "id": "cached_token",
                                "name": "Geer local endpoint",
                                "description": "Geer manages local endpoint authentication",
                            }
                        ],
                        "agentCapabilities": {
                            "loadSession": True,
                            "mcpCapabilities": {"http": True},
                            "promptCapabilities": {"embeddedContext": True},
                        },
                        "_meta": {
                            "modelState": model_state(self.workspace),
                            "availableCommands": [],
                        },
                    },
                )
                return
            if not self.initialized:
                raise AcpError("Initialize the Geer agent first", -32600)
            if method == "authenticate":
                if params.get("methodId") not in {"cached_token", "xai.api_key"}:
                    raise AcpError("Only Geer's local endpoint authentication is supported", -32602)
                self.reply(identifier, {})
                return
            if method in {"session/new", "session/load"}:
                self.reply(identifier, self._open_session(params, load=method == "session/load"))
                return
            session = self.sessions.get(params.get("sessionId"))
            if session is None:
                raise AcpError("Unknown Geer session", -32602)
            if method == "session/prompt":
                text = _prompt_text(params.get("prompt"), client_name=self.client_name)
                if re.match(r"^/plan(?:\s|$)", text.lstrip(), re.IGNORECASE):
                    raise AcpError(
                        "Geer Pi does not support Plan mode. Use a harness with enforced "
                        "Plan mode if you need tools restricted during planning.",
                        -32602,
                    )
                if not session.prompt_lock.acquire(blocking=False):
                    raise AcpError(
                        "A Pi turn is already running; cancel it before another prompt", -32602
                    )
                session.active = True
                session.cancelled = False
                session.error = None
                session.stop_reason = "end_turn"
                session.streamed.clear()
                session.settled.clear()
                session.dispatched.clear()
                threading.Thread(
                    target=self._prompt, args=(identifier, session, text), daemon=True
                ).start()
            elif method == "session/cancel":
                session.cancelled = True
                if session.active:
                    if not session.dispatched.wait(RPC_TIMEOUT):
                        raise self._retire_session(session, PiRpcTimeout("prompt dispatch"))
                    try:
                        session.rpc("clear_queue")
                        session.rpc("abort", timeout=15)
                    except PiRpcTimeout as error:
                        raise self._retire_session(session, error) from error
                if identifier is not None:
                    self.reply(identifier, {})
            elif method == "session/set_model":
                if session.active:
                    raise AcpError("Cannot change the model during a Pi turn", -32602)
                model_id = params.get("modelId")
                if model_id not in {
                    model["modelId"] for model in session.models["availableModels"]
                }:
                    raise AcpError("Only the installed local Geer model is available", -32602)
                metadata = params.get("_meta", {})
                if not isinstance(metadata, dict):
                    raise AcpError("Model selection _meta must be an object", -32602)
                effort = metadata.get("reasoningEffort")
                if effort is not None and effort != "off":
                    raise AcpError(
                        "Reasoning effort controls are not supported for this model", -32602
                    )
                session.rpc("set_model", provider="geer", modelId=model_id)
                session.models["currentModelId"] = model_id
                self.reply(identifier, {})
            elif method == "session/set_mode":
                expected = "full-access" if session.tools else "text-only"
                if params.get("modeId") != expected:
                    raise AcpError(FULL_ACCESS_MESSAGE, -32602)
                self.reply(identifier, {})
            else:
                raise AcpError(f"Unsupported ACP method: {method}", -32601)
        except (AcpError, AssetError, OSError, ValueError, KeyError, TypeError) as error:
            if identifier is not None or not isinstance(request, dict) or "id" in request:
                self.error(identifier, error)

    def close(self) -> None:
        self._closing = True
        for session in list(self.sessions.values()):
            session.close()


def run_acp(workspace: Workspace, arguments: list[str]) -> int:
    """Handle fast T3 probes or serve ACP over strict LF-delimited stdio."""
    if arguments in (["--version"], ["-v"]):
        from .pi_distribution import PI_VERSION

        print(f"Geer Pi {PI_VERSION}")
        return 0
    if arguments == ["update"]:
        raise AssetError(
            "Geer manages its pinned Pi harness. Update Geer and run `geer setup`; "
            "the Grok updater cannot update this provider."
        )
    if arguments == ["models"]:
        print(models_output(workspace), end="")
        return 0
    if arguments in (["inspect", "--json"], ["inspect"]):
        print(json.dumps(inspect_skills(workspace)))
        return 0
    remaining = list(arguments)
    full_access = "--always-approve" in remaining
    if full_access:
        remaining.remove("--always-approve")
    if "--permission-mode" in remaining:
        index = remaining.index("--permission-mode")
        if index + 1 >= len(remaining) or full_access:
            raise AssetError("Invalid Geer permission mode arguments")
        del remaining[index : index + 2]
    if remaining != ["agent", "stdio"]:
        raise AssetError("Expected Geer ACP launcher arguments: agent [--always-approve] stdio")
    bridge = AcpBridge(workspace, full_access=full_access, output=sys.stdout.buffer)
    try:
        for raw in sys.stdin.buffer:
            try:
                request = json.loads(raw)
            except (json.JSONDecodeError, UnicodeError):
                bridge.error(None, AcpError("Malformed JSON-RPC input", -32700))
                continue
            bridge.handle(request)
    finally:
        bridge.close()
    return 0
