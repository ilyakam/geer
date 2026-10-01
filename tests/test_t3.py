from __future__ import annotations

import json
import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from geer.assets import DEFAULT_MODEL_ID, AssetError, Workspace
from geer.t3 import (
    LEGACY_PROVIDER_ID,
    PROVIDER_ID,
    RETIRED_GEER_MODELS,
    T3_BUNDLE_ID,
    T3_TRANSPORT_BUILT_IN_MODELS,
    T3Application,
    _launcher,
    _t3_app_candidates,
    configure_t3,
    find_t3,
    remove_t3,
    running_t3_applications,
    t3_settings_path,
    t3_status,
)


@pytest.fixture(autouse=True)
def isolated_t3_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("geer.t3.running_t3_applications", lambda: [])
    for variable in ("GEER_PI_LAUNCHER", "GEER_HOME", "T3CODE_HOME"):
        monkeypatch.delenv(variable, raising=False)


def test_pi_migration_preserves_other_providers_and_moves_legacy_favorite(
    monkeypatch, tmp_path: Path,
) -> None:
    workspace = _workspace_with_launcher(tmp_path)
    monkeypatch.setattr(
        "geer.t3.verify_assets", lambda workspace: {"model_id": "local-id"}
    )
    settings = tmp_path / "userdata/settings.json"
    settings.parent.mkdir()
    original_other = {"driver": "claudeAgent", "config": {"binaryPath": "claude"}}
    settings.write_text(json.dumps({"providerInstances": {
        "claudeAgent": original_other,
        LEGACY_PROVIDER_ID: {"driver": "claudeAgent", "displayName": "Geer"},
    }}))
    client = settings.with_name("client-settings.json")
    client.write_text(json.dumps({
        "favorites": [
            {"provider": LEGACY_PROVIDER_ID, "model": "old-geer"},
            {"provider": "claudeAgent", "model": "other-model"},
        ],
        "providerModelPreferences": {LEGACY_PROVIDER_ID: {"hiddenModels": []}},
    }))

    result = configure_t3(workspace, settings, tmp_path / "backups")

    providers = json.loads(settings.read_text())["providerInstances"]
    assert providers["claudeAgent"] == original_other
    assert LEGACY_PROVIDER_ID not in providers
    assert providers[PROVIDER_ID]["driver"] == "grok"
    assert providers[PROVIDER_ID]["config"]["customModels"] == []
    assert json.loads(client.read_text())["favorites"] == [
        {"provider": PROVIDER_ID, "model": "local-id"},
        {"provider": "claudeAgent", "model": "other-model"},
    ]
    assert result["legacy_provider_removed"] is True


def test_initial_pi_validation_can_retain_legacy_geer(monkeypatch, tmp_path: Path) -> None:
    workspace = _workspace_with_launcher(tmp_path)
    monkeypatch.setattr("geer.t3.verify_assets", lambda workspace: {"model_id": "local-id"})
    settings = tmp_path / "userdata/settings.json"
    settings.parent.mkdir()
    legacy = {"driver": "claudeAgent", "displayName": "Geer"}
    settings.write_text(json.dumps({"providerInstances": {LEGACY_PROVIDER_ID: legacy}}))

    result = configure_t3(
        workspace, settings, tmp_path / "backups", retire_legacy=False
    )

    assert json.loads(settings.read_text())["providerInstances"][LEGACY_PROVIDER_ID] == legacy
    assert result["legacy_provider_removed"] is False


def _workspace_with_launcher(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path / "geer")
    launcher = workspace.root / "bin" / "geer-pi"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    return workspace


def test_installed_t3_uses_dedicated_pi_acp_launcher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "geer")
    launcher = tmp_path / "geer-pi"
    launcher.touch(mode=0o755)
    monkeypatch.setenv("GEER_PI_LAUNCHER", str(launcher))
    monkeypatch.setenv("GEER_CLI", str(tmp_path / "geer"))

    assert _launcher(workspace) == (launcher, "")


