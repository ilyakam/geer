from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from .assets import (
    DEFAULT_MODEL_ID,
    AssetError,
    Workspace,
    _sha256,
    discover_model_files,
    initialize_assets,
    verify_assets,
)
from .hardware import HardwareProfile, UnsupportedHardwareError, select_hardware_profile
from .pi_distribution import (
    PI_ARCHIVE_BYTES,
    PI_ARCHIVE_SHA256,
    PI_EXTRACTED_BYTES,
    PI_VERSION,
    pi_distribution_manifest,
)
from .pi_distribution import (
    _installed as pi_installed,
)
from .runtime import (
    OMLX_REVISION,
    OMLX_VERSION,
    RUNTIME_ARCHIVE_BYTES,
    RUNTIME_ARCHIVE_NAME,
    RUNTIME_ARCHIVE_SHA256,
    RUNTIME_INSTALLED_BYTES,
    _server_lock,
    compatible_pi,
    ensure_model_settings,
    stop_server,
)
from .t3 import find_t3, running_t3_applications
from .t3_distribution import T3_ARCHIVE_BYTES, T3_INSTALLED_BYTES

MODEL_FAMILIES = {"sage": "sage-a1.5-35b", "ornith": "ornith-1.5-35b"}
TEMPORARY_SPACE_BYTES = 2 * 1024**3
# Only successful full checks are reused, and only while every file's identity
# and modification metadata match. Nothing is persisted or trusted by pathname.
_VERIFIED: dict[tuple[str, str], tuple[Any, ...]] = {}


