from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from geer.setup_protocol import JsonSetupFrontend, ProtocolError, run_json_setup


def records(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_frontend_emits_versioned_lifecycle() -> None:
    output = io.StringIO()
    ready = {
        "status": "ready",
        "t3_configured": True,
        "t3_application_path": "/Applications/T3 Code.app",
    }

    result = run_json_setup(
        lambda frontend: frontend.emit(
            "phase",
            phase="runtime",
            completed=1,
            total=5,
        )
        or ready,
        input_stream=io.StringIO(),
        output_stream=output,
    )

    events = records(output)
    assert result == ready
    assert [event["event"] for event in events] == ["hello", "phase", "completed"]
    assert {event["protocol_version"] for event in events} == {1}
    assert len({event["run_id"] for event in events}) == 1


def test_json_frontend_uses_structured_decision() -> None:
    output = io.StringIO()
    frontend = JsonSetupFrontend(io.StringIO(), output)

    original_emit = frontend.emit

    def emit_with_answer(event: str, **payload: object) -> None:
        original_emit(event, **payload)
        if event == "decision_required":
            frontend.input = io.StringIO(
                json.dumps(
                    {
                        "decision_id": payload["decision_id"],
                        "choice": "no",
                    }
                )
                + "\n"
            )

    frontend.emit = emit_with_answer  # type: ignore[method-assign]

    assert frontend.decide("Continue?", default_yes=True) is False
    decision = records(output)[0]
    assert decision["prompt"] == "Continue?"
    assert decision["default"] == "yes"


def test_json_frontend_rejects_eof() -> None:
    frontend = JsonSetupFrontend(io.StringIO(), io.StringIO())

    with pytest.raises(ProtocolError, match="closed before answering"):
        frontend.decide("Continue?", default_yes=True)


def test_json_frontend_reports_failures() -> None:
    output = io.StringIO()

    def fail(frontend: JsonSetupFrontend) -> dict[str, object]:
        raise RuntimeError("network unavailable")

    with pytest.raises(RuntimeError, match="network unavailable"):
        run_json_setup(fail, input_stream=io.StringIO(), output_stream=output)

    failure = records(output)[-1]
    assert failure["event"] == "failed"
    assert failure["recovery_command"] == "geer setup"


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None,
    reason="native setup contract requires the macOS Swift toolchain",
)
def test_native_setup_requires_verified_t3_completion(tmp_path: Path) -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "packaging/macos/GeerSetup/Sources/GeerSetupApp/GeerSetupApp.swift"
    ).read_text()
    app_source, marker, _ = source.partition("@main\nstruct GeerSetupApp: App")
    assert marker
    harness = tmp_path / "SetupContract.swift"
    harness.write_text(
        app_source
        + r'''
extension SetupViewModel {
    @MainActor
    static func verifyCompletionContract() throws {
        let application = "/Applications/T3 Code.app"
        let ready: [String: Any] = [
            "status": "ready",
            "t3_configured": true,
            "t3_application_path": application
        ]

        func completion(_ result: [String: Any]?) throws -> SetupViewModel {
            let model = SetupViewModel()
            model.state = .running
            var event: [String: Any] = ["protocol_version": 1, "event": "completed"]
            if let result { event["result"] = result }
            var data = try JSONSerialization.data(withJSONObject: event)
            data.append(0x0A)
            let midpoint = data.count / 2
            model.consume(data.prefix(midpoint))
            precondition(model.state == .running)
            model.consume(data.suffix(from: midpoint))
            return model
        }

        let success = try completion(ready)
        precondition(success.state == .ready)
        precondition(success.t3ApplicationURL?.path == application)
        success.stdoutFinished = true
        success.exitStatus = 0
        success.finishIfExited()
        precondition(success.state == .ready)

        let incomplete: [[String: Any]?] = [
            nil,
            [:],
            ["status": "unknown"],
            ["status": "ready"],
            ["status": "ready", "t3_configured": false, "t3_application_path": application],
            ["status": "ready", "t3_configured": 1, "t3_application_path": application],
            ["status": "ready", "t3_configured": "true", "t3_application_path": application],
            ["status": "ready", "t3_configured": true],
            ["status": "ready", "t3_configured": true, "t3_application_path": "T3 Code.app"],
            ["status": "ready", "t3_configured": true, "t3_application_path": "/bad\0path"]
        ]
        for result in incomplete {
            let model = try completion(result)
            precondition(model.state == .failed)
            precondition(model.t3ApplicationURL == nil)
            precondition(!model.detail.isEmpty)
        }

        let cancelled = try completion(["status": "cancelled"])
        precondition(cancelled.state == .cancelled)
        precondition(cancelled.t3ApplicationURL == nil)

        let malformed = SetupViewModel()
        malformed.state = .running
        malformed.consume(Data("not JSON\n".utf8))
        precondition(malformed.state == .failed)
        malformed.handle(["protocol_version": 1, "event": "completed", "result": ready])
        precondition(malformed.state == .failed)

        let missing = SetupViewModel()
        missing.state = .running
        missing.stdoutFinished = true
        missing.exitStatus = 0
        missing.finishIfExited()
        precondition(missing.state == .failed)

        let truncated = SetupViewModel()
        truncated.state = .running
        truncated.consume(try JSONSerialization.data(withJSONObject: [
            "protocol_version": 1, "event": "completed", "result": ready
        ]))
        truncated.stdoutFinished = true
        truncated.exitStatus = 0
        truncated.finishIfExited()
        precondition(truncated.state == .failed)

        let failedExit = try completion(ready)
        failedExit.stdoutFinished = true
        failedExit.exitStatus = 1
        failedExit.finishIfExited()
        precondition(failedExit.state == .failed)
    }
}

@main
struct SetupContract {
    @MainActor
    static func main() throws {
        try SetupViewModel.verifyCompletionContract()
    }
}
'''
    )
    executable = tmp_path / "SetupContract"
    compiled = subprocess.run(
        ["swiftc", "-parse-as-library", "-swift-version", "5", str(harness), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    subprocess.run([str(executable)], check=True, capture_output=True, text=True, timeout=10)