def test_configure_t3_is_additive_and_creates_private_backup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace_with_launcher(tmp_path)
    manifest = {"quantization": {"bits": 6}}
    monkeypatch.setattr("geer.t3.verify_assets", lambda value: manifest)
    launcher = workspace.root / "bin" / "geer-pi"
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
    assert geer["driver"] == "grok"
    assert geer["displayName"] == "Geer"
    assert geer["config"]["binaryPath"] == str(launcher)
    assert geer["config"]["customModels"] == []
    client_settings = json.loads(client_settings_path.read_text())
    assert settings["providerModelPreferences"] == {
        "legacy": {"hiddenModels": ["legacy-model"]}
    }
    assert client_settings["providerModelPreferences"]["codex"]["hiddenModels"] == [
        "old-codex"
    ]
    assert client_settings["providerModelPreferences"][PROVIDER_ID] == {
        "hiddenModels": [
            model for model in (*T3_TRANSPORT_BUILT_IN_MODELS, *RETIRED_GEER_MODELS)
            if model != DEFAULT_MODEL_ID
        ],
        "modelOrder": [DEFAULT_MODEL_ID],
    }
    assert client_settings["favorites"] == [
        {"provider": "codex", "model": "gpt-5.6-sol"},
        {"provider": PROVIDER_ID, "model": DEFAULT_MODEL_ID},
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
    assert result["t3_compatibility_version"] == ">=0.0.43"
    assert result["t3_tested_version"] == "0.0.44"
    assert result["changed"] is True


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
    assert result["changed"] is True
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
    assert settings["defaultModelSelection"] == {
        "instanceId": PROVIDER_ID, "model": DEFAULT_MODEL_ID,
    }
    assert settings["textGenerationModelSelection"] == settings["defaultModelSelection"]
    assert settings["enableProviderUpdateChecks"] is False
    assert settings["providers"] == {
        "codex": {"enabled": False}, "claudeAgent": {"enabled": False},
    }
    assert client_settings["favorites"] == [{"provider": PROVIDER_ID, "model": DEFAULT_MODEL_ID}]
    assert "onboardingCompleted" not in settings and "onboardingCompleted" not in client_settings
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
                    PROVIDER_ID: {"driver": "grok"},
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
    )["providerInstances"][PROVIDER_ID] == {"driver": "grok"}

    second = remove_t3(settings_path, tmp_path / "backups")
    assert second["removed"] is False
    assert second["applied"] is False


def test_remove_t3_cleans_orphaned_geer_favorites_only(tmp_path: Path) -> None:
    settings_path = tmp_path / "userdata" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text('{"providerInstances": {"codex": {}}}\n')
    client_path = settings_path.with_name("client-settings.json")
    other = {"provider": "claudeAgent", "model": "other-model"}
    client_path.write_text(json.dumps({"favorites": [
        {"provider": PROVIDER_ID, "model": "geer-local"},
        other,
        {"provider": LEGACY_PROVIDER_ID, "model": "old-geer"},
    ]}))

    preview = remove_t3(settings_path, tmp_path / "backups", dry_run=True)
    assert preview["removed"] is True
    assert preview["favorites_removed"] == 2
    assert len(json.loads(client_path.read_text())["favorites"]) == 3

    result = remove_t3(settings_path, tmp_path / "backups")
    assert result["applied"] is True
    assert json.loads(client_path.read_text())["favorites"] == [other]
    assert json.loads(settings_path.read_text())["providerInstances"] == {"codex": {}}


def _t3_app(tmp_path: Path, name: str, version: str = "0.0.44") -> Path:
    app = tmp_path / f"{name}.app"
    executable = app / "Contents/MacOS" / name
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    with (app / "Contents/Info.plist").open("wb") as stream:
        plistlib.dump({
            "CFBundleIdentifier": T3_BUNDLE_ID,
            "CFBundleExecutable": name,
            "CFBundleShortVersionString": version,
        }, stream)
    return app.resolve()


def test_t3_settings_and_candidates_follow_current_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert t3_settings_path() == tmp_path / ".t3/userdata/settings.json"
    assert tmp_path / "Applications/T3 Code.app" in _t3_app_candidates()
    alternate = tmp_path / "isolated-t3"
    monkeypatch.setenv("T3CODE_HOME", str(alternate))
    assert t3_settings_path() == alternate / "userdata/settings.json"


def test_find_t3_prefers_running_then_highest_compatible_version(monkeypatch, tmp_path) -> None:
    stable = _t3_app(tmp_path, "Stable", "0.0.44")
    nightly = _t3_app(tmp_path, "Nightly", "0.0.44-nightly.20260930.1000")
    older = _t3_app(tmp_path, "Older", "0.0.43-nightly.20260923.2150")
    unsupported = _t3_app(tmp_path, "Old", "0.0.42")
    monkeypatch.setattr("geer.t3._t3_app_candidates", lambda: (stable, nightly, unsupported))
    monkeypatch.setattr("geer.t3.running_t3_applications", lambda: [older])
    assert find_t3() == T3Application(True, "0.0.43-nightly.20260923.2150", older, True)
    monkeypatch.setattr("geer.t3.running_t3_applications", lambda: [])
    assert find_t3() == T3Application(True, "0.0.44", stable)


