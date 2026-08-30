from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .assets import AssetError, Workspace, model_alias, verify_assets

PROVIDER_ID = "claudeGeer"
T3_MINIMUM_VERSION = "0.0.28"
T3_TESTED_VERSION = "0.0.28"
T3_CLAUDE_BUILT_IN_MODELS = (
    "claude-fable-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
)
RETIRED_GEER_MODELS = (
    "Geer Ornith 1.0 35B-A3B (6-bit MLX)",
    "Geer Ornith 1.0 35B-A3B (4-bit MLX)",
    "Geer Ornith 1.0 35B-A3B (4/8-bit MLX)",
    "Geer Qwen 3.8 27B (Low Reasoning)",
    "Geer Qwen 3.8 27B (Medium Reasoning)",
    "Geer Qwen 3.8 27B (Extra High Reasoning)",
    "Geer Qwen 3.8 27B (No Thinking)",
    "Geer Qwen 3.8 27B (6-bit MLX)",
    "geer-qwen3-8-27b",
    "geer-local",
)


def _read_settings(
    settings_path: Path,
    *,
    missing_ok: bool = False,
) -> dict[str, Any]:
    try:
        settings = json.loads(settings_path.read_text())
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise AssetError(f"cannot read T3 settings from {settings_path}: file is missing")
    except (OSError, json.JSONDecodeError) as error:
        raise AssetError(f"cannot read T3 settings from {settings_path}: {error}") from error
    if not isinstance(settings, dict):
        raise AssetError(f"T3 settings must contain a JSON object: {settings_path}")
    return settings


def _client_settings_path(settings_path: Path) -> Path:
    return settings_path.with_name("client-settings.json")


def _provider_cache_path(settings_path: Path) -> Path:
    return settings_path.parent.parent / "caches" / f"{PROVIDER_ID}.json"


def _migrate_favorites(client_settings: dict[str, Any], alias: str) -> int:
    favorites = client_settings.get("favorites")
    if favorites is None:
        return 0
    if not isinstance(favorites, list):
        raise AssetError("T3 favorites must contain a JSON array")

    migrated = 0
    alias_present = any(
        isinstance(favorite, dict)
        and favorite.get("provider") == PROVIDER_ID
        and favorite.get("model") == alias
        for favorite in favorites
    )
    updated: list[Any] = []
    for favorite in favorites:
        if (
            isinstance(favorite, dict)
            and favorite.get("provider") == PROVIDER_ID
            and favorite.get("model") in RETIRED_GEER_MODELS
        ):
            migrated += 1
            if not alias_present:
                updated.append({**favorite, "model": alias})
                alias_present = True
            continue
        updated.append(favorite)
    client_settings["favorites"] = updated
    return migrated


def _backup_and_write(
    documents: dict[Path, dict[str, Any]],
    backup_base: Path | None,
    invalidate: tuple[Path, ...] = (),
) -> dict[str, str]:
    backup_base = backup_base or Path("~/.local/state/geer/t3-config").expanduser()
    backup_root = backup_base / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_root.mkdir(parents=True, mode=0o700)
    backup_root.chmod(0o700)

    backups: dict[str, str] = {}
    for path in (*documents, *invalidate):
        if not path.is_file():
            continue
        backup = backup_root / path.name
        backup.write_bytes(path.read_bytes())
        backup.chmod(0o600)
        backups[path.name] = str(backup)

    written: list[Path] = []
    invalidated: list[Path] = []
    try:
        for path, document in documents.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f"{path.suffix}.geer.tmp")
            temporary.write_text(json.dumps(document, indent=2) + "\n")
            temporary.chmod(0o600)
            os.replace(temporary, path)
            written.append(path)
        for path in invalidate:
            if path.is_file():
                path.unlink()
                invalidated.append(path)
    except OSError as error:
        for path in written:
            backup_path = backups.get(path.name)
            if backup_path is None:
                path.unlink(missing_ok=True)
            else:
                backup = Path(backup_path)
                path.write_bytes(backup.read_bytes())
                path.chmod(0o600)
        for path in documents:
            path.with_suffix(f"{path.suffix}.geer.tmp").unlink(missing_ok=True)
        for path in invalidated:
            backup = Path(backups[path.name])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(backup.read_bytes())
            path.chmod(0o600)
        raise AssetError(f"cannot update T3 settings transaction: {error}") from error
    return backups


