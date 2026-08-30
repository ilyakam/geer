from __future__ import annotations

import json
from pathlib import Path

import pytest

from geer.assets import AssetError, Workspace, model_alias
from geer.t3 import (
    PROVIDER_ID,
    RETIRED_GEER_MODELS,
    T3_CLAUDE_BUILT_IN_MODELS,
    _launcher,
    configure_t3,
    remove_t3,
)


def _workspace_with_launcher(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path / "geer")
    launcher = workspace.root / "bin" / "geer-claude"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    return workspace


def test_installed_t3_uses_dedicated_claude_compatible_launcher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "geer")
    launcher = tmp_path / "geer-claude"
    launcher.touch(mode=0o755)
    monkeypatch.setenv("GEER_CLAUDE_LAUNCHER", str(launcher))
    monkeypatch.setenv("GEER_CLI", str(tmp_path / "geer"))

    assert _launcher(workspace) == (launcher, "")


def test_configure_t3_is_additive_and_creates_private_backup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace_with_launcher(tmp_path)
    manifest = {"quantization": {"bits": 6}}
    monkeypatch.setattr("geer.t3.verify_assets", lambda value: manifest)
    alias = model_alias(manifest)
    launcher = workspace.root / "bin" / "geer-claude"
    settings_path = tmp_path / "userdata" / "settings.json"
    settings_path.parent.mkdir()
    client_settings_path = settings_path.with_name("client-settings.json")
    cache_path = tmp_path / "caches" / f"{PROVIDER_ID}.json"
    cache_path.parent.mkdir()
    cache_path.write_text('{"models": ["stale-model"]}\n')
    settings_path.write_text(
        json.dumps(
            {
                "providerInstances": {
                    "codex": {"driver": "codex", "enabled": True},
                },
                "providerModelPreferences": {
                    "legacy": {"hiddenModels": ["legacy-model"]},
                    PROVIDER_ID: {"hiddenModels": ["stale-geer-model"]},
                },
            }
        )
    )
    client_settings_path.write_text(
        json.dumps(
            {
                "favorites": [
                    {"provider": "codex", "model": "gpt-5.6-sol"},
                    {"provider": PROVIDER_ID, "model": RETIRED_GEER_MODELS[0]},
                    {"provider": PROVIDER_ID, "model": RETIRED_GEER_MODELS[3]},
                ],
                "providerModelPreferences": {
                    "codex": {
                        "hiddenModels": ["old-codex"],
                        "modelOrder": [],
                    }
                },
            }
        )
    )

    result = configure_t3(workspace, settings_path, tmp_path / "backups")
    settings = json.loads(settings_path.read_text())

    assert settings["providerInstances"]["codex"]["enabled"] is True
    geer = settings["providerInstances"][PROVIDER_ID]
    assert geer["driver"] == "claudeAgent"
    assert geer["displayName"] == "Geer"
    assert geer["config"]["binaryPath"] == str(launcher)
    assert geer["config"]["customModels"] == [alias]
    client_settings = json.loads(client_settings_path.read_text())
    assert settings["providerModelPreferences"] == {
        "legacy": {"hiddenModels": ["legacy-model"]}
    }
    assert client_settings["providerModelPreferences"]["codex"]["hiddenModels"] == [
        "old-codex"
    ]
    assert client_settings["providerModelPreferences"][PROVIDER_ID] == {
        "hiddenModels": [*T3_CLAUDE_BUILT_IN_MODELS, *RETIRED_GEER_MODELS],
        "modelOrder": [alias],
    }
    assert client_settings["favorites"] == [
        {"provider": "codex", "model": "gpt-5.6-sol"},
        {"provider": PROVIDER_ID, "model": alias},
    ]
    assert result["favorites_migrated"] == 2
    settings_backup = Path(result["backups"]["settings.json"])
    client_backup = Path(result["backups"]["client-settings.json"])
    cache_backup = Path(result["backups"][f"{PROVIDER_ID}.json"])
    assert json.loads(settings_backup.read_text())["providerInstances"]["codex"][
        "enabled"
    ] is True
    assert json.loads(client_backup.read_text())["providerModelPreferences"][
        "codex"
    ]["hiddenModels"] == ["old-codex"]
    assert settings_backup.stat().st_mode & 0o777 == 0o600
    assert client_backup.stat().st_mode & 0o777 == 0o600
    assert json.loads(cache_backup.read_text())["models"] == ["stale-model"]
    assert not cache_path.exists()
    assert result["cache_invalidated"] is True
    assert result["applied"] is True
    assert result["t3_compatibility_version"] == ">=0.0.28"
    assert result["t3_tested_version"] == "0.0.28"