def test_find_t3_identifies_an_old_app_for_in_place_upgrade(monkeypatch, tmp_path) -> None:
    old = _t3_app(tmp_path, "T3 Code (Alpha)", "0.0.28")
    monkeypatch.setattr("geer.t3._t3_app_candidates", lambda: (old,))
    monkeypatch.setattr("geer.t3.running_t3_applications", lambda: [old])
    assert find_t3() == T3Application(False, "0.0.28", old, True)


@pytest.mark.parametrize("defect", ["bundle_id", "missing_executable", "not_executable", "version"])
def test_find_t3_rejects_incomplete_or_unrelated_bundles(monkeypatch, tmp_path, defect) -> None:
    app = _t3_app(tmp_path, "T3")
    info = app / "Contents/Info.plist"
    metadata = plistlib.loads(info.read_bytes())
    if defect == "bundle_id":
        metadata["CFBundleIdentifier"] = "other.application"
    elif defect == "missing_executable":
        (app / "Contents/MacOS/T3").unlink()
    elif defect == "not_executable":
        (app / "Contents/MacOS/T3").chmod(0o644)
    else:
        metadata["CFBundleShortVersionString"] = "unversioned"
    info.write_bytes(plistlib.dumps(metadata))
    monkeypatch.setattr("geer.t3._t3_app_candidates", lambda: (app,))
    assert find_t3().found is False


def test_running_t3_inventory_includes_old_custom_bundles_and_helpers(
    monkeypatch, tmp_path,
) -> None:
    old = _t3_app(tmp_path / "Downloads", "Renamed T3", "0.0.1")
    active = _t3_app(tmp_path / "other", "T3 Active")
    other_user = _t3_app(tmp_path, "Another account")
    unrelated = _t3_app(tmp_path, "Unrelated")
    info = unrelated / "Contents/Info.plist"
    metadata = plistlib.loads(info.read_bytes())
    metadata["CFBundleIdentifier"] = "unrelated.app"
    info.write_bytes(plistlib.dumps(metadata))
    helper = active / "Contents/Frameworks/Helper.app/Contents/MacOS/Helper"
    helper.parent.mkdir(parents=True)
    helper.touch(mode=0o755)
    uid = os.getuid()
    inventory = "\n".join([
        f"{uid} 11 {old}/Contents/MacOS/Renamed T3",
        f"{uid} 12 {active}/Contents/MacOS/T3 Active",
        f"{uid} 13 {helper}",
        f"{uid + 1} 14 {other_user}/Contents/MacOS/Another account",
        f"{uid} 15 {unrelated}/Contents/MacOS/Unrelated",
        f"{uid} 16 /usr/bin/python",
    ])
    commands: list[list[str]] = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, inventory, "")

    monkeypatch.setattr("geer.t3.subprocess.run", run)
    assert running_t3_applications() == sorted([old, active])
    assert commands == [["ps", "-axww", "-o", "uid=,pid=,comm="]]


@pytest.mark.parametrize("failure", ["exit", "timeout", "missing", "malformed"])
def test_running_t3_inventory_errors_do_not_assume_stopped(monkeypatch, failure) -> None:
    def run(command, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 5)
        if failure == "missing":
            raise OSError("ps unavailable")
        if failure == "malformed":
            return subprocess.CompletedProcess(command, 0, "truncated process inventory", "")
        return subprocess.CompletedProcess(command, 1, "", "permission denied")

    monkeypatch.setattr("geer.t3.subprocess.run", run)
    with pytest.raises(AssetError, match="cannot inspect running T3"):
        running_t3_applications()


