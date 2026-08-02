from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from geer.assets import (
    DEFAULT_MODEL_ALIAS,
    AssetError,
    Workspace,
    find_workspace,
    initialize_assets,
    model_alias,
    verify_assets,
)


def test_missing_asset_manifest_reports_incomplete_account_setup(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)

    with pytest.raises(AssetError, match="setup is incomplete for this account"):
        verify_assets(workspace)


def test_find_workspace_uses_launcher_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "geer"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("")
    (workspace / "AGENTS.md").write_text("")
    monkeypatch.setenv("GEER_WORKSPACE", str(workspace))

    assert find_workspace(tmp_path).root == workspace


def test_workspace_cache_is_under_runtime_home(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "source", tmp_path / "home")

    assert workspace.cache == tmp_path / "home/cache"


def make_model(path: Path) -> Path:
    path.mkdir()
    files = {
        "geer-build-manifest.json": json.dumps(
            {
                "schema_version": 1,
                "recipe": "test-6bit",
                "display_name": "Geer Test Model (6-bit MLX)",
                "source": {
                    "repo_id": "example/test",
                    "revision": "abc123",
                    "license": "MIT",
                },
            }
        ),
        "chat_template.jinja": "{{ messages }}",
        "config.json": json.dumps(
            {
                "architectures": ["TestForCausalLM"],
                "model_type": "test",
                "max_position_embeddings": 4096,
                "quantization": {"bits": 4, "group_size": 64, "mode": "affine"},
            }
        ),
        "model-00001-of-00001.safetensors": "weights",
        "model.safetensors.index.json": json.dumps(
            {
                "metadata": {"total_size": 7},
                "weight_map": {"model.layers.0.weight": "model-00001-of-00001.safetensors"},
            }
        ),
        "tokenizer.json": "{}",
        "tokenizer_config.json": "{}",
    }
    for name, content in files.items():
        (path / name).write_text(content)
    (path / "prefix_cache.safetensors").write_text("private")
    return path


def test_initialize_assets_links_only_selected_model_files(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    source = make_model(tmp_path / "source")

    manifest = initialize_assets(workspace, source, "local")

    linked_model = workspace.models / "local"
    assert (linked_model / "config.json").is_symlink()
    assert not (linked_model / "prefix_cache.safetensors").exists()
    assert manifest["indexed_weight_bytes"] == 7
    assert manifest["build"]["recipe"] == "test-6bit"
    assert manifest["files"][0]["sha256"]
    assert verify_assets(workspace)["model_id"] == "local"


def test_initialize_assets_retargets_managed_symlinks_for_a_new_model(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    source = make_model(tmp_path / "source")
    other = tmp_path / "other"
    other.write_text("other")
    target = workspace.models / "local"
    target.mkdir(parents=True)
    (target / "config.json").symlink_to(other)
    (target / "stale.json").symlink_to(other)

    initialize_assets(workspace, source, "local")

    assert (target / "config.json").resolve() == (source / "config.json").resolve()
    assert not (target / "stale.json").exists()
    assert verify_assets(workspace)["source"] == str(source.resolve())


def test_initialize_assets_refuses_unmanaged_files(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    source = make_model(tmp_path / "source")
    target = workspace.models / "local"
    target.mkdir(parents=True)
    (target / "notes.txt").write_text("keep me")

    with pytest.raises(AssetError, match="unmanaged entries: notes.txt"):
        initialize_assets(workspace, source, "local")


def test_verify_assets_accepts_hugging_face_cache_symlinks(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    cache = tmp_path / "cache"
    cache.mkdir()
    source = make_model(cache / "snapshot")
    blob = cache / "blobs" / "chat-template"
    blob.parent.mkdir()
    blob.write_text((source / "chat_template.jinja").read_text())
    (source / "chat_template.jinja").unlink()
    (source / "chat_template.jinja").symlink_to(blob)

    initialize_assets(workspace, source, "local")

    assert verify_assets(workspace)["model_id"] == "local"
    assert initialize_assets(workspace, source, "local")["model_id"] == "local"


def test_manifest_hash_matches_source(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    source = make_model(tmp_path / "source")

    manifest = initialize_assets(workspace, source, "local")

    shard = next(item for item in manifest["files"] if item["name"].endswith(".safetensors"))
    assert shard["sha256"] == hashlib.sha256(b"weights").hexdigest()


def test_full_verification_detects_same_size_corruption(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    source = make_model(tmp_path / "source")
    initialize_assets(workspace, source, "local")
    weights = source / "model-00001-of-00001.safetensors"
    weights.write_text("changed")

    assert verify_assets(workspace)["model_id"] == "local"
    with pytest.raises(AssetError, match="asset hash changed"):
        verify_assets(workspace, verify_hashes=True)


def test_model_alias_reports_quantization() -> None:
    assert model_alias({"build": {"display_name": "Geer Built Model"}}) == "Geer Built Model"
    assert model_alias({"quantization": {"bits": 6}}).endswith("(6-bit MLX)")
    assert model_alias({}) == DEFAULT_MODEL_ALIAS
