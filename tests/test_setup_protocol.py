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
    assert {event["protocol_version"] for event in events} == {2}
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
                        "protocol_version": 2,
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


@pytest.mark.parametrize("choice", ["sage", "ornith", "cancel"])
def test_model_choice_has_family_identity_and_cancellation(choice: str) -> None:
    output = io.StringIO()
    frontend = JsonSetupFrontend(io.StringIO(), output)
    original_emit = frontend.emit

    def answer(event: str, **payload: object) -> None:
        original_emit(event, **payload)
        frontend.input = io.StringIO(json.dumps({
            "protocol_version": 2,
            "decision_id": payload["decision_id"],
            "choice": choice,
        }) + "\n")

    frontend.emit = answer  # type: ignore[method-assign]
    catalog = {"selected": "sage", "models": [
        {"family": "sage", "eligible": True}, {"family": "ornith", "eligible": True},
    ]}
    assert frontend.choose_model(catalog, {"note": "quantized scores unmeasured"}) == (
        None if choice == "cancel" else choice
    )
    event = records(output)[0]
    assert event["event"] == "model_choice_required"
    assert event["catalog"] == catalog
    assert event["comparison"] == {"note": "quantized scores unmeasured"}


@pytest.mark.parametrize("response, message", [
    ({"protocol_version": 1, "choice": "sage"}, "incompatible protocols"),
    ({"protocol_version": 2, "choice": "ornith"}, "unknown decision"),
    ({"protocol_version": 2, "choice": []}, "unknown decision"),
])
def test_model_choice_rejects_old_protocol_and_ineligible_responses(response, message) -> None:
    frontend = JsonSetupFrontend(io.StringIO(), io.StringIO())

    def answer(event: str, **payload: object) -> None:
        frontend.input = io.StringIO(json.dumps({
            "decision_id": payload["decision_id"], **response,
        }) + "\n")

    frontend.emit = answer  # type: ignore[method-assign]
    with pytest.raises(ProtocolError, match=message):
        frontend.choose_model({"models": [
            {"family": "sage", "eligible": True}, {"family": "ornith", "eligible": False},
        ]}, {})


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
            var event: [String: Any] = ["protocol_version": 2, "event": "completed"]
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
        malformed.handle(["protocol_version": 2, "event": "completed", "result": ready])
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
            "protocol_version": 2, "event": "completed", "result": ready
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

        let chooser = SetupViewModel()
        chooser.state = .running
        let pipe = Pipe()
        chooser.input = pipe.fileHandleForWriting
        func option(_ family: String, eligible: Bool) -> [String: Any] {
            var row: [String: Any] = [
             "family": family, "display_name": family, "eligible": eligible,
             "reason": "Not available on this Mac", "model_variant": "6bit",
             "context_window_human": "256K tokens", "download_human": "0 B",
             "required_free_human": "0 B", "installed_human": "26 GiB",
             "recommended": family == "sage", "active": family == "ornith",
             "installed": true]
            if !eligible {
                row["model_variant"] = NSNull()
                row["context_window_human"] = "Unavailable"
            }
            return row
        }
        let choice: [String: Any] = [
            "protocol_version": 2, "event": "model_choice_required", "decision_id": "family",
            "catalog": ["selected": "ornith", "models": [
                option("sage", eligible: false), option("ornith", eligible: true)
            ]],
            "comparison": ["title": "Selected upstream results", "note": "Not quantized scores",
                           "methodology": "Harnesses differ", "source_url": "https://example.org",
                           "rows": [["id": "ifbench", "label": "IFBench",
                                     "description": "Strict prompt accuracy", "ornith": 44.6,
                                     "sage": 81.6],
                                    ["id": "pinchbench", "label": "PinchBench",
                                     "description": "General agent tasks", "ornith": 88.6,
                                     "sage": 90.0]]]
        ]
        var choiceWire = try JSONSerialization.data(withJSONObject: choice)
        choiceWire.append(0x0A)
        chooser.consume(choiceWire)
        precondition(chooser.choosingModel)
        precondition(chooser.selectedFamily == "ornith")
        precondition(chooser.modelOptions.count == 2)
        precondition(chooser.modelOptions[0].variant == "Unavailable")
        precondition(!chooser.modelOptions[0].eligible)
        precondition(chooser.comparison?.rows[0].sage == "81.6")
        precondition(chooser.comparison?.rows[0].ornith == "44.6")
        precondition(chooser.comparison?.rows[1].ornith == "88.6")
        precondition(chooser.comparison?.rows[1].sage == "90.0")
        chooser.answer("sage")
        precondition(chooser.choosingModel)
        chooser.answer("ornith")
        precondition(!chooser.choosingModel)
        let answer = try JSONSerialization.jsonObject(
            with: pipe.fileHandleForReading.availableData
        ) as! [String: Any]
        precondition(answer["protocol_version"] as? Int == 2)
        precondition(answer["decision_id"] as? String == "family")
        precondition(answer["choice"] as? String == "ornith")

        let oldProtocol = SetupViewModel()
        oldProtocol.state = .running
        oldProtocol.handle(["protocol_version": 1, "event": "hello"])
        precondition(oldProtocol.state == .failed)
        precondition(oldProtocol.detail.contains("incompatible protocols"))

        let oldCommand = SetupViewModel()
        oldCommand.state = .running
        oldCommand.stdoutFinished = true
        oldCommand.exitStatus = 2
        oldCommand.finishIfExited()
        precondition(oldCommand.state == .failed)
        precondition(oldCommand.detail.contains("incompatible setup options"))

        for memoryGB in [16, 24] {
            let unsupported = SetupViewModel()
            unsupported.state = .running
            let reason = "Geer requires at least 32 GB of unified memory; detected \(memoryGB) GB"
            var failure = try JSONSerialization.data(withJSONObject: [
                "protocol_version": 2, "event": "failed", "code": "SetupError",
                "message": reason, "recovery_command": "geer setup"
            ])
            failure.append(0x0A)
            unsupported.consume(failure)
            precondition(unsupported.state == .failed)
            precondition(unsupported.detail == reason)
            precondition(!unsupported.choosingModel)
            precondition(unsupported.modelOptions.isEmpty)
        }
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