def test_configure_t3_preserves_existing_selections_and_model_preferences(
    monkeypatch, tmp_path,
) -> None:
    workspace = _workspace_with_launcher(tmp_path)
    monkeypatch.setattr("geer.t3.verify_assets", lambda value: {"model_id": "real-model"})
    settings = tmp_path / "userdata/settings.json"
    settings.parent.mkdir()
    existing = {
        "defaultModelSelection": {"instanceId": "claudeAgent", "model": "user-choice"},
        "textGenerationModelSelection": {"instanceId": "codex", "model": "user-title-choice"},
        "enableProviderUpdateChecks": True,
        "providers": {"codex": {"enabled": True}},
    }
    settings.write_text(json.dumps(existing))
    client = settings.with_name("client-settings.json")
    favorite = {"provider": "codex", "model": "user-favorite"}
    client.write_text(json.dumps({
        "favorites": [favorite],
        "providerModelPreferences": {PROVIDER_ID: {
            "hiddenModels": ["real-model", "user-hidden"],
            "modelOrder": ["user-model", "real-model"],
            "customPreference": True,
        }},
    }))
    configure_t3(workspace, settings, tmp_path / "backups")
    configured = json.loads(settings.read_text())
    assert all(configured[key] == value for key, value in existing.items())
    updated_client = json.loads(client.read_text())
    assert updated_client["favorites"] == [favorite]
    preference = updated_client["providerModelPreferences"][PROVIDER_ID]
    assert "real-model" not in preference["hiddenModels"]
    assert "user-hidden" in preference["hiddenModels"]
    assert preference["modelOrder"] == ["real-model", "user-model"]
    assert preference["customPreference"] is True


def test_configure_t3_migrates_legacy_and_project_model_selections(monkeypatch, tmp_path) -> None:
    workspace = _workspace_with_launcher(tmp_path)
    monkeypatch.setattr("geer.t3.verify_assets", lambda value: {"model_id": "real-model"})
    settings = tmp_path / "userdata/settings.json"
    settings.parent.mkdir()
    legacy = {
        "provider": LEGACY_PROVIDER_ID, "model": "old-geer",
        "options": [{"id": "reasoningEffort", "value": "high"}],
    }
    settings.write_text(json.dumps({
        "defaultModelSelection": legacy,
        "textGenerationModelSelection": {
            "instanceId": PROVIDER_ID, "model": RETIRED_GEER_MODELS[0],
        },
        "projectSettingsOverrides": {
            "project": {"defaultModelSelection": legacy, "defaultAutoPull": True},
        },
    }))
    result = configure_t3(workspace, settings, tmp_path / "backups")
    updated = json.loads(settings.read_text())
    selection = {"instanceId": PROVIDER_ID, "model": "real-model"}
    assert updated["defaultModelSelection"] == selection
    assert updated["textGenerationModelSelection"] == {
        "instanceId": PROVIDER_ID, "model": "real-model",
    }
    assert updated["projectSettingsOverrides"]["project"] == {
        "defaultModelSelection": selection, "defaultAutoPull": True,
    }
    assert result["selections_migrated"] == 3


def test_configure_t3_is_idempotent_even_with_active_application(monkeypatch, tmp_path) -> None:
    workspace = _workspace_with_launcher(tmp_path)
    monkeypatch.setattr("geer.t3.verify_assets", lambda value: {"model_id": "real-model"})
    settings = tmp_path / "userdata/settings.json"
    backup_base = tmp_path / "backups"
    configure_t3(workspace, settings, backup_base)
    # Native T3 folds the legacy nested flag into the provider-level flag.
    document = json.loads(settings.read_text())
    provider = document["providerInstances"][PROVIDER_ID]
    provider["enabled"] = provider["config"].pop("enabled", provider["enabled"])
    settings.write_text(json.dumps(document))
    client = settings.with_name("client-settings.json")
    cache = tmp_path / "caches" / f"{PROVIDER_ID}.json"
    cache.parent.mkdir()
    cache.write_text('{"models":["verified-model"]}\n')
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (settings, client, cache)
    }
    backups = list(backup_base.iterdir())

    def running():
        raise AssertionError("unchanged settings must not require stopping or probing T3")

    monkeypatch.setattr("geer.t3.running_t3_applications", running)
    for dry_run in (True, False):
        result = configure_t3(workspace, settings, backup_base, dry_run=dry_run)
        assert result["changed"] is False
        assert result["applied"] is False
        assert result["backups"] == {}
        assert result["cache_invalidated"] is False
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}
    assert list(backup_base.iterdir()) == backups


