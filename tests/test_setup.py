from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import test_models

from geer.assets import AssetError, Workspace
from geer.cli import main
from geer.onboarding import (
    Prerequisite,
    SetupError,
    add_t3_integration,
    choose_model,
    confirm,
    find_pi,
    prepare_t3,
    print_welcome,
    setup,
    setup_complete,
)
from geer.t3 import T3Application
from geer.t3_distribution import T3DistributionError

model_inventory = test_models.inventory


def test_find_pi_uses_verified_distribution(monkeypatch, tmp_path: Path) -> None:
    command = tmp_path / "pi"
    command.touch(mode=0o755)
    monkeypatch.setattr("geer.onboarding.compatible_pi", lambda workspace: str(command))
    monkeypatch.setattr("geer.onboarding._version", lambda command: "1.4.0")
    result = find_pi()
    assert result.found is True
    assert result.version == "1.4.0"
    assert result.path == command


def test_find_pi_reports_missing_without_installing(monkeypatch) -> None:
    def missing(workspace):
        raise AssetError("Pi missing")

    monkeypatch.setattr("geer.onboarding.compatible_pi", missing)
    assert find_pi().found is False


def test_confirm_uses_requested_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert confirm("Continue?", default_yes=True, assume_yes=False) is True
    assert confirm("Continue?", default_yes=False, assume_yes=False) is False


def test_confirm_requires_tty_without_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SetupError, match="interactive consent"):
        confirm("Continue?", default_yes=False, assume_yes=False)


@pytest.mark.parametrize("configured", [True, False])
@pytest.mark.parametrize("live_ready", [True, False])
def test_setup_completion_requires_live_required_integration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
    live_ready: bool,
) -> None:
    workspace = Workspace(tmp_path)
    assert setup_complete(workspace) is False
    workspace.state.mkdir(parents=True)
    workspace.setup_state.write_text(
        json.dumps(
            {
                "status": "ready",
                "harness": "pi",
                "t3_configured": configured,
            }
        )
    )
    monkeypatch.setattr("geer.onboarding.compatible_pi", lambda value: "/bin/pi")
    monkeypatch.setattr("geer.onboarding.t3_status", lambda value: {"ready": live_ready})
    assert setup_complete(workspace) is (configured and live_ready)


def test_setup_completion_rejects_removed_pi(tmp_path: Path, monkeypatch) -> None:
    workspace = Workspace(tmp_path)
    workspace.state.mkdir(parents=True)
    workspace.setup_state.write_text('{"status":"ready","harness":"pi","t3_configured":true}')

    def missing(value):
        raise AssetError("Pi was removed")

    monkeypatch.setattr("geer.onboarding.compatible_pi", missing)
    assert setup_complete(workspace) is False


@pytest.fixture
def setup_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = Workspace(tmp_path)
    plan = {
        "display_name": "Geer Test Model",
        "family": "sage",
        "active": True,
        "eligible": True,
        "activation_changes": False,
        "active_model_installed": True,
        "download_human": "1.0 GiB",
        "runtime_download_human": "300.0 MiB",
        "runtime_installed_human": "1.0 GiB",
        "t3_download_human": "131.2 MiB",
        "t3_installed_human": "318.8 MiB",
        "temporary_human": "2.0 GiB",
        "required_free_human": "4.0 GiB",
        "active_link": str(workspace.runtime / "models/active"),
        "reusable_active_model": True,
    }
    calls: list[str] = []
    monkeypatch.setattr("geer.onboarding.platform.system", lambda: "Darwin")
    monkeypatch.setattr("geer.onboarding.platform.machine", lambda: "arm64")
    monkeypatch.setattr("geer.onboarding.memory_bytes", lambda: 128 * 1024**3)
    monkeypatch.setattr("geer.onboarding.model_plan", lambda value, **kwargs: plan)
    monkeypatch.setattr("geer.onboarding.model_catalog", lambda value, **kwargs: {
        "selected": "sage",
        "models": [{
            "family": "sage", "display_name": "Geer Test Model", "recommended": True,
            "active": True, "installed": True, "eligible": True, "reason": "",
            "model_variant": "6bit", "context_window_human": "256K tokens",
            "download_human": "0 B", "required_free_human": "0 B", "installed_human": "1 GiB",
        }],
    })
    monkeypatch.setattr(
        "geer.onboarding.prerequisite_snapshot",
        lambda: {
            "pi": Prerequisite(False),
            "t3": T3Application(False),
        },
    )
    monkeypatch.setattr("geer.onboarding.bootstrap_runtime", lambda value: calls.append("runtime"))
    monkeypatch.setattr("geer.onboarding._probe_mlx", lambda value: calls.append("mlx"))
    monkeypatch.setattr("geer.onboarding.bootstrap_pi", lambda value: calls.append("pi"))
    monkeypatch.setattr("geer.onboarding.prepare_t3", lambda value: calls.append("t3"))

    def model(*args, **kwargs):
        calls.append("model")
        return {"reused": True}

    monkeypatch.setattr("geer.onboarding.install_model", model)
    monkeypatch.setattr("geer.onboarding.retrieval_canary", lambda value: calls.append("retrieval"))
    monkeypatch.setattr(
        "geer.onboarding.start_server",
        lambda value: {
            "endpoint": "http://127.0.0.1:8765",
        },
    )
    monkeypatch.setattr("geer.onboarding.canary", lambda *args: calls.append("canary"))

    def integration(*args, **kwargs):
        calls.append("configure")
        return {"t3_application_path": str(tmp_path / "Applications/T3 Code.app")}

    monkeypatch.setattr("geer.onboarding.add_t3_integration", integration)
    return workspace, plan, calls


