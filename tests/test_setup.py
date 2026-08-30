from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from geer.assets import Workspace
from geer.onboarding import (
    Prerequisite,
    SetupError,
    _t3_running,
    add_t3_integration,
    confirm,
    find_claude,
    find_t3,
    print_missing,
    print_welcome,
    setup,
    setup_complete,
)


def test_find_claude_checks_standard_install_location(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    command = tmp_path / "claude"
    command.touch(mode=0o755)
    monkeypatch.setattr("geer.onboarding.shutil.which", lambda name: str(command))
    monkeypatch.setattr(
        "geer.onboarding.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout="2.1.220 (Claude Code)\n",
        ),
    )

    result = find_claude()

    assert result.found is True
    assert result.version == "2.1.220"
    assert result.path == command


def test_find_claude_rejects_unsupported_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    command = tmp_path / "claude"
    command.touch(mode=0o755)
    monkeypatch.setattr("geer.onboarding.shutil.which", lambda name: str(command))
    monkeypatch.setattr(
        "geer.onboarding.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout="1.9.0 (Claude Code)\n",
        ),
    )

    assert find_claude().found is False


def test_find_t3_requires_supported_version_and_reports_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = tmp_path / "T3 Code.app"
    info = app / "Contents" / "Info.plist"
    info.parent.mkdir(parents=True)
    with info.open("wb") as stream:
        plistlib.dump({"CFBundleShortVersionString": "0.0.28"}, stream)
    settings = tmp_path / ".t3/userdata/settings.json"
    settings.parent.mkdir(parents=True)
    monkeypatch.setattr("geer.onboarding.T3_APP_CANDIDATES", (app,))
    monkeypatch.setattr("geer.onboarding.Path.home", lambda: tmp_path)
    monkeypatch.setattr("geer.onboarding._t3_running", lambda value: True)

    result = find_t3()

    assert result.found is True
    assert result.version == "0.0.28"
    assert result.settings_ready is True
    assert result.running is True


def test_find_t3_accepts_nightly_prerelease_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = tmp_path / "T3 Code (Nightly).app"
    info = app / "Contents" / "Info.plist"
    info.parent.mkdir(parents=True)
    with info.open("wb") as stream:
        plistlib.dump(
            {"CFBundleShortVersionString": "0.0.29-nightly.20260727.918"},
            stream,
        )
    (tmp_path / ".t3/userdata").mkdir(parents=True)
    monkeypatch.setattr("geer.onboarding.T3_APP_CANDIDATES", (app,))
    monkeypatch.setattr("geer.onboarding.Path.home", lambda: tmp_path)
    monkeypatch.setattr("geer.onboarding._t3_running", lambda value: False)

    result = find_t3()

    assert result.found is True
    assert result.version == "0.0.29-nightly.20260727.918"


def test_find_t3_prefers_the_running_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    alpha = tmp_path / "T3 Code (Alpha).app"
    nightly = tmp_path / "T3 Code (Nightly).app"
    for app, version in (
        (alpha, "0.0.28"),
        (nightly, "0.0.29-nightly.20260727.918"),
    ):
        info = app / "Contents/Info.plist"
        info.parent.mkdir(parents=True)
        with info.open("wb") as stream:
            plistlib.dump({"CFBundleShortVersionString": version}, stream)
    monkeypatch.setattr(
        "geer.onboarding.T3_APP_CANDIDATES",
        (alpha, nightly),
    )
    monkeypatch.setattr(
        "geer.onboarding._t3_running",
        lambda app: app == nightly,
    )

    result = find_t3()

    assert result.path == nightly
    assert result.version == "0.0.29-nightly.20260727.918"
    assert result.running is True


def test_t3_running_is_scoped_to_the_current_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.extend(command)
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr("geer.onboarding.subprocess.run", fake_run)
    monkeypatch.setattr("geer.onboarding.os.getuid", lambda: 502)

    assert _t3_running(Path("/Applications/T3 Code.app")) is False
    assert captured[:4] == ["pgrep", "-u", "502", "-f"]


def test_t3_guidance_distinguishes_install_from_initialization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_missing({"claude": Prerequisite(True), "t3": Prerequisite(False)})
    assert "T3 Code is not installed" in capsys.readouterr().out

    installed = Prerequisite(
        True,
        "0.0.29-nightly.20260727.918",
        tmp_path / "T3 Code (Nightly).app",
        settings_ready=False,
    )
    print_missing({"claude": Prerequisite(True), "t3": installed})
    assert "installed but has not been initialized" in capsys.readouterr().out

    monkeypatch.setattr(
        "geer.onboarding.prerequisite_snapshot",
        lambda: {"claude": Prerequisite(True), "t3": installed},
    )
    with pytest.raises(SetupError, match="installed but has not been initialized"):
        add_t3_integration(Workspace(tmp_path))


def test_confirm_uses_requested_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    assert confirm("Continue?", default_yes=True, assume_yes=False) is True
    assert confirm("Continue?", default_yes=False, assume_yes=False) is False


def test_confirm_requires_tty_without_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    with pytest.raises(SetupError, match="interactive consent"):
        confirm("Continue?", default_yes=False, assume_yes=False)