def load_model_build(workspace: Workspace) -> ModuleType:
    path = workspace.root / "tools" / "model_build.py"
    spec = importlib.util.spec_from_file_location("geer_model_installer", path)
    if spec is None or spec.loader is None:
        raise AssetError(f"cannot load the model installer: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _model_name(value: str) -> tuple[str, str | None]:
    for family, prefix in MODEL_FAMILIES.items():
        if value == family:
            return family, None
        label = "Sage-A1.5" if family == "sage" else "Ornith 1.5"
        repo_label = "Sage-A1.5" if family == "sage" else "Ornith-1.5"
        for variant, bits in (("6bit", "6-bit"), ("4-8bit", "4/8-bit")):
            if value in {
                f"{prefix}-{variant}",
                f"Geer {label} 35B-A3B ({bits} MLX)",
                f"ilyakam/Geer-{repo_label}-35B-A3B-{variant}-MLX",
            }:
                return family, variant
    raise AssetError(f"unknown model {value!r}; run `geer model list` for available models")


def require_model_name(value: str) -> None:
    _model_name(value)


def recipe_and_installer(
    workspace: Workspace,
    hardware_profile: HardwareProfile,
    *,
    family: str = "sage",
) -> tuple[Any, ModuleType]:
    installer = load_model_build(workspace)
    recipe = installer.load_recipe(
        workspace.root
        / "model-recipes"
        / (f"{MODEL_FAMILIES[family]}-{hardware_profile.model_variant}.toml")
    )
    return recipe, installer


def _selection_path(workspace: Workspace) -> Path:
    return workspace.state / "model-selection.json"


def _saved_family(workspace: Workspace) -> str | None:
    try:
        value = json.loads(_selection_path(workspace).read_text())
    except (OSError, ValueError):
        return None
    if isinstance(value, dict) and value.get("schema_version") == 1:
        family = value.get("family")
        if value.get("confirmed") is True and isinstance(family, str) and family in MODEL_FAMILIES:
            return family
    return None


def _snapshot_signature(snapshot: Path) -> tuple[Any, ...]:
    records = []
    for path in sorted(snapshot.rglob("*")):
        relative = path.relative_to(snapshot).as_posix()
        if relative.startswith(".cache/"):
            continue
        if path.is_file():
            stat = path.stat()
            records.append(
                (
                    relative,
                    str(path.resolve()),
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                )
            )
        elif path.is_symlink():
            raise AssetError("model snapshot contains a broken link")
    if not records:
        raise AssetError("model snapshot is empty")
    return tuple(records)


def _verified_snapshot(installer: ModuleType, recipe: Any, cache: Path) -> Path | None:
    snapshot = installer.published_snapshot_path(recipe, cache)
    if not snapshot.is_dir() or snapshot.is_symlink():
        return None
    key = (str(snapshot), json.dumps(recipe.data, sort_keys=True))
    try:
        before = _snapshot_signature(snapshot)
        if _VERIFIED.get(key) != before:
            installer.verify_published_snapshot(recipe, snapshot, cache)
            discover_model_files(snapshot)
            if _snapshot_signature(snapshot) != before:
                return None
            if len(_VERIFIED) >= 8:
                _VERIFIED.clear()
            _VERIFIED[key] = before
    except (RuntimeError, OSError, ValueError, KeyError, TypeError):
        _VERIFIED.pop(key, None)
        return None
    return snapshot


def _legacy_active_family(
    workspace: Workspace,
    installer: ModuleType,
    rows: list[dict[str, Any]],
) -> str | None:
    active = workspace.models / "active"
    if not active.is_symlink() or not active.exists():
        return None
    snapshot = active.resolve()
    for row in rows:
        if not row["eligible"]:
            continue
        recipe = installer.load_recipe(
            workspace.root
            / "model-recipes"
            / (f"{MODEL_FAMILIES[row['family']]}-{row['model_variant']}.toml")
        )
        key = (str(snapshot), json.dumps(recipe.data, sort_keys=True))
        try:
            before = _snapshot_signature(snapshot)
            if _VERIFIED.get(key) != before:
                installer.verify_output(recipe, snapshot)
                discover_model_files(snapshot)
                if _snapshot_signature(snapshot) != before:
                    continue
                _VERIFIED[key] = before
        except (RuntimeError, OSError, ValueError, KeyError, TypeError):
            continue
        return row["family"]
    return None


def _verified_archive(path: Path, size: int, digest: str) -> bool:
    try:
        stat = path.stat()
        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if stat.st_size != size:
            return False
        key = (str(path), digest)
        if _VERIFIED.get(key) == signature:
            return True
        after = path.stat() if _sha256(path) == digest else None
        if after is None or signature != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            return False
        _VERIFIED[key] = signature
        return True
    except OSError:
        return False


def _dependency_plan(workspace: Workspace) -> dict[str, Any]:
    try:
        marker = json.loads((workspace.runtime / "runtime.json").read_text())
    except (OSError, ValueError):
        marker = {}
    runtime_installed = (
        isinstance(marker, dict)
        and marker.get("version") == OMLX_VERSION
        and marker.get("revision") == OMLX_REVISION
        and marker.get("archive_sha256") == RUNTIME_ARCHIVE_SHA256
        and (workspace.runtime / "omlx/bin/omlx").is_file()
    )
    pi_reusable = False
    if os.environ.get("GEER_PI_BIN"):
        try:
            compatible_pi(workspace, timeout=5)
            pi_reusable = True
        except AssetError:
            pass
    else:
        directory = workspace.runtime / "pi-runtime/pi"
        key = (str(directory), json.dumps(pi_distribution_manifest(), sort_keys=True))
        try:
            signature = _snapshot_signature(directory)
            if _VERIFIED.get(key) == signature:
                pi_reusable = True
            elif pi_installed(directory) and _snapshot_signature(directory) == signature:
                _VERIFIED[key] = signature
                pi_reusable = True
        except (AssetError, OSError):
            pass
    try:
        t3_installed = find_t3().found
    except AssetError:
        t3_installed = False
    runtime_archives = [
        workspace.cache / RUNTIME_ARCHIVE_NAME,
        workspace.root.parent / RUNTIME_ARCHIVE_NAME,
        Path("~/Downloads").expanduser() / RUNTIME_ARCHIVE_NAME,
    ]
    if configured := os.environ.get("GEER_RUNTIME_ARCHIVE"):
        runtime_archives.insert(0, Path(configured).expanduser())
    runtime_download = (
        0
        if runtime_installed
        or any(
            _verified_archive(path, RUNTIME_ARCHIVE_BYTES, RUNTIME_ARCHIVE_SHA256)
            for path in runtime_archives
        )
        else RUNTIME_ARCHIVE_BYTES
    )
    pi_archive = workspace.runtime / "pi-runtime/archives" / f"pi-{PI_VERSION}-darwin-arm64.tar.gz"
    pi_download = (
        0
        if pi_reusable
        or _verified_archive(
            pi_archive,
            PI_ARCHIVE_BYTES,
            PI_ARCHIVE_SHA256,
        )
        else PI_ARCHIVE_BYTES
    )
    return {
        "runtime_download_bytes": runtime_download,
        "runtime_required_free_bytes": runtime_download
        + (0 if runtime_installed else RUNTIME_INSTALLED_BYTES),
        "runtime_reusable": runtime_installed,
        "pi_download_bytes": pi_download,
        "pi_required_free_bytes": pi_download + (0 if pi_reusable else PI_EXTRACTED_BYTES),
        "pi_reusable": pi_reusable,
        "t3_download_bytes": 0 if t3_installed else T3_ARCHIVE_BYTES,
        "t3_required_free_bytes": 0 if t3_installed else T3_ARCHIVE_BYTES + T3_INSTALLED_BYTES,
        "t3_reusable": t3_installed,
    }


def _catalog(workspace: Workspace, total_memory_bytes: int | None) -> dict[str, Any]:
    rows = []
    cache = workspace.cache / "huggingface"
    active = workspace.models / "active"
    installer = load_model_build(workspace)
    dependencies = _dependency_plan(workspace)
    # Detect once so both rows describe the same hardware inventory.
    try:
        detected = select_hardware_profile(total_memory_bytes)
    except UnsupportedHardwareError as error:
        detected = None
        hardware_error = str(error)
    for family, prefix in MODEL_FAMILIES.items():
        profile = (
            select_hardware_profile(detected.total_memory_bytes, family=family)
            if detected
            else None
        )
        variant = profile.model_variant if profile else None
        recipe = installer.load_recipe(
            workspace.root / "model-recipes" / (f"{prefix}-{variant or '4-8bit'}.toml")
        )
        eligible = profile is not None
        reason = None if eligible else hardware_error
        distribution = recipe.distribution
        try:
            revision = installer.distribution_revision(recipe)
            installed_bytes = int(distribution["expected_bytes"])
        except (RuntimeError, ValueError, KeyError, TypeError):
            revision = None
            installed_bytes = 0
            eligible = False
            reason = "This model's approved distribution is not published yet."
        snapshot = _verified_snapshot(installer, recipe, cache) if eligible else None
        installed = snapshot is not None
        is_active = bool(
            installed and active.is_symlink() and active.resolve() == snapshot.resolve()
        )
        download_bytes = 0 if installed else installed_bytes
        estimated = not installed
        if eligible and not installed:
            try:
                download_plan = installer.published_install_plan(recipe, cache)
                download_bytes = int(download_plan["download_bytes"])
                estimated = bool(download_plan.get("download_estimated", False))
                if download_bytes == 0:
                    download_bytes = installed_bytes
                    estimated = True
            except (RuntimeError, OSError, ValueError):
                # Offline preview still describes the pinned distribution. It
                # never treats remote availability as a verified installation.
                pass
        temporary_bytes = (
            0 if installed else max(TEMPORARY_SPACE_BYTES, installer.SAFETY_MARGIN_BYTES)
        )
        required_free = (
            download_bytes
            + temporary_bytes
            + sum(dependencies[f"{name}_required_free_bytes"] for name in ("runtime", "pi", "t3"))
        )
        row = {
            "id": family,
            "family": family,
            "profile": family,
            "display_name": str(recipe.data["display_name"]),
            "recommended": family == "sage",
            "eligible": eligible,
            "reason": reason,
            "installed": installed,
            "active": is_active,
            "activation_changes": not is_active,
            "model_variant": variant,
            "hardware_profile": profile.id if profile else None,
            "repo_id": str(distribution["repo_id"]),
            "revision": revision,
            "download_estimated": estimated,
            "context_window": profile.max_context_window if profile else 0,
            "context_window_human": f"{profile.max_context_window // 1024}K tokens"
            if profile
            else "Unavailable",
            "kv_cache": profile.kv_cache if profile else None,
            "reusable_active_model": is_active,
            "active_model_installed": active.is_symlink() and active.exists(),
            "cache_dir": str(cache),
            "active_link": str(active),
            "snapshot": str(snapshot) if snapshot else None,
            "temporary_note": "Conservative estimate for additional temporary space.",
            **dependencies,
        }
        for key, count in {
            "download": download_bytes,
            "installed": installed_bytes,
            "required_free": required_free,
            "temporary": temporary_bytes,
            "runtime_download": dependencies["runtime_download_bytes"],
            "runtime_installed": RUNTIME_INSTALLED_BYTES,
            "pi_download": dependencies["pi_download_bytes"],
            "pi_installed": PI_EXTRACTED_BYTES,
            "t3_download": dependencies["t3_download_bytes"],
            "t3_installed": T3_INSTALLED_BYTES,
        }.items():
            row[f"{key}_bytes"] = count
            row[f"{key}_human"] = installer.human_bytes(count)
        row.update(name=row["display_name"], download=row["download_human"])
        rows.append(row)
    saved = _saved_family(workspace)
    eligible_rows = [row for row in rows if row["eligible"]]
    active_rows = [row for row in eligible_rows if row["active"]]
    cached_rows = [row for row in eligible_rows if row["installed"]]
    if any(row["family"] == saved for row in eligible_rows):
        selected = saved
        selection_source = "saved"
    elif active_rows:
        selected = active_rows[0]["family"]
        selection_source = "legacy_active"
    elif legacy := _legacy_active_family(workspace, installer, rows):
        selected = legacy
        selection_source = "legacy_active"
    elif len(cached_rows) == 1:
        selected = cached_rows[0]["family"]
        selection_source = "cached"
    else:
        selected = "sage"
        selection_source = "recommended"
    return {"selected": selected, "selection_source": selection_source, "models": rows}


def model_catalog(
    workspace: Workspace,
    *,
    total_memory_bytes: int | None = None,
) -> dict[str, Any]:
    return _catalog(workspace, total_memory_bytes)


def model_plan(
    workspace: Workspace,
    *,
    family: str | None = None,
    total_memory_bytes: int | None = None,
) -> dict[str, Any]:
    catalog = model_catalog(workspace, total_memory_bytes=total_memory_bytes)
    selected, variant = _model_name(family) if family is not None else (catalog["selected"], None)
    row = next(row for row in catalog["models"] if row["family"] == selected)
    if variant is not None and variant != row["model_variant"]:
        raise AssetError(
            f"{family} is incompatible with this Mac; use the {row['model_variant']} variant"
        )
    return row


def _require_eligible(plan: dict[str, Any]) -> None:
    if not plan["eligible"]:
        raise AssetError(str(plan["reason"]))


def _require_t3_closed() -> None:
    if running_t3_applications():
        raise AssetError(
            "Finish your chats and quit T3 Code before changing the active model, then retry."
        )


def _capture_paths(workspace: Workspace) -> dict[Path, tuple[str, Any]]:
    paths = [
        workspace.models / "active",
        workspace.models / "previous",
        workspace.manifest,
        workspace.setup_state,
        workspace.omlx_model_settings,
        _selection_path(workspace),
    ]
    linked = workspace.models / DEFAULT_MODEL_ID
    if linked.is_symlink() or (linked.exists() and not linked.is_dir()):
        raise AssetError(f"refusing to replace unexpected asset path: {linked}")
    if linked.is_dir():
        paths.extend(linked.iterdir())
    backup = {}
    for path in paths:
        if path.is_symlink():
            backup[path] = ("link", os.readlink(path))
        elif path.is_file() and path.parent != linked and path.name not in {"active", "previous"}:
            backup[path] = ("file", (path.read_bytes(), path.stat().st_mode & 0o777))
        elif path.exists():
            raise AssetError(f"refusing to replace unmanaged model path: {path}")
        else:
            backup[path] = ("absent", None)
    return backup


def _restore_paths(workspace: Workspace, backup: dict[Path, tuple[str, Any]]) -> None:
    linked = workspace.models / DEFAULT_MODEL_ID
    if linked.is_dir():
        for path in linked.iterdir():
            if path not in backup and path.is_symlink():
                path.unlink()
    for path, (kind, value) in backup.items():
        if kind == "absent":
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.rollback-{os.getpid()}")
            temporary.unlink(missing_ok=True)
            if kind == "link":
                temporary.symlink_to(value)
            else:
                temporary.write_bytes(value[0])
                temporary.chmod(value[1])
            os.replace(temporary, path)


def _confirm_choice(workspace: Workspace, family: str) -> None:
    path = _selection_path(workspace)
    workspace.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise AssetError(f"refusing to replace unexpected selection symlink: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps({"schema_version": 1, "family": family, "confirmed": True}) + "\n"
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _activate(
    workspace: Workspace,
    plan: dict[str, Any],
    recipe: Any,
    installer: ModuleType,
    snapshot: Path,
    total_memory_bytes: int | None,
) -> dict[str, Any]:
    with _server_lock(workspace):
        active = workspace.models / "active"
        same = active.is_symlink() and active.resolve() == snapshot.resolve()
        if same:
            try:
                assets = verify_assets(workspace)
                same = Path(assets["source"]).resolve() == snapshot.resolve()
            except (AssetError, OSError, KeyError):
                same = False
        if same:
            _confirm_choice(workspace, plan["family"])
            return {"model": plan["family"], "active": True, "assets": assets, "reused": True}
        changing_active = active.is_symlink() and active.exists()
        if changing_active:
            _require_t3_closed()
        backup = _capture_paths(workspace)
        if changing_active:
            stop_server(workspace)
        try:
            activation = installer.activate_snapshot(snapshot, home=workspace.runtime)
            assets = initialize_assets(workspace, snapshot, DEFAULT_MODEL_ID)
            settings = ensure_model_settings(workspace, total_memory_bytes=total_memory_bytes)
            _confirm_choice(workspace, plan["family"])
        except Exception:
            _restore_paths(workspace, backup)
            raise
    return {
        "model": plan["family"],
        "active": True,
        "assets": assets,
        "activation": activation,
        "model_settings": settings,
        "reused": False,
    }


def install_model(
    workspace: Workspace,
    *,
    family: str | None = None,
    high_performance: bool = False,
    total_memory_bytes: int | None = None,
    activate: bool = True,
) -> dict[str, Any]:
    plan = model_plan(workspace, family=family, total_memory_bytes=total_memory_bytes)
    _require_eligible(plan)
    profile = select_hardware_profile(total_memory_bytes, family=plan["family"])
    recipe, installer = recipe_and_installer(workspace, profile, family=plan["family"])
    if activate and plan["active_model_installed"] and plan["activation_changes"]:
        _require_t3_closed()
    previous_transfer = os.environ.get("HF_XET_HIGH_PERFORMANCE")
    try:
        if high_performance:
            os.environ["HF_XET_HIGH_PERFORMANCE"] = "1"
        installed = installer.install_published(recipe, Path(plan["cache_dir"]), activate=False)
    finally:
        if high_performance:
            if previous_transfer is None:
                os.environ.pop("HF_XET_HIGH_PERFORMANCE", None)
            else:
                os.environ["HF_XET_HIGH_PERFORMANCE"] = previous_transfer
    if not activate:
        return {"model": plan["family"], "installed": installed, "activated": False}
    snapshot = Path(installed["output_dir"])
    result = _activate(workspace, plan, recipe, installer, snapshot, total_memory_bytes)
    return {**result, "installed": installed, "activated": True}


def list_models(workspace: Workspace) -> dict[str, Any]:
    return model_catalog(workspace)


def use_model(
    workspace: Workspace,
    value: str,
    *,
    total_memory_bytes: int | None = None,
) -> dict[str, Any]:
    plan = model_plan(workspace, family=value, total_memory_bytes=total_memory_bytes)
    _require_eligible(plan)
    if not plan["installed"]:
        raise AssetError(f"the model is not installed; run `geer model add {plan['family']}`")
    profile = select_hardware_profile(total_memory_bytes, family=plan["family"])
    recipe, installer = recipe_and_installer(workspace, profile, family=plan["family"])
    return _activate(workspace, plan, recipe, installer, Path(plan["snapshot"]), total_memory_bytes)


def remove_model(
    workspace: Workspace,
    value: str,
    *,
    total_memory_bytes: int | None = None,
) -> dict[str, Any]:
    plan = model_plan(workspace, family=value, total_memory_bytes=total_memory_bytes)
    active = workspace.models / "active"
    previous = workspace.models / "previous"
    snapshot = Path(plan["snapshot"]) if plan["snapshot"] else None
    with _server_lock(workspace):
        # Removing another cached family never touches the current runtime.
        removes_active = bool(
            snapshot and active.is_symlink() and active.resolve() == snapshot.resolve()
        )
        if not removes_active and active.is_symlink() and active.exists():
            installer = load_model_build(workspace)
            removes_active = _legacy_active_family(workspace, installer, [plan]) == plan["family"]
        if removes_active:
            _require_t3_closed()
            try:
                assets = verify_assets(workspace)
                matching_assets = Path(assets["source"]).resolve() == active.resolve()
            except (AssetError, OSError, KeyError):
                matching_assets = False
            backup = _capture_paths(workspace)
            stop_server(workspace)
            try:
                linked = workspace.models / DEFAULT_MODEL_ID
                if matching_assets and linked.is_dir():
                    for entry in linked.iterdir():
                        entry.unlink()
                    linked.rmdir()
                active.unlink()
                if matching_assets:
                    workspace.manifest.unlink(missing_ok=True)
                    workspace.setup_state.unlink(missing_ok=True)
            except Exception:
                _restore_paths(workspace, backup)
                raise
        retained = bool(
            snapshot and previous.is_symlink() and previous.resolve() == snapshot.resolve()
        )
        reclaimed = False
        if snapshot and not retained:
            from huggingface_hub import scan_cache_dir

            info = scan_cache_dir(Path(plan["cache_dir"]))
            for repo in info.repos:
                if repo.repo_id == plan["repo_id"]:
                    for revision in repo.revisions:
                        if revision.commit_hash == plan["revision"]:
                            info.delete_revisions(revision.commit_hash).execute()
                            reclaimed = True
                            break
    return {
        "model": plan["family"],
        "removed": removes_active or reclaimed,
        "active_removed": removes_active,
        "cache_reclaimed": reclaimed,
        "cache_retained_for_rollback": retained,
    }