def test_configure_t3_repairs_legacy_disabled_flags_without_reintroducing_them(
    monkeypatch, tmp_path,
) -> None:
    workspace = _workspace_with_launcher(tmp_path)
    monkeypatch.setattr("geer.t3.verify_assets", lambda value: {"model_id": "real-model"})
    settings = tmp_path / "userdata/settings.json"
    configure_t3(workspace, settings, tmp_path / "backups")
    document = json.loads(settings.read_text())
    provider = document["providerInstances"][PROVIDER_ID]
    provider["enabled"] = False
    provider["config"]["enabled"] = False
    settings.write_text(json.dumps(document))

    result = configure_t3(workspace, settings, tmp_path / "backups")
    repaired = json.loads(settings.read_text())["providerInstances"][PROVIDER_ID]
    assert result["changed"] is True
    assert repaired["enabled"] is True
    assert "enabled" not in repaired["config"]


def test_configure_t3_refuses_changed_settings_while_application_active(
    monkeypatch, tmp_path,
) -> None:
    workspace = _workspace_with_launcher(tmp_path)
    monkeypatch.setattr("geer.t3.verify_assets", lambda value: {"model_id": "real-model"})
    app = _t3_app(tmp_path, "T3 Active")
    monkeypatch.setattr("geer.t3.running_t3_applications", lambda: [app])
    settings = tmp_path / "userdata/settings.json"
    with pytest.raises(AssetError, match="quit T3 Code before updating"):
        configure_t3(workspace, settings, tmp_path / "backups")
    assert not settings.exists() and not (tmp_path / "backups").exists()


def _ready_t3_profile(monkeypatch, tmp_path):
    workspace = _workspace_with_launcher(tmp_path)
    monkeypatch.setattr("geer.t3.verify_assets", lambda value: {"model_id": "real-model"})
    settings = tmp_path / "userdata/settings.json"
    configure_t3(workspace, settings, tmp_path / "backups")
    app = _t3_app(tmp_path, "T3 Ready")
    monkeypatch.setattr("geer.t3.find_t3", lambda: T3Application(True, "0.0.44", app, True))
    return workspace, settings, app


def test_t3_status_reads_readiness_without_writes_or_inference(monkeypatch, tmp_path) -> None:
    workspace, settings, app = _ready_t3_profile(monkeypatch, tmp_path)
    client = settings.with_name("client-settings.json")
    documents = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (settings, client)}

    def forbidden(*args, **kwargs):
        raise AssertionError("readiness must not configure or bootstrap")

    monkeypatch.setattr("geer.t3.configure_t3", forbidden)
    status = t3_status(workspace, settings)
    assert status["available"] is True and status["configured"] is True and status["ready"] is True
    assert status["application_path"] == str(app)
    assert status["version"] == "0.0.44" and status["running"] is True
    assert status["model_id"] == "real-model" and status["error"] is None
    assert documents == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in documents}


@pytest.mark.parametrize("defect", [
    "missing_app", "disabled", "config_disabled", "enabled_missing",
    "driver", "binary", "launcher", "hidden", "order",
    "legacy_provider", "legacy_favorite", "legacy_selection", "options", "malformed",
])
def test_t3_status_reports_incomplete_required_integration(monkeypatch, tmp_path, defect) -> None:
    workspace, settings, _ = _ready_t3_profile(monkeypatch, tmp_path)
    document = json.loads(settings.read_text())
    client = settings.with_name("client-settings.json")
    client_document = json.loads(client.read_text())
    provider = document["providerInstances"][PROVIDER_ID]
    preference = client_document["providerModelPreferences"][PROVIDER_ID]
    if defect == "missing_app":
        monkeypatch.setattr("geer.t3.find_t3", lambda: T3Application(False))
    elif defect == "disabled":
        provider["enabled"] = False
    elif defect == "config_disabled":
        provider["config"]["enabled"] = False
    elif defect == "enabled_missing":
        provider.pop("enabled", None)
        provider["config"].pop("enabled", None)
    elif defect == "driver":
        provider["driver"] = "claudeAgent"
    elif defect == "binary":
        provider["config"]["binaryPath"] = "/wrong/geer-pi"
    elif defect == "launcher":
        (workspace.root / "bin/geer-pi").chmod(0o644)
    elif defect == "hidden":
        preference["hiddenModels"].append("real-model")
    elif defect == "order":
        preference["modelOrder"] = []
    elif defect == "legacy_provider":
        document["providerInstances"][LEGACY_PROVIDER_ID] = {"driver": "claudeAgent"}
    elif defect == "legacy_favorite":
        client_document["favorites"].append({"provider": LEGACY_PROVIDER_ID, "model": "old"})
    elif defect == "legacy_selection":
        document["textGenerationModelSelection"] = {
            "instanceId": LEGACY_PROVIDER_ID, "model": "old",
        }
    elif defect == "options":
        document["textGenerationModelSelection"]["options"] = [
            {"id": "reasoningEffort", "value": "high"},
        ]
    settings.write_text(json.dumps(document) if defect != "malformed" else "[")
    client.write_text(json.dumps(client_document))
    status = t3_status(workspace, settings)
    assert status["configured"] is False and status["ready"] is False
    assert status["error"]