def test_setup_completion_requires_ready_state(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    assert setup_complete(workspace) is False
    workspace.state.mkdir(parents=True)
    workspace.setup_state.write_text('{"status":"ready"}\n')
    assert setup_complete(workspace) is True


def test_welcome_distinguishes_found_and_missing_tools(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("geer.onboarding.platform.system", lambda: "Darwin")
    monkeypatch.setattr("geer.onboarding.platform.machine", lambda: "arm64")
    monkeypatch.setattr("geer.onboarding.memory_bytes", lambda: 128 * 1024**3)
    print_welcome(
        {
            "claude": Prerequisite(False),
            "t3": Prerequisite(True, "0.0.28", Path("/Applications/T3 Code.app")),
        },
        {
            "display_name": "Geer Test Model",
            "download_human": "27.1 GiB",
            "runtime_download_human": "300.0 MiB",
            "runtime_installed_human": "1.0 GiB",
            "temporary_human": "2.0 GiB",
            "required_free_human": "30.0 GiB",
        },
    )

    output = capsys.readouterr().out
    assert "✓ 128 GB unified memory" in output
    assert "× Claude Code not found" in output
    assert "✓ T3 Code 0.0.28 found" in output


def test_setup_downloads_before_deferring_missing_integrations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = {"claude": Prerequisite(False), "t3": Prerequisite(False)}
    monkeypatch.setattr("geer.onboarding.platform.system", lambda: "Darwin")
    monkeypatch.setattr("geer.onboarding.platform.machine", lambda: "arm64")
    monkeypatch.setattr("geer.onboarding.memory_bytes", lambda: 48 * 1024**3)
    monkeypatch.setattr("geer.onboarding.prerequisite_snapshot", lambda: missing)
    monkeypatch.setattr(
        "geer.onboarding.model_plan",
        lambda workspace, **kwargs: {
            "display_name": "Geer Test Model",
            "download_human": "1.0 GiB",
            "runtime_download_human": "300.0 MiB",
            "runtime_installed_human": "1.0 GiB",
            "temporary_human": "2.0 GiB",
            "required_free_human": "2.0 GiB",
            "active_link": "~/.geer/models/active",
            "reusable_active_model": True,
        },
    )
    monkeypatch.setattr("geer.onboarding.bootstrap_runtime", lambda workspace: {})
    monkeypatch.setattr("geer.onboarding._probe_mlx", lambda workspace: None)
    monkeypatch.setattr(
        "geer.onboarding.install_model",
        lambda *args, **kwargs: {
            "installed": {"activation": {"reused": True}},
        },
    )
    monkeypatch.setattr("geer.onboarding.retrieval_canary", lambda workspace: {})
    monkeypatch.setattr(
        "geer.onboarding.start_server",
        lambda workspace: {
            "server": "online",
            "endpoint": "http://127.0.0.1:8765",
        },
    )
    monkeypatch.setattr("geer.onboarding.canary", lambda *args, **kwargs: {})

    result = setup(Workspace(tmp_path), assume_yes=True)

    output = capsys.readouterr().out
    assert output.index("T3 Code is not installed") < output.index("Preparing model")
    assert "Reused the verified active model" in output
    assert "Geer's local model is ready" in output
    assert "geer integration add t3" in output
    assert result["status"] == "ready"
    assert setup_complete(Workspace(tmp_path)) is True


def test_setup_upgrades_retained_model_without_continue_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class ReachedRuntime(Exception):
        pass

    monkeypatch.setattr("geer.onboarding.platform.system", lambda: "Darwin")
    monkeypatch.setattr("geer.onboarding.platform.machine", lambda: "arm64")
    monkeypatch.setattr("geer.onboarding.memory_bytes", lambda: 48 * 1024**3)
    monkeypatch.setattr(
        "geer.onboarding.prerequisite_snapshot",
        lambda: {"claude": Prerequisite(False), "t3": Prerequisite(False)},
    )
    monkeypatch.setattr(
        "geer.onboarding.model_plan",
        lambda workspace, **kwargs: {
            "display_name": "Geer Ornith 1.5 35B-A3B (4/8-bit MLX)",
            "download_human": "1.0 GiB",
            "runtime_download_human": "300.0 MiB",
            "runtime_installed_human": "1.0 GiB",
            "temporary_human": "2.0 GiB",
            "required_free_human": "2.0 GiB",
            "active_link": "~/.geer/models/active",
            "active_model_installed": True,
            "reusable_active_model": False,
        },
    )

    def reject_prompt(*args: object, **kwargs: object) -> bool:
        raise AssertionError("the retained-model upgrade must not prompt")

    def stop_after_upgrade_notice(workspace: Workspace) -> None:
        raise ReachedRuntime

    monkeypatch.setattr("geer.onboarding.confirm", reject_prompt)
    monkeypatch.setattr("geer.onboarding.bootstrap_runtime", stop_after_upgrade_notice)

    with pytest.raises(ReachedRuntime):
        setup(Workspace(tmp_path))

    assert "upgrade it to the latest Geer Ornith 1.5" in capsys.readouterr().out
