"""Install the reviewed upstream GGUF files without creating conversion provenance."""

from __future__ import annotations

import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .assets import AssetError, _sha256, find_workspace

SAFETY_MARGIN_BYTES = 2 * 1024**3
RECIPE_ID = "qwen3.5-9b-q4-k-m"
MODEL_FILE = "Qwen3.5-9B-Q4_K_M.gguf"
PROJECTOR_FILE = "mmproj-F16.gguf"


@dataclass(frozen=True)
class Recipe:
    path: Path
    data: dict[str, Any]

    @property
    def identifier(self) -> str:
        return str(self.data["id"])

    @property
    def distribution(self) -> dict[str, Any]:
        path = self.path.parent.parent / "model-distributions" / f"{self.identifier}.toml"
        with path.open("rb") as stream:
            value = tomllib.load(stream)
        if value.get("schema_version") != 1 or value.get("recipe") != self.identifier:
            raise AssetError("invalid upstream GGUF distribution manifest")
        return value


def load_recipe(path: Path) -> Recipe:
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    if data.get("schema_version") != 1 or data.get("format") != "gguf":
        raise AssetError("invalid upstream GGUF recipe")
    return Recipe(path.resolve(), data)


def default_recipe() -> Recipe:
    # The installed launcher exports the payload root. PyInstaller's internal
    # directory contains Python modules, while recipes live beside that root.
    root = (
        find_workspace().root
        if os.environ.get("GEER_WORKSPACE")
        else Path(__file__).resolve().parents[2]
    )
    return load_recipe(root / "model-recipes" / f"{RECIPE_ID}.toml")


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def distribution_revision(recipe: Recipe) -> str:
    revision = recipe.distribution.get("revision")
    if not isinstance(revision, str) or re.fullmatch(r"[a-f0-9]{40}", revision) is None:
        raise AssetError("upstream GGUF distribution requires an immutable revision")
    return revision


def published_snapshot_path(recipe: Recipe, cache_dir: Path) -> Path:
    repo_id = str(recipe.distribution["repo_id"])
    if re.fullmatch(r"[\w.-]+/[\w.-]+", repo_id) is None:
        raise AssetError("invalid upstream GGUF repository ID")
    return (
        cache_dir
        / f"models--{repo_id.replace('/', '--')}"
        / "snapshots"
        / distribution_revision(recipe)
    )


def model_files(source: Path, recipe: Recipe | None = None) -> list[Path]:
    recipe = recipe or default_recipe()
    records = recipe.distribution["files"]
    if {row["path"] for row in records} != {MODEL_FILE, PROJECTOR_FILE}:
        raise AssetError(
            "upstream GGUF distribution must preserve the model and F16 vision projector"
        )
    files = []
    for record in records:
        path = source / record["path"]
        if not path.is_file() or path.stat().st_size != record["bytes"]:
            raise AssetError(f"upstream GGUF file is missing or has the wrong size: {path.name}")
        with path.open("rb") as stream:
            if stream.read(4) != b"GGUF":
                raise AssetError(f"invalid GGUF header: {path.name}")
        files.append(path)
    return files


def verify_output(recipe: Recipe, snapshot: Path) -> dict[str, Any]:
    model_files(snapshot, recipe)
    for record in recipe.distribution["files"]:
        if _sha256(snapshot / record["path"]) != record["sha256"]:
            raise AssetError(f"upstream GGUF SHA-256 mismatch: {record['path']}")
    return {"output_dir": str(snapshot), "files": recipe.distribution["files"], "verified": True}


def verify_published_snapshot(recipe: Recipe, snapshot: Path, cache_dir: Path) -> dict[str, Any]:
    expected = published_snapshot_path(recipe, cache_dir)
    if expected.is_symlink() or snapshot.resolve() != expected.resolve():
        raise AssetError("model is not the exact approved upstream GGUF cache snapshot")
    return verify_output(recipe, expected)


def published_install_plan(recipe: Recipe, cache_dir: Path) -> dict[str, Any]:
    # Discovery stays offline. Unverified or partial caches reserve the entire
    # pinned payload; only a full local verification establishes reuse.
    expected = int(recipe.distribution["expected_bytes"])
    return {
        "repo_id": recipe.distribution["repo_id"],
        "revision": distribution_revision(recipe),
        "download_bytes": expected,
        "installed_bytes": expected,
        "download_estimated": True,
        "required_free_bytes": expected + SAFETY_MARGIN_BYTES,
        "cache_dir": str(cache_dir),
    }


def install_published(recipe: Recipe, cache_dir: Path, *, activate: bool = False) -> dict[str, Any]:
    if activate:
        raise AssetError("activate upstream GGUF models through `geer model use` or setup")
    snapshot = published_snapshot_path(recipe, cache_dir)
    try:
        return verify_published_snapshot(recipe, snapshot, cache_dir)
    except (AssetError, OSError):
        pass
    probe = cache_dir
    while not probe.exists():
        probe = probe.parent
    plan = published_install_plan(recipe, cache_dir)
    if shutil.disk_usage(probe).free < plan["required_free_bytes"]:
        raise AssetError(
            f"insufficient free space; need {human_bytes(plan['required_free_bytes'])}"
        )
    from huggingface_hub import hf_hub_download, snapshot_download

    arguments = {
        "repo_id": plan["repo_id"],
        "revision": plan["revision"],
        "cache_dir": cache_dir,
    }
    try:
        downloaded = Path(
            snapshot_download(
                **arguments,
                allow_patterns=[MODEL_FILE, PROJECTOR_FILE],
            )
        )
        if downloaded.resolve() != snapshot.resolve():
            raise AssetError("downloaded GGUF model is outside its approved snapshot")
        # A Hub cache entry can exist with damaged bytes. Repair only the two
        # approved files, preserving every unrelated cache and previous model.
        for record in recipe.distribution["files"]:
            path = snapshot / record["path"]
            if (
                not path.is_file()
                or path.stat().st_size != record["bytes"]
                or _sha256(path) != record["sha256"]
            ):
                hf_hub_download(**arguments, filename=record["path"], force_download=True)
        return verify_published_snapshot(recipe, snapshot, cache_dir)
    except AssetError:
        raise
    except Exception as error:
        raise AssetError(f"cannot download or verify the upstream GGUF model: {error}") from error


def asset_metadata(recipe: Recipe | None = None) -> dict[str, Any]:
    recipe = recipe or default_recipe()
    return {
        "recipe": recipe.identifier,
        "display_name": recipe.data["display_name"],
        "format": "gguf",
        "producer": "Unsloth",
        "source": dict(recipe.distribution),
        "conversion": "Third-party upstream conversion; no Geer conversion performed.",
    }
