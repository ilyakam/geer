from __future__ import annotations

import json
import sys
import threading
import uuid
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import asdict, is_dataclass
from typing import Any, TextIO

PROTOCOL_VERSION = 1


class ProtocolError(RuntimeError):
    """Raised when a graphical setup client sends an invalid response."""


class JsonSetupFrontend:
    """Versioned JSON Lines bridge used by the native setup application."""

    def __init__(self, input_stream: TextIO, output_stream: TextIO) -> None:
        self.input = input_stream
        self.output = output_stream
        self._write_lock = threading.Lock()
        self.run_id = str(uuid.uuid4())

    def emit(self, event: str, **payload: Any) -> None:
        record = {
            "protocol_version": PROTOCOL_VERSION,
            "event": event,
            "run_id": self.run_id,
            **payload,
        }
        with self._write_lock:
            self.output.write(json.dumps(record, default=_json_default) + "\n")
            self.output.flush()

    def output_text(self, value: str = "", **_: Any) -> None:
        self.emit("log", message=value)

    def decide(self, prompt: str, *, default_yes: bool) -> bool:
        decision_id = str(uuid.uuid4())
        self.emit(
            "decision_required",
            decision_id=decision_id,
            prompt=prompt,
            default="yes" if default_yes else "no",
            choices=["yes", "no"],
        )
        line = self.input.readline()
        if not line:
            raise ProtocolError("the setup window closed before answering")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as error:
            raise ProtocolError("the setup window sent an invalid response") from error
        if response.get("decision_id") != decision_id:
            raise ProtocolError("the setup window answered the wrong decision")
        choice = response.get("choice")
        if choice not in {"yes", "no"}:
            raise ProtocolError("the setup window sent an unknown decision")
        return choice == "yes"


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__fspath__"):
        return str(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


def run_json_setup(
    operation: Callable[[JsonSetupFrontend], dict[str, Any]],
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> dict[str, Any]:
    """Run setup with protocol-only stdout and diagnostic stderr."""

    frontend = JsonSetupFrontend(
        input_stream or sys.stdin,
        output_stream or sys.stdout,
    )
    frontend.emit(
        "hello",
        capabilities=["decisions", "phase_progress", "logs", "resume"],
    )
    try:
        with redirect_stdout(sys.stderr):
            result = operation(frontend)
    except Exception as error:
        frontend.emit(
            "failed",
            code=type(error).__name__,
            message=str(error),
            recovery_command="geer setup",
        )
        raise
    frontend.emit("completed", result=result)
    return result