def planned_t3_settings(
    workspace: Workspace,
    settings_path: Path,
) -> tuple[dict[Path, dict[str, Any]], dict[str, Any]]:
    alias = model_alias(verify_assets(workspace))
    launcher, launch_args = _launcher(workspace)
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise AssetError(f"T3 launcher is missing or not executable: {launcher}")

    settings = _read_settings(settings_path, missing_ok=True)
    client_path = _client_settings_path(settings_path)
    client_settings = _read_settings(client_path, missing_ok=True)
    providers = settings.setdefault("providerInstances", {})
    if not isinstance(providers, dict):
        raise AssetError("T3 providerInstances must contain a JSON object")
    preferences = client_settings.setdefault("providerModelPreferences", {})
    if not isinstance(preferences, dict):
        raise AssetError("T3 providerModelPreferences must contain a JSON object")

    providers[PROVIDER_ID] = {
        "driver": "claudeAgent",
        "displayName": "Geer",
        "enabled": True,
        "config": {
            "enabled": True,
            "binaryPath": str(launcher),
            "homePath": "",
            "customModels": [alias],
            "launchArgs": launch_args,
        },
    }
    legacy_preferences = settings.get("providerModelPreferences")
    if legacy_preferences is not None and not isinstance(legacy_preferences, dict):
        raise AssetError("T3 providerModelPreferences must contain a JSON object")
    if isinstance(legacy_preferences, dict):
        legacy_preferences.pop(PROVIDER_ID, None)
        if not legacy_preferences:
            del settings["providerModelPreferences"]
    preferences[PROVIDER_ID] = {
        "hiddenModels": [*T3_CLAUDE_BUILT_IN_MODELS, *RETIRED_GEER_MODELS],
        "modelOrder": [alias],
    }
    favorites_migrated = _migrate_favorites(client_settings, alias)
    result = {
        "provider": PROVIDER_ID,
        "driver": "claudeAgent",
        "model": alias,
        "launcher": str(launcher),
        "launch_args": launch_args,
        "settings": str(settings_path),
        "client_settings": str(client_path),
        "t3_compatibility_version": f">={T3_MINIMUM_VERSION}",
        "t3_tested_version": T3_TESTED_VERSION,
        "hidden_models": len(T3_CLAUDE_BUILT_IN_MODELS) + len(RETIRED_GEER_MODELS),
        "favorites_migrated": favorites_migrated,
    }
    return {settings_path: settings, client_path: client_settings}, result


def _launcher(workspace: Workspace) -> tuple[Path, str]:
    configured_launcher = os.environ.get("GEER_CLAUDE_LAUNCHER")
    if configured_launcher:
        return Path(configured_launcher).expanduser().resolve(), ""
    if os.environ.get("GEER_HOME"):
        installed_launcher = shutil.which("geer-claude")
        if installed_launcher:
            return Path(installed_launcher).resolve(), ""
    configured = os.environ.get("GEER_CLI")
    if configured:
        return Path(configured).expanduser().resolve(), "launch"
    if os.environ.get("GEER_HOME"):
        installed = shutil.which("geer")
        if installed:
            return Path(installed).resolve(), "launch"
    return workspace.root / "bin" / "geer-claude", ""


def configure_t3(
    workspace: Workspace,
    settings_path: Path,
    backup_base: Path | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    documents, result = planned_t3_settings(workspace, settings_path)
    cache = _provider_cache_path(settings_path)
    if dry_run:
        return {
            **result,
            "applied": False,
            "backups": {},
            "provider_cache": str(cache),
            "cache_invalidated": False,
        }
    cache_exists = cache.is_file()
    backups = _backup_and_write(documents, backup_base, (cache,))
    return {
        **result,
        "applied": True,
        "backups": backups,
        "provider_cache": str(cache),
        "cache_invalidated": cache_exists,
    }


def remove_t3(
    settings_path: Path,
    backup_base: Path | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    client_path = _client_settings_path(settings_path)
    settings = _read_settings(settings_path)
    client_settings = _read_settings(client_path)
    documents = {settings_path: settings, client_path: client_settings}
    removed = False
    for document, section in (
        (settings, "providerInstances"),
        (settings, "providerModelPreferences"),
        (client_settings, "providerModelPreferences"),
    ):
        value = document.get(section)
        if value is not None and not isinstance(value, dict):
            raise AssetError(f"T3 {section} must contain a JSON object")
        if isinstance(value, dict) and PROVIDER_ID in value:
            del value[PROVIDER_ID]
            removed = True
    result = {
        "provider": PROVIDER_ID,
        "settings": str(settings_path),
        "client_settings": str(client_path),
        "removed": removed,
    }
    cache = _provider_cache_path(settings_path)
    if dry_run or not removed:
        return {
            **result,
            "applied": False,
            "backups": {},
            "provider_cache": str(cache),
            "cache_invalidated": False,
        }
    cache_exists = cache.is_file()
    backups = _backup_and_write(documents, backup_base, (cache,))
    return {
        **result,
        "applied": True,
        "backups": backups,
        "provider_cache": str(cache),
        "cache_invalidated": cache_exists,
    }
