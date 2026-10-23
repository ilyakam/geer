from __future__ import annotations

from pathlib import Path

import pytest

from geer import __version__
from geer.assets import Workspace
from geer.cli import _confirm_t3_remove, _print_status, main, normalize_arguments, parser
from geer.onboarding import SetupError


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (["install"], ["setup"]),
        (["remove"], ["uninstall"]),
        (["delete"], ["uninstall"]),
        (["health"], ["status"]),
        (["fix"], ["doctor"]),
        (["usage"], ["stats"]),
        (["model", "show"], ["model", "list"]),
        (["model", "install", "ornith"], ["model", "add", "ornith"]),
        (["model", "setup", "ornith"], ["model", "add", "ornith"]),
        (["model", "set", "ornith"], ["model", "use", "ornith"]),
        (["model", "delete", "ornith"], ["model", "remove", "ornith"]),
        (["server", "up"], ["server", "start"]),
        (["server", "down"], ["server", "stop"]),
        (["server", "reboot"], ["server", "restart"]),
        (["server", "health"], ["server", "status"]),
        (["integration", "show"], ["integration", "list"]),
        (["integration", "install", "t3"], ["integration", "add", "t3"]),
        (["integration", "setup", "t3"], ["integration", "add", "t3"]),
        (["integration", "delete", "t3"], ["integration", "remove", "t3"]),
    ],
)
def test_hidden_aliases_normalize_to_canonical_commands(
    source: list[str],
    expected: list[str],
) -> None:
    assert normalize_arguments(source) == expected


def test_restart_is_a_public_server_command() -> None:
    options = parser().parse_args(["server", "restart"])

    assert options.command == "server"
    assert options.server_command == "restart"
    assert options.port is None


def test_uninstall_is_a_public_command() -> None:
    options = parser().parse_args(["uninstall", "--yes"])

    assert options.command == "uninstall"
    assert options.yes is True


def test_setup_accepts_hidden_graphical_frontend() -> None:
    options = parser().parse_args(["setup", "--frontend", "json-v2"])

    assert options.command == "setup"
    assert options.frontend == "json-v2"
    assert "--frontend" not in parser().format_help()


def test_old_native_protocol_is_rejected_explicitly() -> None:
    with pytest.raises(SystemExit, match="2"):
        parser().parse_args(["setup", "--frontend", "json-v1"])


@pytest.mark.parametrize("family", ["sage", "ornith"])
def test_setup_forwards_explicit_family_plan_and_yes(monkeypatch, tmp_path, family) -> None:
    calls = []
    monkeypatch.setattr("geer.cli.find_workspace", lambda: Workspace(tmp_path))
    monkeypatch.setattr("geer.cli.setup", lambda workspace, **kw: calls.append(kw))
    assert main(["setup", "--model", family, "--plan", "--yes"]) == 0
    assert calls == [{
        "family": family, "plan_only": True, "assume_yes": True, "high_performance": False,
    }]


@pytest.mark.parametrize("command", ["add", "install", "setup"])
@pytest.mark.parametrize("family", ["sage", "ornith"])
def test_model_add_downloads_without_activation(monkeypatch, tmp_path, command, family) -> None:
    calls = []
    monkeypatch.setattr("geer.cli.find_workspace", lambda: Workspace(tmp_path))
    monkeypatch.setattr("geer.cli.require_model_name", lambda value: None)
    monkeypatch.setattr("geer.cli.install_model", lambda workspace, **kw: calls.append(kw) or {})
    assert main(["model", command, family]) == 0
    assert calls == [{"family": family, "activate": False, "high_performance": False}]