def test_setup_requires_t3_before_model_and_migrates_after_canary(
    setup_environment, capsys
) -> None:
    workspace, plan, calls = setup_environment
    result = setup(workspace, assume_yes=True)
    assert calls == ["runtime", "mlx", "pi", "t3", "model", "retrieval", "canary", "configure"]
    state = json.loads(workspace.setup_state.read_text())
    assert state["status"] == result["status"] == "ready"
    assert state["t3_configured"] is result["t3_configured"] is True
    assert Path(result["t3_application_path"]).is_absolute()
    output = capsys.readouterr().out
    assert "T3 Code will be installed automatically" in output
    assert "Reused the verified active model" in output
    assert "Full access" in output
    assert "To add T3 Code later" not in output


@pytest.mark.parametrize("family", ["sage", "ornith"])
def test_wizard_settles_with_real_core_activation_and_same_artifact_repair(
    model_inventory, monkeypatch, family,
) -> None:
    from geer import models
    from geer.setup_protocol import run_json_setup

    # Reuse the core's synthetic artifact fixture, including real integrity,
    # acquisition, activation, asset/settings writes, and saved-family handling.
    inventory = model_inventory
    workspace = inventory.workspace
    target = inventory.make(family)
    monkeypatch.setattr("geer.onboarding.platform.system", lambda: "Darwin")
    monkeypatch.setattr("geer.onboarding.platform.machine", lambda: "arm64")
    monkeypatch.setattr("geer.onboarding.memory_bytes", lambda: 128 * 1024**3)
    monkeypatch.setattr("geer.onboarding.prerequisite_snapshot", lambda: {
        "pi": Prerequisite(False), "t3": T3Application(False),
    })
    stages = []
    for name in (
        "bootstrap_runtime", "_probe_mlx", "bootstrap_pi", "prepare_t3", "retrieval_canary",
    ):
        monkeypatch.setattr(
            f"geer.onboarding.{name}", lambda *a, stage=name: stages.append(stage),
        )
    monkeypatch.setattr("geer.onboarding.start_server", lambda *a: (
        stages.append("start_server") or {"endpoint": "http://127.0.0.1:8765"}
    ))
    monkeypatch.setattr("geer.onboarding.canary", lambda *a: stages.append("canary"))
    monkeypatch.setattr("geer.onboarding.add_t3_integration", lambda *a, **kw: (
        stages.append("integration") or {"t3_application_path": "/Applications/T3 Code.app"}
    ))
    monkeypatch.setattr(
        "geer.onboarding.running_t3_applications", lambda: pytest.fail("unexpected quit probe"),
    )

    def wizard(*, repair=False):
        output = io.StringIO()

        def operation(frontend):
            emit = frontend.emit

            def answer(event, **payload):
                emit(event, **payload)
                if event in {"model_choice_required", "decision_required"}:
                    frontend.input = io.StringIO(json.dumps({
                        "protocol_version": 2, "decision_id": payload["decision_id"],
                        "choice": family if event == "model_choice_required" else "yes",
                    }) + "\n")

            frontend.emit = answer
            return setup(workspace, frontend=frontend, choose_family=not repair)

        result = run_json_setup(
            operation, input_stream=io.StringIO(), output_stream=output,
        )
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        assert events[-1]["event"] == "completed"
        assert events[-1]["result"]["status"] == result["status"] == "ready"
        assert result["t3_configured"] is True
        assert json.loads(workspace.setup_state.read_text())["status"] == "ready"
        assert json.loads(models._selection_path(workspace).read_text()) == {
            "schema_version": 1, "family": family, "confirmed": True,
        }
        assert (workspace.models / "active").resolve() == target
        assert inventory.stopped == []
        return result, events

    installed, initial_events = wizard()
    assert installed["installed"]["installed"]["activation"] is None
    assert installed["installed"]["reused"] is False
    assert any(event["event"] == "model_choice_required" for event in initial_events)
    assert stages == [
        "bootstrap_runtime", "_probe_mlx", "bootstrap_pi", "prepare_t3", "retrieval_canary",
        "start_server", "canary", "integration",
    ]

    repaired, repair_events = wizard(repair=True)
    assert repaired["installed"]["installed"]["activation"] is None
    assert repaired["installed"]["reused"] is True
    assert not any(event["event"] == "model_choice_required" for event in repair_events)
    assert any(
        "Reused the verified active model" in event.get("message", "")
        for event in repair_events if event["event"] == "log"
    )


