from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from .assets import DEFAULT_MODEL_ID, AssetError, Workspace, initialize_assets
from .hardware import HardwareProfile, select_hardware_profile
from .runtime import (
    RUNTIME_ARCHIVE_BYTES,
    RUNTIME_INSTALLED_BYTES,
    ensure_model_settings,
)

MODEL_PROFILE = "ornith"
MODEL_RECIPES = {
    "6bit": "ornith-1.0-35b-6bit.toml",
    "4-8bit": "ornith-1.0-35b-4-8bit.toml",
}
MODEL_NAMES = {
    MODEL_PROFILE,
    "ornith-1.0-35b-6bit",
    "ornith-1.0-35b-4-8bit",
    "Geer Ornith 1.0 35B-A3B (6-bit MLX)",
    "Geer Ornith 1.0 35B-A3B (4/8-bit MLX)",
    "ilyakam/Geer-Ornith-1.0-35B-A3B-6bit-MLX",
    "ilyakam/Geer-Ornith-1.0-35B-A3B-4-8bit-MLX",
}
TEMPORARY_SPACE_BYTES = 2 * 1024**3


def load_model_build(workspace: Workspace) -> ModuleType:
    path = workspace.root / "tools" / "model_build.py"
    spec = importlib.util.spec_from_file_location("geer_model_installer", path)
    if spec is None or spec.loader is None:
        raise AssetError(f"cannot load the model installer: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def recipe_and_installer(
    workspace: Workspace,
    hardware_profile: HardwareProfile,
) -> tuple[Any, ModuleType]:
    installer = load_model_build(workspace)
    recipe = installer.load_recipe(
        workspace.root / "model-recipes" / MODEL_RECIPES[hardware_profile.model_variant]
    )
    return recipe, installer


def _reusable_active_model(installer: ModuleType, recipe: Any) -> bool:
    active = installer.geer_home() / "models" / "active"
    if not active.is_symlink() or not active.exists():
        return False
    try:
        manifest = json.loads(
            (active.resolve() / installer.BUILD_MANIFEST).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(manifest, dict) and manifest.get("recipe") == recipe.identifier


def model_plan(
    workspace: Workspace,
    *,
    total_memory_bytes: int | None = None,
) -> dict[str, Any]:
    hardware_profile = select_hardware_profile(total_memory_bytes)
    recipe, installer = recipe_and_installer(workspace, hardware_profile)
    distribution = recipe.distribution
    installed_bytes = int(distribution["expected_bytes"])
    cache = installer.default_cache_dir()
    return {
        "profile": MODEL_PROFILE,
        "hardware_profile": hardware_profile.id,
        "display_name": str(recipe.data["display_name"]),
        "repo_id": str(distribution["repo_id"]),
        "revision": str(distribution["revision"]),
        "download_bytes": installed_bytes,
        "download_human": installer.human_bytes(installed_bytes),
        "installed_bytes": installed_bytes,
        "installed_human": installer.human_bytes(installed_bytes),
        "runtime_download_bytes": RUNTIME_ARCHIVE_BYTES,
        "runtime_download_human": installer.human_bytes(RUNTIME_ARCHIVE_BYTES),
        "runtime_installed_bytes": RUNTIME_INSTALLED_BYTES,
        "runtime_installed_human": installer.human_bytes(RUNTIME_INSTALLED_BYTES),
        "temporary_bytes": TEMPORARY_SPACE_BYTES,
        "temporary_human": installer.human_bytes(TEMPORARY_SPACE_BYTES),
        "required_free_bytes": (
            installed_bytes
            + RUNTIME_ARCHIVE_BYTES
            + RUNTIME_INSTALLED_BYTES
            + TEMPORARY_SPACE_BYTES
        ),
        "required_free_human": installer.human_bytes(
            installed_bytes
            + RUNTIME_ARCHIVE_BYTES
            + RUNTIME_INSTALLED_BYTES
            + TEMPORARY_SPACE_BYTES
        ),
        "context_window": hardware_profile.max_context_window,
        "context_window_human": f"{hardware_profile.max_context_window // 1024}K tokens",
        "kv_cache": hardware_profile.kv_cache,
        "reusable_active_model": _reusable_active_model(installer, recipe),
        "cache_dir": str(cache),
        "active_link": str(installer.geer_home() / "models" / "active"),
    }


def install_model(
    workspace: Workspace,
    *,
    high_performance: bool = False,
    total_memory_bytes: int | None = None,
) -> dict[str, Any]:
    hardware_profile = select_hardware_profile(total_memory_bytes)
    recipe, installer = recipe_and_installer(workspace, hardware_profile)
    if high_performance:
        os.environ["HF_XET_HIGH_PERFORMANCE"] = "1"
    installed = installer.install_published(recipe, installer.default_cache_dir())
    assets = initialize_assets(
        workspace,
        installer.geer_home() / "models" / "active",
    )
    model_settings = ensure_model_settings(
        workspace,
        total_memory_bytes=total_memory_bytes,
    )
    return {
        "installed": installed,
        "assets": assets,
        "model_settings": model_settings,
    }


def list_models(workspace: Workspace) -> dict[str, Any]:
    plan = model_plan(workspace)
    active = Path(plan["active_link"])
    installed = active.is_symlink() and active.exists()
    return {
        "models": [
            {
                "id": MODEL_PROFILE,
                "name": plan["display_name"],
                "download": plan["download_human"],
                "installed": installed,
                "active": installed,
            }
        ]
    }


def require_model_name(value: str) -> None:
    if value not in MODEL_NAMES:
        raise AssetError(
            f"unknown model {value!r}; run `geer model list` for available models"
        )


def use_model(workspace: Workspace, value: str) -> dict[str, Any]:
    require_model_name(value)
    active = Path(model_plan(workspace)["active_link"])
    if not active.is_symlink() or not active.exists():
        raise AssetError("the model is not installed; run `geer model add ornith`")
    assets = initialize_assets(workspace, active, DEFAULT_MODEL_ID)
    return {"model": MODEL_PROFILE, "active": True, "assets": assets}


def remove_model(workspace: Workspace, value: str) -> dict[str, Any]:
    require_model_name(value)
    plan = model_plan(workspace)
    active = Path(plan["active_link"])
    was_installed = active.is_symlink() and active.exists()

    linked = workspace.models / DEFAULT_MODEL_ID
    if linked.is_dir() and not linked.is_symlink():
        for entry in linked.iterdir():
            if not entry.is_symlink():
                raise AssetError(f"refusing to remove unmanaged model file: {entry}")
            entry.unlink()
        linked.rmdir()
    workspace.manifest.unlink(missing_ok=True)
    workspace.setup_state.unlink(missing_ok=True)
    if active.is_symlink():
        active.unlink()

    reclaimed = False
    if was_installed:
        cache = Path(plan["cache_dir"])
        try:
            from huggingface_hub import scan_cache_dir

            info = scan_cache_dir(cache)
            for repo in info.repos:
                if repo.repo_id != plan["repo_id"]:
                    continue
                for revision in repo.revisions:
                    if revision.commit_hash == plan["revision"]:
                        info.delete_revisions(revision.commit_hash).execute()
                        reclaimed = True
                        break
        except Exception:
            pass
    return {
        "model": MODEL_PROFILE,
        "removed": True,
        "cache_reclaimed": reclaimed,
    }