def test_model_add_plan_never_installs(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("geer.cli.find_workspace", lambda: Workspace(tmp_path))
    monkeypatch.setattr("geer.cli.require_model_name", lambda value: None)
    monkeypatch.setattr("geer.cli.install_model", lambda *a, **kw: pytest.fail("install"))
    calls = []
    monkeypatch.setattr("geer.cli.model_plan", lambda workspace, **kw: calls.append(kw) or {})
    assert main(["model", "add", "ornith", "--plan"]) == 0
    assert calls == [{"family": "ornith"}]


def test_bare_command_repairs_without_forcing_saved_family_question(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr("geer.cli.find_workspace", lambda: Workspace(tmp_path))
    monkeypatch.setattr("geer.cli.setup_complete", lambda workspace: False)
    monkeypatch.setattr("geer.cli.setup", lambda workspace, **kw: calls.append(kw))
    assert main([]) == 0
    assert calls == [{"choose_family": False}]


def test_uninstall_is_last_in_public_help() -> None:
    help_text = parser().format_help()

    assert "{setup,status,doctor,stats,model,server,integration,uninstall}" in help_text
    assert help_text.index("integration         manage app integrations") < help_text.index(
        "uninstall           uninstall Geer from this Mac"
    )


def test_version_is_public(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="0"):
        parser().parse_args(["--version"])

    assert capsys.readouterr().out == f"geer {__version__}\n"


def test_t3_remove_requires_explicit_noninteractive_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("geer.cli.sys.stdin.isatty", lambda: False)

    with pytest.raises(SetupError, match="--yes.*--plan"):
        _confirm_t3_remove(assume_yes=False, plan_only=False)

    assert _confirm_t3_remove(assume_yes=True, plan_only=False) is True
    assert _confirm_t3_remove(assume_yes=False, plan_only=True) is True


@pytest.mark.parametrize("pi_ready", [True, False])
@pytest.mark.parametrize("t3_ready", [True, False])
def test_status_requires_pi_and_t3_even_with_an_online_engine(
    capsys: pytest.CaptureFixture[str],
    pi_ready: bool,
    t3_ready: bool,
) -> None:
    _print_status(
        {
            "server": "online",
            "model_id": "geer-local",
            "retrieval": {"provider": "semble"},
            "t3": {
                "ready": t3_ready,
                "version": "0.0.44",
                **({} if t3_ready else {"error": "T3 Code provider is disabled"}),
            },
            "pi": {"status": "ready", "version": "1.4.0"}
            if pi_ready
            else {
                "status": "unavailable",
                "error": "Pi executable is missing",
            },
        }
    )
    output = capsys.readouterr().out
    if pi_ready and t3_ready:
        assert "Geer is ready" in output
        assert "Harness    Pi 1.4.0" in output
        assert "install or repair" not in output
    else:
        assert "Geer is ready" not in output
        assert "Geer needs setup" in output
        if not pi_ready:
            assert "Pi executable is missing" in output
        if not t3_ready:
            assert "T3 Code provider is disabled" in output
        assert "geer setup" in output


def test_setup_cannot_skip_required_t3() -> None:
    with pytest.raises(SystemExit, match="2"):
        parser().parse_args(["setup", "--skip-t3"])


def test_family_removal_is_forwarded_without_cli_runtime_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = []
    monkeypatch.setattr("geer.cli.find_workspace", lambda: Workspace(tmp_path))
    monkeypatch.setattr("geer.cli.stop_server", lambda *a: pytest.fail("stopped server"))
    monkeypatch.setattr(
        "geer.cli.remove_model",
        lambda workspace, family: calls.append(family) or {"removed": family},
    )

    assert main(["model", "remove", "sage", "--yes"]) == 0
    assert calls == ["sage"]


@pytest.mark.parametrize("ready", [True, False])
def test_doctor_exit_code_reflects_required_readiness(monkeypatch, tmp_path, capsys, ready) -> None:
    monkeypatch.setattr("geer.cli.find_workspace", lambda: Workspace(tmp_path))
    monkeypatch.setattr("geer.cli.server_endpoint", lambda *args: "http://127.0.0.1:8765")
    monkeypatch.setattr("geer.cli.doctor", lambda *args: {"ready": ready})
    assert main(["doctor", "--json"]) == (0 if ready else 1)
    assert '"ready"' in capsys.readouterr().out