@pytest.mark.parametrize("failure_phase", ["t3", "configure", "canary"])
def test_required_failure_clears_ready_and_preserves_retained_model(
    setup_environment,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
    capsys,
) -> None:
    workspace, plan, calls = setup_environment
    retained = workspace.runtime / "models/snapshot/weights.safetensors"
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"retained model")
    active = workspace.runtime / "models/active"
    active.symlink_to(retained.parent, target_is_directory=True)
    workspace.state.mkdir(parents=True)
    workspace.setup_state.write_text('{"status":"ready","harness":"pi","t3_configured":true}')
    targets = {"t3": "prepare_t3", "configure": "add_t3_integration", "canary": "canary"}

    def fail(*args, **kwargs):
        raise SetupError(f"{failure_phase} failed: synthetic reason")

    monkeypatch.setattr(f"geer.onboarding.{targets[failure_phase]}", fail)
    with pytest.raises(SetupError, match="synthetic reason"):
        setup(workspace, assume_yes=True)
    state = json.loads(workspace.setup_state.read_text())
    assert state["status"] == "failed"
    assert state["t3_configured"] is False
    assert "synthetic reason" in state["error"]
    assert retained.read_bytes() == b"retained model"
    assert active.resolve() == retained.parent
    if failure_phase == "t3":
        assert "model" not in calls
    assert "Geer is ready" not in capsys.readouterr().out


def test_plan_and_cancellation_do_not_acquire_or_mark_ready(setup_environment, monkeypatch) -> None:
    workspace, plan, calls = setup_environment
    assert setup(workspace, plan_only=True)["status"] == "planned"
    assert calls == []
    assert not workspace.setup_state.exists()
    monkeypatch.setattr("geer.onboarding.choose_model", lambda *args, **kwargs: "sage")
    monkeypatch.setattr("geer.onboarding.confirm", lambda *args, **kwargs: False)
    assert setup(workspace)["status"] == "cancelled"
    assert calls == []
    assert not workspace.setup_state.exists()


@pytest.mark.parametrize("family", ["sage", "ornith"])
def test_explicit_setup_resolves_family_once_and_preserves_install_identity(
    setup_environment, monkeypatch, family,
) -> None:
    workspace, plan, calls = setup_environment
    selections = []
    plan["family"] = family

    def resolve(value, **kwargs):
        selections.append(kwargs)
        return plan

    monkeypatch.setattr("geer.onboarding.model_plan", resolve)
    monkeypatch.setattr("geer.onboarding.choose_model", lambda *a: pytest.fail("chooser"))

    def install(value, **kwargs):
        assert kwargs["family"] == family
        return {"reused": True}

    monkeypatch.setattr("geer.onboarding.install_model", install)
    setup(workspace, family=family, assume_yes=True)
    assert selections == [{"family": family, "total_memory_bytes": 128 * 1024**3}]


