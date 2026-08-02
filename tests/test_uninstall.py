from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from geer.assets import Workspace
from geer.uninstall import _remove_installation, directory_size, uninstall


def test_uninstall_declines_without_changing_anything(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = Workspace(tmp_path, tmp_path / ".geer")
    monkeypatch.setattr("geer.uninstall._server_running", lambda value: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompts: list[str] = []

    def decline(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", decline)
    monkeypatch.setattr(
        "geer.uninstall.stop_server",
        lambda value: pytest.fail("server should not be stopped"),
    )

    result = uninstall(workspace)

    assert result == {"status": "cancelled"}
    assert "Geer is currently running" in capsys.readouterr().out
    assert prompts == ["Continue? [y/N] "]


def test_uninstall_reports_progress_and_retained_size(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = Workspace(tmp_path, tmp_path / ".geer")
    monkeypatch.setattr("geer.uninstall._server_running", lambda value: True)
    monkeypatch.setattr(
        "geer.uninstall.stop_server",
        lambda value: {"server": "stopped", "stopped": True},
    )
    monkeypatch.setattr(
        "geer.uninstall.remove_t3",
        lambda path: {"removed": True},
    )
    monkeypatch.setattr("geer.uninstall.directory_size", lambda path: 27_123_456_789)
    removed: list[tuple[Path, ...]] = []
    monkeypatch.setattr("geer.uninstall._remove_installation", removed.append)

    result = uninstall(workspace, assume_yes=True)

    output = capsys.readouterr().out
    assert "✓ Stopped the Geer server" in output
    assert "✓ Removed Geer from T3 Code" in output
    assert "✓ Deleted the Geer CLI and application files" in output
    assert "Run `rm -rf ~/.geer`" in output
    assert "an additional 27.1 GB" in output
    assert removed
    assert result["status"] == "uninstalled"
    assert result["retained_bytes"] == 27_123_456_789


def test_directory_size_uses_disk_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / ".geer"
    home.mkdir()
    monkeypatch.setattr(
        "geer.uninstall.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout="2048\tignored\n",
        ),
    )

    assert directory_size(home) == 2 * 1024**2


def test_remove_installation_uses_only_existing_fixed_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cli = tmp_path / "geer"
    cli.touch()
    missing = tmp_path / "missing"
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="")

    monkeypatch.setattr("geer.uninstall.subprocess.run", run)

    _remove_installation((cli, missing))

    assert commands[0] == [
        "/usr/sbin/pkgutil",
        "--pkg-info",
        "com.ilyakam.geer",
    ]
    assert commands[1][0:2] == ["/usr/bin/osascript", "-e"]
    assert str(cli) in commands[1][2]
    assert str(missing) not in commands[1][2]
    assert "/usr/sbin/pkgutil --forget com.ilyakam.geer" not in commands[1][2]


def test_remove_installation_forgets_receipt_in_same_authorization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cli = tmp_path / "geer"
    cli.touch()
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr("geer.uninstall.subprocess.run", run)

    _remove_installation((cli,))

    assert commands[1][0:2] == ["/usr/bin/osascript", "-e"]
    assert str(cli) in commands[1][2]
    assert "/usr/sbin/pkgutil --forget com.ilyakam.geer" in commands[1][2]
    assert "with administrator privileges" in commands[1][2]
