from __future__ import annotations

import pytest

from geer import __version__
from geer.cli import _confirm_t3_remove, normalize_arguments, parser
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
    options = parser().parse_args(["setup", "--frontend", "json-v1"])

    assert options.command == "setup"
    assert options.frontend == "json-v1"
    assert "--frontend" not in parser().format_help()


def test_uninstall_is_last_in_public_help() -> None:
    help_text = parser().format_help()

    assert (
        "{setup,status,doctor,stats,model,server,integration,uninstall}"
        in help_text
    )
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