def test_noninteractive_setup_defers_default_policy_to_core(setup_environment, monkeypatch) -> None:
    workspace, plan, calls = setup_environment
    selected = []
    monkeypatch.setattr(
        "geer.onboarding.model_plan", lambda value, **kw: selected.append(kw) or plan,
    )
    monkeypatch.setattr("geer.onboarding.choose_model", lambda *a: pytest.fail("chooser"))
    setup(workspace, assume_yes=True)
    assert selected[0]["family"] is None


def test_model_choice_cancellation_is_before_acquisition(setup_environment, monkeypatch) -> None:
    workspace, plan, calls = setup_environment
    monkeypatch.setattr("geer.onboarding.choose_model", lambda *a: None)
    monkeypatch.setattr("geer.onboarding.model_plan", lambda *a, **kw: pytest.fail("plan"))
    assert setup(workspace) == {"status": "cancelled"}
    assert calls == []
    assert not workspace.setup_state.exists()


def test_changed_active_model_never_silently_quits_t3(setup_environment, monkeypatch) -> None:
    workspace, plan, calls = setup_environment
    plan.update(active_model_installed=True, active=False, activation_changes=True)
    monkeypatch.setattr("geer.onboarding.running_t3_applications", lambda: [Path("/T3.app")])
    monkeypatch.setattr("geer.onboarding._quit_t3", lambda *a: pytest.fail("quit"))
    with pytest.raises(SetupError, match="Quit T3 Code"):
        setup(workspace, family="sage", assume_yes=True)
    assert calls == []
    assert not workspace.setup_state.exists()


def test_model_switch_closes_t3_after_consent_before_downloads(
    setup_environment, monkeypatch,
) -> None:
    workspace, plan, calls = setup_environment
    plan.update(active_model_installed=True, active=False, activation_changes=True)
    live = [Path("/T3.app")]
    monkeypatch.setattr("geer.onboarding.running_t3_applications", lambda: list(live))
    monkeypatch.setattr("geer.onboarding.confirm", lambda *a, **kw: True)

    def quit_t3(app):
        calls.append("quit")
        live.remove(app)

    monkeypatch.setattr("geer.onboarding._quit_t3", quit_t3)
    setup(workspace, family="sage")
    assert calls[0:2] == ["quit", "runtime"]


def test_same_active_model_does_not_probe_or_quit_t3(setup_environment, monkeypatch) -> None:
    workspace, plan, calls = setup_environment
    plan.update(active_model_installed=True, active=True)
    monkeypatch.setattr(
        "geer.onboarding.running_t3_applications", lambda: pytest.fail("quit probe"),
    )
    setup(workspace, family="sage", assume_yes=True)


def test_first_install_keeps_running_t3_until_configuration(setup_environment, monkeypatch) -> None:
    workspace, plan, calls = setup_environment
    plan.update(active_model_installed=False, active=False, activation_changes=True)
    monkeypatch.setattr(
        "geer.onboarding.running_t3_applications", lambda: pytest.fail("early quit probe"),
    )
    setup(workspace, family="sage", assume_yes=True)


def test_model_switch_checks_t3_closed_after_consent(setup_environment, monkeypatch) -> None:
    workspace, plan, calls = setup_environment
    plan.update(active_model_installed=True, active=False, activation_changes=True)
    monkeypatch.setattr("geer.onboarding.running_t3_applications", lambda: [Path("/T3.app")])
    monkeypatch.setattr("geer.onboarding.confirm", lambda *a, **kw: True)
    monkeypatch.setattr("geer.onboarding._quit_t3", lambda app: None)
    with pytest.raises(SetupError, match="still running"):
        setup(workspace, family="sage")
    assert calls == []
    assert not workspace.setup_state.exists()


def test_ineligible_model_is_visible_but_never_acquired(setup_environment, capsys) -> None:
    workspace, plan, calls = setup_environment
    plan.update(eligible=False, reason="This model is not published yet.")
    assert setup(workspace, plan_only=True)["plan"]["eligible"] is False
    with pytest.raises(SetupError, match="not published yet"):
        setup(workspace, family="sage", assume_yes=True)
    assert "Selected model" in capsys.readouterr().out
    assert calls == []


