from __future__ import annotations

import io
import json

import pytest

from geer.setup_protocol import JsonSetupFrontend, ProtocolError, run_json_setup


def records(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_frontend_emits_versioned_lifecycle() -> None:
    output = io.StringIO()

    result = run_json_setup(
        lambda frontend: frontend.emit(
            "phase",
            phase="runtime",
            completed=1,
            total=5,
        )
        or {"status": "ready"},
        input_stream=io.StringIO(),
        output_stream=output,
    )

    events = records(output)
    assert result == {"status": "ready"}
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
