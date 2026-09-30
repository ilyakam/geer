from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from geer.assets import AssetError, Workspace
from geer.cli import main
from geer.onboarding import (
    Prerequisite,
    SetupError,
    add_t3_integration,
    confirm,
    find_pi,
    prepare_t3,
    print_welcome,
    setup,
    setup_complete,
)
from geer.t3 import T3Application
from geer.t3_distribution import T3DistributionError


def test_find_pi_uses_verified_distribution(monkeypatch, tmp_path: Path) -> None:
    command = tmp_path / "pi"
    command.touch(mode=0o755)
    monkeypatch.setattr("geer.onboarding.compatible_pi", lambda workspace: str(command))
    monkeypatch.setattr("geer.onboarding._version", lambda command: "0.99.1")
    result = find_pi()
    assert result.found is True
    assert result.version == "0.99.1"
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
        return {"installed": {"activation": {"reused": True}}}

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
    monkeypatch.setattr("geer.onboarding.confirm", lambda *args, **kwargs: False)
    assert setup(workspace)["status"] == "cancelled"
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
    monkeypatch.setattr("geer.onboarding.find_pi", lambda: Prerequisite(True, "0.99.1"))
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