@pytest.fixture
def unsupported_setup_environment(tmp_path, monkeypatch):
    from geer import models

    workspace = Workspace(Path(__file__).resolve().parents[1], tmp_path / "home")
    # Use the real hardware policy and catalog, without probing installed services.
    monkeypatch.setattr(models, "_dependency_plan", lambda _: {
        f"{name}_{field}_bytes": 0
        for name in ("runtime", "pi", "t3")
        for field in ("download", "required_free")
    })
    monkeypatch.setattr("geer.onboarding.platform.system", lambda: "Darwin")
    monkeypatch.setattr("geer.onboarding.platform.machine", lambda: "arm64")
    monkeypatch.setattr("geer.onboarding.prerequisite_snapshot", lambda: {
        "pi": Prerequisite(False), "t3": T3Application(False),
    })
    monkeypatch.setattr("geer.onboarding.choose_model", lambda *a: pytest.fail("model question"))
    monkeypatch.setattr("geer.onboarding.confirm", lambda *a, **kw: pytest.fail("confirmation"))
    monkeypatch.setattr(
        "geer.onboarding._prepare_and_validate", lambda *a: pytest.fail("acquisition"),
    )
    monkeypatch.setattr(
        "geer.onboarding._write_setup_state", lambda *a, **kw: pytest.fail("state write"),
    )
    return workspace


@pytest.mark.parametrize("memory_gib", [16, 24])
@pytest.mark.parametrize("family", [None, "sage", "ornith"])
@pytest.mark.parametrize("native", [False, True])
def test_unsupported_memory_fails_before_model_question_or_acquisition(
    unsupported_setup_environment, monkeypatch, memory_gib, family, native,
) -> None:
    from geer.setup_protocol import run_json_setup

    workspace = unsupported_setup_environment
    monkeypatch.setattr("geer.onboarding.memory_bytes", lambda: memory_gib * 1024**3)
    reason = f"Geer requires at least 32 GB of unified memory; detected {memory_gib} GB"
    output = io.StringIO()
    with pytest.raises(SetupError, match=reason):
        if native:
            run_json_setup(
                lambda frontend: setup(workspace, family=family, frontend=frontend),
                input_stream=io.StringIO(), output_stream=output,
            )
        else:
            setup(workspace, family=family)
    if native:
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        assert [event["event"] for event in events] == ["hello", "failed"]
        assert events[-1]["message"] == reason
    assert not workspace.runtime.exists()


@pytest.mark.parametrize("memory_gib", [16, 24])
@pytest.mark.parametrize("family", [None, "sage", "ornith"])
def test_unsupported_memory_plan_retains_diagnostic_catalog_without_questions(
    unsupported_setup_environment, monkeypatch, memory_gib, family,
) -> None:
    workspace = unsupported_setup_environment
    monkeypatch.setattr("geer.onboarding.memory_bytes", lambda: memory_gib * 1024**3)
    result = setup(workspace, family=family, plan_only=True)
    assert result["status"] == "planned"
    assert result["plan"]["eligible"] is False
    assert result["catalog"]["selected"] == (family or "sage")
    assert len(result["catalog"]["models"]) == 2
    assert all(row["eligible"] is False for row in result["catalog"]["models"])
    assert all(row["model_variant"] is None for row in result["catalog"]["models"])
    assert str(memory_gib) in result["plan"]["reason"]
    assert not workspace.runtime.exists()


@pytest.mark.parametrize("selection_source, explicit_setup, asks", [
    ("saved", False, False), ("saved", True, True), ("recommended", False, True),
])
def test_implicit_repair_reuses_saved_family_without_repeating_chooser(
    setup_environment, monkeypatch, selection_source, explicit_setup, asks,
) -> None:
    from geer import onboarding

    workspace, plan, calls = setup_environment
    catalog = onboarding.model_catalog(workspace)
    catalog["selection_source"] = selection_source
    monkeypatch.setattr("geer.onboarding.model_catalog", lambda *a, **kw: catalog)
    questions = []
    monkeypatch.setattr(
        "geer.onboarding.choose_model", lambda *a: questions.append(True) or "sage",
    )
    monkeypatch.setattr("geer.onboarding.confirm", lambda *a, **kw: False)
    assert setup(workspace, choose_family=explicit_setup)["status"] == "cancelled"
    assert bool(questions) == asks
    assert calls == []


