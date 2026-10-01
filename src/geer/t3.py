from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .assets import DEFAULT_MODEL_ID, AssetError, Workspace, model_alias, verify_assets

PROVIDER_ID = "piGeer"
LEGACY_PROVIDER_ID = "claudeGeer"
# This release has the ACP metadata discovery contract used by Geer's adapter.
T3_MINIMUM_VERSION = "0.0.43"
T3_TESTED_VERSION = "0.0.44"
T3_BUNDLE_ID = "com.t3tools.t3code"
T3_TRANSPORT_BUILT_IN_MODELS = ("grok-build",)
_MODEL_SELECTION_KEYS = (
    "defaultModelSelection", "textGenerationModelSelection", "sourceControlWriterModelSelection",
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


@dataclass(frozen=True)
class T3Application:
    found: bool
    version: str | None = None
    path: Path | None = None
    running: bool = False


def t3_settings_path() -> Path:
    configured_home = os.environ.get("T3CODE_HOME")
    home = Path(configured_home).expanduser() if configured_home else Path.home() / ".t3"
    return home.resolve() / "userdata" / "settings.json"


def _t3_app_candidates() -> tuple[Path, ...]:
    names = ("T3 Code.app", "T3 Code (Alpha).app", "T3 Code (Nightly).app", "T3.app")
    directories = (Path("/Applications"), Path.home() / "Applications")
    candidates = [directory / name for directory in directories for name in names]
    for directory in directories:
        candidates.extend(sorted(directory.glob("*.app")))
    return tuple(dict.fromkeys(candidates))


def _t3_bundle(app: Path) -> tuple[str | None, Path] | None:
    try:
        with (app / "Contents" / "Info.plist").open("rb") as stream:
            metadata = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    if not isinstance(metadata, dict) or metadata.get("CFBundleIdentifier") != T3_BUNDLE_ID:
        return None
    executable_name = metadata.get("CFBundleExecutable")
    if (
        not isinstance(executable_name, str)
        or not executable_name
        or Path(executable_name).name != executable_name
        or executable_name in {".", ".."}
    ):
        return None
    executable = app / "Contents" / "MacOS" / executable_name
    if not executable.is_file() or not os.access(executable, os.X_OK):
        return None
    version = metadata.get("CFBundleShortVersionString")
    return (version if isinstance(version, str) else None), executable.resolve()


def _version_key(version: str | None) -> tuple[Any, ...] | None:
    if version is None:
        return None
    match = re.fullmatch(
        r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", version
    )
    if match is None:
        return None
    major, minor, patch, prerelease = match.groups()
    identifiers = tuple(
        (0, int(part)) if part.isdecimal() else (1, part)
        for part in (prerelease or "").split(".")
    )
    return (int(major), int(minor), int(patch)), prerelease is None, identifiers


def running_t3_applications() -> list[Path]:
    """Identify this account's active T3 bundles without matching process arguments."""
    try:
        inventory = subprocess.run(
            ["ps", "-axww", "-o", "uid=,pid=,comm="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AssetError(f"cannot inspect running T3 Code applications: {error}") from error
    if inventory.returncode != 0:
        detail = inventory.stderr.strip() or f"ps exited with status {inventory.returncode}"
        raise AssetError(f"cannot inspect running T3 Code applications: {detail}")
    applications: set[Path] = set()
    for line in inventory.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.strip().split(maxsplit=2)
        if len(parts) != 3 or not parts[0].lstrip("-").isdecimal() or not parts[1].isdecimal():
            raise AssetError(
                "cannot inspect running T3 Code applications: invalid process inventory"
            )
        if int(parts[0]) != os.getuid():
            continue
        executable = Path(parts[2])
        if not executable.is_absolute():
            continue
        for app in executable.parents:
            if app.suffix != ".app":
                continue
            if _t3_bundle(app) is not None:
                applications.add(app.resolve())
                break
    return sorted(applications)


def find_t3() -> T3Application:
    running = set(running_t3_applications())
    minimum = _version_key(T3_MINIMUM_VERSION)
    assert minimum is not None
    applications: list[tuple[bool, tuple[Any, ...], str, Path]] = []
    candidates = dict.fromkeys((*_t3_app_candidates(), *sorted(running)))
    for candidate in candidates:
        app = candidate.resolve()
        bundle = _t3_bundle(app)
        if bundle is None:
            continue
        version, _ = bundle
        parsed = _version_key(version)
        if parsed is None:
            continue
        applications.append((app in running, parsed, version or "", app))
    if not applications:
        return T3Application(False)
    compatible = [application for application in applications if application[1][0] >= minimum[0]]
    active, _, version, app = max(
        compatible or applications, key=lambda value: (value[0], value[1])
    )
    return T3Application(bool(compatible), version, app, active)


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


def _provider_cache_path(settings_path: Path, provider: str = PROVIDER_ID) -> Path:
    return settings_path.parent.parent / "caches" / f"{provider}.json"


def _migrate_favorites(
    client_settings: dict[str, Any], model_id: str, *, retire_legacy: bool = True
) -> int:
    favorites = client_settings.get("favorites")
    if favorites is None:
        return 0
    if not isinstance(favorites, list):
        raise AssetError("T3 favorites must contain a JSON array")

    migrated = 0
    model_present = any(
        isinstance(favorite, dict)
        and favorite.get("provider") == PROVIDER_ID
        and favorite.get("model") == model_id
        for favorite in favorites
    )
    updated: list[Any] = []
    for favorite in favorites:
        if (
            isinstance(favorite, dict)
            and (
                favorite.get("provider") == PROVIDER_ID
                and favorite.get("model") in RETIRED_GEER_MODELS
                and favorite.get("model") != model_id
                or retire_legacy and favorite.get("provider") == LEGACY_PROVIDER_ID
            )
        ):
            migrated += 1
            if not model_present:
                updated.append({**favorite, "provider": PROVIDER_ID, "model": model_id})
                model_present = True
            continue
        updated.append(favorite)
    client_settings["favorites"] = updated
    return migrated


def _selection_sections(settings: dict[str, Any]) -> list[dict[str, Any]]:
    sections = [settings]
    overrides = settings.get("projectSettingsOverrides", {})
    if not isinstance(overrides, dict):
        raise AssetError("T3 projectSettingsOverrides must contain a JSON object")
    sections.extend(value for value in overrides.values() if isinstance(value, dict))
    return sections


def _model_selections(settings: dict[str, Any]) -> list[dict[str, Any]]:
    selections: list[dict[str, Any]] = []
    for section in _selection_sections(settings):
        for key in _MODEL_SELECTION_KEYS:
            selection = section.get(key)
            if selection is None:
                continue
            if not isinstance(selection, dict):
                raise AssetError(f"T3 {key} must contain a model-selection object")
            selections.append(selection)
    return selections


def _legacy_selection(
    selection: dict[str, Any], model_id: str, *, retire_legacy: bool = True
) -> bool:
    provider = selection.get("instanceId", selection.get("provider"))
    return bool(
        retire_legacy and provider == LEGACY_PROVIDER_ID
        or provider == PROVIDER_ID and (
            "instanceId" not in selection
            or selection.get("model") in RETIRED_GEER_MODELS
            and selection.get("model") != model_id
            or bool(selection.get("options"))
        )
    )


def _migrate_selections(
    settings: dict[str, Any], model_id: str, *, retire_legacy: bool = True
) -> int:
    migrated = 0
    for selection in _model_selections(settings):
        if not _legacy_selection(selection, model_id, retire_legacy=retire_legacy):
            continue
        provider = selection.get("instanceId", selection.get("provider"))
        if provider == LEGACY_PROVIDER_ID or selection.get("model") in RETIRED_GEER_MODELS:
            selection["model"] = model_id
        selection["instanceId"] = PROVIDER_ID
        selection.pop("provider", None)
        selection.pop("options", None)
        migrated += 1
    return migrated


def _model_preferences(preferences: dict[str, Any], model_id: str) -> list[str]:
    current = preferences.get(PROVIDER_ID, {})
    if not isinstance(current, dict):
        raise AssetError("T3 Geer model preferences must contain a JSON object")
    hidden = current.get("hiddenModels", [])
    order = current.get("modelOrder", [])
    if not isinstance(hidden, list) or not all(isinstance(model, str) for model in hidden):
        raise AssetError("T3 Geer hiddenModels must contain an array of model IDs")
    if not isinstance(order, list) or not all(isinstance(model, str) for model in order):
        raise AssetError("T3 Geer modelOrder must contain an array of model IDs")
    retired = [
        model for model in (*T3_TRANSPORT_BUILT_IN_MODELS, *RETIRED_GEER_MODELS)
        if model != model_id
    ]
    hidden = list(dict.fromkeys(model for model in (*hidden, *retired) if model != model_id))
    order = list(dict.fromkeys((model_id, *(model for model in order if model not in retired))))
    preferences[PROVIDER_ID] = {**current, "hiddenModels": hidden, "modelOrder": order}
    return hidden


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
    *,
    retire_legacy: bool = True,
) -> tuple[dict[Path, dict[str, Any]], dict[str, Any]]:
    manifest = verify_assets(workspace)
    alias = model_alias(manifest)
    model_id = str(manifest.get("model_id", DEFAULT_MODEL_ID))
    launcher, launch_args = _launcher(workspace)
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise AssetError(f"T3 launcher is missing or not executable: {launcher}")

    new_settings = not settings_path.exists()
    settings = _read_settings(settings_path, missing_ok=True)
    client_path = _client_settings_path(settings_path)
    client_settings = _read_settings(client_path, missing_ok=True)
    if new_settings:
        # Stable T3 0.0.44 decodes these sparse server settings directly.
        selection = {"instanceId": PROVIDER_ID, "model": model_id}
        settings["defaultModelSelection"] = dict(selection)
        settings["textGenerationModelSelection"] = dict(selection)
        settings["enableProviderUpdateChecks"] = False
        settings["providers"] = {
            "codex": {"enabled": False},
            "claudeAgent": {"enabled": False},
        }
    providers = settings.setdefault("providerInstances", {})
    if not isinstance(providers, dict):
        raise AssetError("T3 providerInstances must contain a JSON object")
    preferences = client_settings.setdefault("providerModelPreferences", {})
    if not isinstance(preferences, dict):
        raise AssetError("T3 providerModelPreferences must contain a JSON object")

    # T3 does not yet have a native Pi driver. Its existing Grok ACP driver
    # speaks the same transport as Geer's Pi adapter; no Grok CLI is launched.
    current_provider = providers.get(PROVIDER_ID, {})
    if not isinstance(current_provider, dict):
        raise AssetError("T3 Geer provider must contain a JSON object")
    current_config = current_provider.get("config", {})
    if not isinstance(current_config, dict):
        raise AssetError("T3 Geer provider config must contain a JSON object")
    # T3 folds this legacy flag into the provider's enabled field on each save.
    current_config.pop("enabled", None)
    providers[PROVIDER_ID] = {
        **current_provider,
        "driver": "grok",
        "displayName": "Geer",
        "enabled": True,
        "config": {
            **current_config,
            "binaryPath": str(launcher),
            "customModels": [],
        },
    }
    legacy_preferences = settings.get("providerModelPreferences")
    if legacy_preferences is not None and not isinstance(legacy_preferences, dict):
        raise AssetError("T3 providerModelPreferences must contain a JSON object")
    if isinstance(legacy_preferences, dict):
        legacy_preferences.pop(PROVIDER_ID, None)
        if retire_legacy:
            legacy_preferences.pop(LEGACY_PROVIDER_ID, None)
        if not legacy_preferences:
            del settings["providerModelPreferences"]
    hidden_models = _model_preferences(preferences, model_id)
    legacy_removed = False
    if retire_legacy:
        legacy_removed = providers.pop(LEGACY_PROVIDER_ID, None) is not None
        preferences.pop(LEGACY_PROVIDER_ID, None)
    favorites_migrated = _migrate_favorites(
        client_settings, model_id, retire_legacy=retire_legacy
    )
    selections_migrated = _migrate_selections(settings, model_id, retire_legacy=retire_legacy)
    if new_settings:
        favorites = client_settings.get("favorites")
        if favorites is None:
            favorites = []
            client_settings["favorites"] = favorites
        favorite = {"provider": PROVIDER_ID, "model": model_id}
        if not any(
            isinstance(value, dict)
            and value.get("provider") == PROVIDER_ID
            and value.get("model") == model_id
            for value in favorites
        ):
            favorites.append(favorite)
    result = {
        "provider": PROVIDER_ID,
        "driver": "grok",
        "harness": "Pi",
        "transport": "ACP through T3's Grok driver",
        "model": alias,
        "model_id": model_id,
        "launcher": str(launcher),
        "launch_args": launch_args,
        "settings": str(settings_path),
        "client_settings": str(client_path),
        "t3_compatibility_version": f">={T3_MINIMUM_VERSION}",
        "t3_tested_version": T3_TESTED_VERSION,
        "hidden_models": len(hidden_models),
        "favorites_migrated": favorites_migrated,
        "selections_migrated": selections_migrated,
        "legacy_provider_removed": legacy_removed,
    }
    return {settings_path: settings, client_path: client_settings}, result


def _launcher(workspace: Workspace) -> tuple[Path, str]:
    configured_launcher = os.environ.get("GEER_PI_LAUNCHER")
    if configured_launcher:
        return Path(configured_launcher).expanduser().resolve(), ""
    if os.environ.get("GEER_HOME"):
        installed_launcher = shutil.which("geer-pi")
        if installed_launcher:
            return Path(installed_launcher).resolve(), ""
    return workspace.root / "bin" / "geer-pi", ""


def t3_status(workspace: Workspace, settings_path: Path | None = None) -> dict[str, Any]:
    """Read application, launcher, model, and profile readiness without starting anything."""
    settings_path = settings_path or t3_settings_path()
    result: dict[str, Any] = {
        "available": False,
        "application_path": None,
        "version": None,
        "running": False,
        "settings": str(settings_path),
        "configured": False,
        "ready": False,
        "error": None,
    }
    try:
        application = find_t3()
        result.update(
            available=application.found,
            application_path=str(application.path) if application.path is not None else None,
            version=application.version,
            running=application.running,
        )
        if not application.found:
            if application.path is not None:
                raise AssetError(
                    f"T3 Code {application.version} at {application.path} needs an update "
                    f"to {T3_MINIMUM_VERSION} or newer; update that app and rerun geer setup"
                )
            raise AssetError(
                f"a compatible T3 Code application (>={T3_MINIMUM_VERSION}) is missing; "
                "run geer setup"
            )
        manifest = verify_assets(workspace)
        model_id = str(manifest.get("model_id", DEFAULT_MODEL_ID))
        result["model_id"] = model_id
        launcher, _ = _launcher(workspace)
        if not launcher.is_file() or not os.access(launcher, os.X_OK):
            raise AssetError(f"T3 launcher is missing or not executable: {launcher}")
        settings = _read_settings(settings_path)
        client_settings = _read_settings(_client_settings_path(settings_path))
        providers = settings.get("providerInstances")
        if not isinstance(providers, dict):
            raise AssetError("T3 providerInstances must contain a JSON object")
        provider = providers.get(PROVIDER_ID)
        if not isinstance(provider, dict) or provider.get("driver") != "grok":
            raise AssetError("T3 Geer provider is missing or does not use the Grok ACP driver")
        config = provider.get("config")
        if (
            not isinstance(config, dict)
            or provider.get("enabled", config.get("enabled", False)) is not True
            or config.get("enabled", True) is not True
        ):
            raise AssetError("T3 Geer provider is disabled or has invalid configuration")
        binary_path = config.get("binaryPath")
        if (
            not isinstance(binary_path, str)
            or not Path(binary_path).is_absolute()
            or Path(binary_path).resolve() != launcher.resolve()
        ):
            raise AssetError(f"T3 Geer provider does not use the expected launcher: {launcher}")
        preferences = client_settings.get("providerModelPreferences")
        if not isinstance(preferences, dict):
            raise AssetError("T3 client model preferences are missing")
        preference = preferences.get(PROVIDER_ID)
        if not isinstance(preference, dict):
            raise AssetError("T3 Geer model preferences are missing")
        hidden = preference.get("hiddenModels", [])
        order = preference.get("modelOrder", [])
        if (
            not isinstance(hidden, list) or not isinstance(order, list)
            or not all(isinstance(model, str) for model in (*hidden, *order))
        ):
            raise AssetError("T3 Geer model preferences contain invalid model lists")
        if model_id in hidden or model_id not in order:
            raise AssetError(
                f"the active Geer model {model_id} is hidden or missing from T3 modelOrder"
            )
        legacy_preferences = settings.get("providerModelPreferences", {})
        if not isinstance(legacy_preferences, dict):
            raise AssetError("T3 server model preferences contain an invalid object")
        if (
            LEGACY_PROVIDER_ID in providers
            or LEGACY_PROVIDER_ID in preferences
            or any(provider in legacy_preferences for provider in (PROVIDER_ID, LEGACY_PROVIDER_ID))
        ):
            raise AssetError(
                "T3 still contains legacy Geer provider bindings; run geer setup to migrate"
            )
        favorites = client_settings.get("favorites", [])
        if not isinstance(favorites, list):
            raise AssetError("T3 favorites must contain a JSON array")
        if any(
            isinstance(favorite, dict)
            and (
                favorite.get("provider") == LEGACY_PROVIDER_ID
                or favorite.get("provider") == PROVIDER_ID
                and favorite.get("model") in RETIRED_GEER_MODELS
                and favorite.get("model") != model_id
            )
            for favorite in favorites
        ) or any(
            _legacy_selection(selection, model_id) for selection in _model_selections(settings)
        ):
            raise AssetError(
                "T3 has obsolete Geer model selections or unsupported options; "
                "run geer setup to migrate"
            )
        result.update(configured=True, ready=True)
    except (AssetError, OSError, ValueError) as error:
        result["error"] = str(error)
    return result


def configure_t3(
    workspace: Workspace,
    settings_path: Path,
    backup_base: Path | None = None,
    *,
    dry_run: bool = False,
    retire_legacy: bool = True,
) -> dict[str, Any]:
    documents, result = planned_t3_settings(
        workspace, settings_path, retire_legacy=retire_legacy
    )
    changed_documents = {
        path: document for path, document in documents.items()
        if not path.is_file() or _read_settings(path) != document
    }
    changed = bool(changed_documents)
    cache = _provider_cache_path(settings_path)
    caches = (
        (cache, _provider_cache_path(settings_path, LEGACY_PROVIDER_ID))
        if retire_legacy
        else (cache,)
    )
    if dry_run or not changed:
        return {
            **result,
            "changed": changed,
            "applied": False,
            "backups": {},
            "provider_cache": str(cache),
            "cache_invalidated": False,
        }
    active = running_t3_applications()
    if active:
        raise AssetError(
            "quit T3 Code before updating its settings: " + ", ".join(map(str, active))
        )
    cache_exists = any(path.is_file() for path in caches)
    backups = _backup_and_write(changed_documents, backup_base, caches)
    return {
        **result,
        "changed": True,
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
    settings = _read_settings(settings_path, missing_ok=True)
    client_settings = _read_settings(client_path, missing_ok=True)
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
        if isinstance(value, dict):
            for provider in (PROVIDER_ID, LEGACY_PROVIDER_ID):
                if provider in value:
                    del value[provider]
                    removed = True
    favorites = client_settings.get("favorites")
    favorites_removed = 0
    if favorites is not None:
        if not isinstance(favorites, list):
            raise AssetError("T3 favorites must contain a JSON array")
        retained = [
            favorite for favorite in favorites
            if not (
                isinstance(favorite, dict)
                and favorite.get("provider") in {PROVIDER_ID, LEGACY_PROVIDER_ID}
            )
        ]
        favorites_removed = len(favorites) - len(retained)
        if favorites_removed:
            client_settings["favorites"] = retained
            removed = True
    selections_removed = 0
    for section in _selection_sections(settings):
        for key in _MODEL_SELECTION_KEYS:
            selection = section.get(key)
            if (
                isinstance(selection, dict)
                and selection.get("instanceId", selection.get("provider"))
                in {PROVIDER_ID, LEGACY_PROVIDER_ID}
            ):
                del section[key]
                selections_removed += 1
                removed = True
    result = {
        "provider": PROVIDER_ID,
        "settings": str(settings_path),
        "client_settings": str(client_path),
        "removed": removed,
        "favorites_removed": favorites_removed,
        "selections_removed": selections_removed,
    }
    cache = _provider_cache_path(settings_path)
    caches = (cache, _provider_cache_path(settings_path, LEGACY_PROVIDER_ID))
    if dry_run or not removed:
        return {
            **result,
            "applied": False,
            "backups": {},
            "provider_cache": str(cache),
            "cache_invalidated": False,
        }
    active = running_t3_applications()
    if active:
        raise AssetError(
            "quit T3 Code before removing Geer settings: " + ", ".join(map(str, active))
        )
    cache_exists = any(path.is_file() for path in caches)
    changed_documents = {
        path: document for path, document in documents.items()
        if path.is_file() and _read_settings(path) != document
    }
    backups = _backup_and_write(changed_documents, backup_base, caches)
    return {
        **result,
        "applied": True,
        "backups": backups,
        "provider_cache": str(cache),
        "cache_invalidated": cache_exists,
    }