def test_configure_t3_dry_run_does_not_write_or_back_up(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace_with_launcher(tmp_path)
    monkeypatch.setattr(
        "geer.t3.verify_assets",
        lambda value: {"quantization": {"bits": 6}},
    )
    settings_path = tmp_path / "userdata" / "settings.json"
    settings_path.parent.mkdir()
    client_settings_path = settings_path.with_name("client-settings.json")
    original = '{"providerInstances": {}}\n'
    original_client = '{"providerModelPreferences": {}}\n'
    cache_path = tmp_path / "caches" / f"{PROVIDER_ID}.json"
    cache_path.parent.mkdir(exist_ok=True)
    cache_path.write_text('{"models": ["stale-model"]}\n')
    settings_path.write_text(original)
    client_settings_path.write_text(original_client)

    result = configure_t3(
        workspace,
        settings_path,
        tmp_path / "backups",
        dry_run=True,
    )

    assert settings_path.read_text() == original
    assert client_settings_path.read_text() == original_client
    assert result["applied"] is False
    assert result["backups"] == {}
    assert result["cache_invalidated"] is False
    assert cache_path.is_file()
    assert not (tmp_path / "backups").exists()


def test_configure_t3_initializes_optional_settings_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace_with_launcher(tmp_path)
    monkeypatch.setattr(
        "geer.t3.verify_assets",
        lambda value: {"quantization": {"bits": 6}},
    )
    settings_path = tmp_path / "userdata" / "settings.json"
    settings_path.parent.mkdir()

    result = configure_t3(workspace, settings_path, tmp_path / "backups")

    settings = json.loads(settings_path.read_text())
    client_settings = json.loads(
        settings_path.with_name("client-settings.json").read_text()
    )
    assert settings["providerInstances"][PROVIDER_ID]["displayName"] == "Geer"
    assert PROVIDER_ID in client_settings["providerModelPreferences"]
    assert result["backups"] == {}
    assert settings_path.stat().st_mode & 0o777 == 0o600
    assert settings_path.with_name("client-settings.json").stat().st_mode & 0o777 == 0o600


def test_configure_t3_rolls_back_new_documents_after_write_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace_with_launcher(tmp_path)
    monkeypatch.setattr(
        "geer.t3.verify_assets",
        lambda value: {"quantization": {"bits": 6}},
    )
    settings_path = tmp_path / "userdata" / "settings.json"
    settings_path.parent.mkdir()
    real_replace = __import__("os").replace
    replacements = 0

    def fail_second_replace(source, destination):
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise OSError("interrupted write")
        real_replace(source, destination)

    monkeypatch.setattr("geer.t3.os.replace", fail_second_replace)

    with pytest.raises(AssetError, match="cannot update T3 settings"):
        configure_t3(workspace, settings_path, tmp_path / "backups")

    client_path = settings_path.with_name("client-settings.json")
    assert not settings_path.exists()
    assert not client_path.exists()
    assert not settings_path.with_suffix(".json.geer.tmp").exists()
    assert not client_path.with_suffix(".json.geer.tmp").exists()


def test_remove_t3_preserves_unrelated_settings_and_is_idempotent(tmp_path: Path) -> None:
    settings_path = tmp_path / "userdata" / "settings.json"
    settings_path.parent.mkdir()
    client_settings_path = settings_path.with_name("client-settings.json")
    settings_path.write_text(
        json.dumps(
            {
                "providerInstances": {
                    "codex": {"driver": "codex"},
                    PROVIDER_ID: {"driver": "claudeAgent"},
                },
                "theme": "system",
            }
        )
    )
    client_settings_path.write_text(
        json.dumps(
            {
                "providerModelPreferences": {
                    "codex": {"hiddenModels": []},
                    PROVIDER_ID: {"hiddenModels": ["claude-opus-4-6"]},
                },
                "wordWrap": True,
            }
        )
    )

    result = remove_t3(settings_path, tmp_path / "backups")
    settings = json.loads(settings_path.read_text())
    client_settings = json.loads(client_settings_path.read_text())

    assert result["applied"] is True
    assert settings["providerInstances"] == {"codex": {"driver": "codex"}}
    assert client_settings["providerModelPreferences"] == {
        "codex": {"hiddenModels": []}
    }
    assert settings["theme"] == "system"
    assert client_settings["wordWrap"] is True
    assert json.loads(
        Path(result["backups"]["settings.json"]).read_text()
    )["providerInstances"][PROVIDER_ID] == {"driver": "claudeAgent"}

    second = remove_t3(settings_path, tmp_path / "backups")
    assert second["removed"] is False
    assert second["applied"] is False