def test_terminal_choice_rejects_ineligible_family_then_accepts_number(monkeypatch) -> None:
    monkeypatch.setattr("geer.onboarding.sys.stdin.isatty", lambda: True)
    answers = iter(["sage", "2"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    catalog = {"selected": "sage", "models": [
        {"family": "sage", "eligible": False, "reason": "Unpublished"},
        {"family": "ornith", "eligible": True},
    ]}
    assert choose_model(catalog, {}) == "ornith"


def test_native_setup_choice_resolves_before_download_and_confirmation(
    setup_environment, monkeypatch,
) -> None:
    from geer import onboarding
    from geer.setup_protocol import JsonSetupFrontend

    workspace, plan, calls = setup_environment
    plan["family"] = "ornith"
    catalog = onboarding.model_catalog(workspace)
    catalog["models"].append({**catalog["models"][0], "family": "ornith"})
    monkeypatch.setattr("geer.onboarding.model_catalog", lambda *a, **kw: catalog)
    selections = []
    monkeypatch.setattr(
        "geer.onboarding.model_plan", lambda value, **kw: selections.append(kw) or plan,
    )
    output = io.StringIO()
    frontend = JsonSetupFrontend(io.StringIO(), output)
    emit = frontend.emit

    def answer(event, **payload):
        emit(event, **payload)
        if event in {"model_choice_required", "decision_required"}:
            frontend.input = io.StringIO(json.dumps({
                "protocol_version": 2, "decision_id": payload["decision_id"],
                "choice": "ornith" if event == "model_choice_required" else "no",
            }) + "\n")

    frontend.emit = answer
    assert setup(workspace, frontend=frontend)["status"] == "cancelled"
    assert selections[0]["family"] == "ornith"
    events = [json.loads(line)["event"] for line in output.getvalue().splitlines()]
    assert events.index("model_choice_required") < events.index("plan")
    assert events.index("plan") < events.index("decision_required")
    assert calls == []
    assert not workspace.setup_state.exists()


def test_welcome_explains_automatic_required_components(setup_environment, capsys) -> None:
    workspace, plan, calls = setup_environment
    print_welcome({"pi": Prerequisite(False), "t3": T3Application(False)}, plan)
    output = capsys.readouterr().out
    assert "Pi will be installed automatically" in output
    assert "T3 Code will be installed automatically" in output
    assert "T3 Code download:" in output
    assert "Run T3 Code once" not in output


@pytest.mark.parametrize(
    ("name", "version", "running"),
    [
        ("T3 Code (Alpha).app", "0.0.44", False),
        ("T3 Code (Nightly).app", "0.0.43-nightly.20260923.2150", True),
    ],
)
def test_prepare_t3_reuses_compatible_existing_app_without_download(
    tmp_path: Path, monkeypatch, name: str, version: str, running: bool
) -> None:
    app = tmp_path / name
    monkeypatch.setattr(
        "geer.onboarding.find_t3", lambda: T3Application(True, version, app, running)
    )
    monkeypatch.setattr(
        "geer.onboarding.install_t3", lambda *args, **kwargs: pytest.fail("download")
    )
    assert prepare_t3(Workspace(tmp_path)) == app


def test_prepare_t3_surfaces_acquisition_reason(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("geer.onboarding.find_t3", lambda: T3Application(False))

    def fail(*args, **kwargs):
        raise T3DistributionError("download checksum mismatch")

    monkeypatch.setattr("geer.onboarding.install_t3", fail)
    with pytest.raises(SetupError, match="T3 Code is required.*checksum mismatch"):
        prepare_t3(Workspace(tmp_path))


def test_old_t3_requires_updating_the_existing_app_without_installing_another(
    tmp_path: Path, monkeypatch, setup_environment, capsys,
) -> None:
    _, plan, _ = setup_environment
    old = T3Application(False, "0.0.28", tmp_path / "T3 Code (Alpha).app")
    monkeypatch.setattr("geer.onboarding.find_t3", lambda: old)
    monkeypatch.setattr(
        "geer.onboarding.install_t3", lambda *args, **kwargs: pytest.fail("duplicate app")
    )
    with pytest.raises(SetupError, match="Update that app.*No additional T3 Code app"):
        prepare_t3(Workspace(tmp_path))
    print_welcome({"pi": Prerequisite(False), "t3": old}, plan)
    output = capsys.readouterr().out
    assert "T3 Code 0.0.28 needs an update" in output
    assert "T3 Code will be installed automatically" not in output


def test_real_distribution_error_is_reported_and_marks_setup_failed(
    setup_environment, monkeypatch, capsys,
) -> None:
    workspace, plan, calls = setup_environment
    monkeypatch.setattr("geer.cli.find_workspace", lambda: workspace)
    monkeypatch.setattr("geer.onboarding.prepare_t3", prepare_t3)
    monkeypatch.setattr("geer.onboarding.find_t3", lambda: T3Application(False))

    def fail(*args, **kwargs):
        raise T3DistributionError("official signature verification failed")

    monkeypatch.setattr("geer.onboarding.install_t3", fail)
    assert main(["setup", "--yes"]) == 1
    state = json.loads(workspace.setup_state.read_text())
    assert state["status"] == "failed"
    assert "official signature verification failed" in state["error"]
    assert "model" not in calls
    assert "T3 Code is required" in capsys.readouterr().err


@pytest.fixture
def integration_environment(tmp_path: Path, monkeypatch):
    app = tmp_path / "T3 Code.app"
    monkeypatch.setattr("geer.onboarding.find_pi", lambda: Prerequisite(True, "1.4.0"))
    monkeypatch.setattr("geer.onboarding.find_t3", lambda: T3Application(True, "0.0.44", app))
    monkeypatch.setattr(
        "geer.onboarding.t3_settings_path", lambda: tmp_path / ".t3/userdata/settings.json"
    )
    monkeypatch.setattr(
        "geer.onboarding.t3_status",
        lambda *args: {
            "ready": True,
            "application_path": str(app),
        },
    )
    return Workspace(tmp_path), app


def test_unchanged_integration_does_not_quit_running_t3(
    integration_environment, monkeypatch
) -> None:
    workspace, app = integration_environment
    monkeypatch.setattr("geer.onboarding.configure_t3", lambda *args, **kwargs: {"changed": False})
    monkeypatch.setattr(
        "geer.onboarding.running_t3_applications", lambda: pytest.fail("quit probe")
    )
    monkeypatch.setattr("geer.onboarding._quit_t3", lambda value: pytest.fail("quit"))
    assert add_t3_integration(workspace, assume_yes=True)["t3_application_path"] == str(app)


@pytest.mark.parametrize("assume_yes", [True, False])
def test_changed_integration_cannot_silently_skip_or_quit(
    integration_environment,
    monkeypatch,
    assume_yes: bool,
) -> None:
    workspace, app = integration_environment
    writes: list[bool] = []

    def configure(*args, **kwargs):
        if not kwargs.get("dry_run"):
            writes.append(True)
        return {"changed": True}

    monkeypatch.setattr("geer.onboarding.configure_t3", configure)
    monkeypatch.setattr("geer.onboarding.running_t3_applications", lambda: [app])
    monkeypatch.setattr("geer.onboarding._quit_t3", lambda value: pytest.fail("quit"))
    monkeypatch.setattr("geer.onboarding.confirm", lambda *args, **kwargs: False)
    with pytest.raises(SetupError, match="Quit T3 Code"):
        add_t3_integration(workspace, assume_yes=assume_yes)
    assert writes == []


def test_changed_integration_stops_all_t3_apps_after_consent(
    integration_environment, monkeypatch
) -> None:
    workspace, app = integration_environment
    other = app.with_name("Old T3.app")
    live = [app, other]
    writes: list[bool] = []

    def configure(*args, **kwargs):
        if not kwargs.get("dry_run"):
            assert not live
            writes.append(True)
        return {"changed": True}

    monkeypatch.setattr("geer.onboarding.configure_t3", configure)
    monkeypatch.setattr("geer.onboarding.running_t3_applications", lambda: list(live))
    monkeypatch.setattr("geer.onboarding.confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr("geer.onboarding._quit_t3", lambda value: live.remove(value))
    add_t3_integration(workspace)
    assert writes == [True]


def test_post_write_validation_failure_is_not_ready(integration_environment, monkeypatch) -> None:
    workspace, app = integration_environment
    monkeypatch.setattr("geer.onboarding.configure_t3", lambda *args, **kwargs: {"changed": False})
    monkeypatch.setattr(
        "geer.onboarding.t3_status", lambda *args: {"ready": False, "error": "disabled provider"}
    )
    with pytest.raises(SetupError, match="disabled provider"):
        add_t3_integration(workspace)