def test_t3_status_accepts_existing_cloud_defaults_and_reports_process_probe_failure(
    monkeypatch, tmp_path,
) -> None:
    workspace, settings, _ = _ready_t3_profile(monkeypatch, tmp_path)
    document = json.loads(settings.read_text())
    document["defaultModelSelection"] = None
    document.pop("textGenerationModelSelection")
    document.pop("enableProviderUpdateChecks")
    document.pop("providers")
    settings.write_text(json.dumps(document))
    assert t3_status(workspace, settings)["ready"] is True

    def fail():
        raise AssetError("process inventory unavailable")

    monkeypatch.setattr("geer.t3.find_t3", fail)
    assert t3_status(workspace, settings)["error"] == "process inventory unavailable"


def test_remove_t3_clears_only_geer_model_selections(monkeypatch, tmp_path) -> None:
    workspace, settings, _ = _ready_t3_profile(monkeypatch, tmp_path)
    document = json.loads(settings.read_text())
    other = {"instanceId": "codex", "model": "user-choice"}
    document["sourceControlWriterModelSelection"] = other
    document["projectSettingsOverrides"] = {"project": {
        "defaultModelSelection": {"instanceId": LEGACY_PROVIDER_ID, "model": "old"},
        "sourceControlWriterModelSelection": other,
        "defaultAutoPull": True,
    }}
    settings.write_text(json.dumps(document))

    result = remove_t3(settings, tmp_path / "removal-backups")
    updated = json.loads(settings.read_text())
    assert result["selections_removed"] == 3
    assert "defaultModelSelection" not in updated
    assert "textGenerationModelSelection" not in updated
    assert updated["sourceControlWriterModelSelection"] == other
    assert updated["projectSettingsOverrides"]["project"] == {
        "sourceControlWriterModelSelection": other, "defaultAutoPull": True,
    }
    assert updated["enableProviderUpdateChecks"] is False
    assert configure_t3(workspace, settings, tmp_path / "backups", dry_run=True)["changed"] is True


def test_remove_t3_missing_profiles_is_no_op_and_skips_process_probe(monkeypatch, tmp_path) -> None:
    def forbidden():
        raise AssertionError("missing/unchanged profiles must not probe or stop T3")

    monkeypatch.setattr("geer.t3.running_t3_applications", forbidden)
    settings = tmp_path / "userdata/settings.json"
    for dry_run in (True, False):
        result = remove_t3(settings, tmp_path / "backups", dry_run=dry_run)
        assert result["removed"] is False and result["applied"] is False
    assert not settings.parent.exists() and not (tmp_path / "backups").exists()


def test_remove_t3_refuses_active_changed_profile_and_keeps_malformed_files(
    monkeypatch, tmp_path,
) -> None:
    _, settings, app = _ready_t3_profile(monkeypatch, tmp_path)
    original = settings.read_bytes()
    monkeypatch.setattr("geer.t3.running_t3_applications", lambda: [app])
    assert remove_t3(settings, tmp_path / "removal-backups", dry_run=True)["removed"] is True
    with pytest.raises(AssetError, match="quit T3 Code before removing"):
        remove_t3(settings, tmp_path / "removal-backups")
    assert settings.read_bytes() == original and not (tmp_path / "removal-backups").exists()
    settings.write_text("[")
    with pytest.raises(AssetError, match="cannot read T3 settings"):
        remove_t3(settings, tmp_path / "removal-backups")
    assert settings.read_text() == "["


def test_remove_t3_does_not_create_missing_client_settings(tmp_path) -> None:
    settings = tmp_path / "userdata/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"providerInstances": {PROVIDER_ID: {"driver": "grok"}}}))
    assert remove_t3(settings, tmp_path / "backups")["removed"] is True
    assert not settings.with_name("client-settings.json").exists()
