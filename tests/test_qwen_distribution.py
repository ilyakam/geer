from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from geer import models
from geer import qwen_distribution as qwen
from geer.assets import (
    AssetError,
    Workspace,
    initialize_assets,
    model_alias,
    model_family,
    verify_assets,
)
from geer.hardware import GIB


@pytest.fixture
def qwen_snapshot(tmp_path, monkeypatch):
    recipe = qwen.default_recipe()
    distribution = copy.deepcopy(recipe.distribution)
    payloads = {qwen.MODEL_FILE: b"GGUFmodel", qwen.PROJECTOR_FILE: b"GGUFvision"}
    for record in distribution["files"]:
        payload = payloads[record["path"]]
        record.update(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    distribution["expected_bytes"] = sum(len(value) for value in payloads.values())
    monkeypatch.setattr(qwen.Recipe, "distribution", property(lambda _: distribution))
    workspace = Workspace(Path(__file__).parents[1], tmp_path / "home")
    cache = workspace.cache / "huggingface"
    snapshot = qwen.published_snapshot_path(recipe, cache)
    snapshot.mkdir(parents=True)
    for name, payload in payloads.items():
        (snapshot / name).write_bytes(payload)
    models._VERIFIED.clear()
    return SimpleNamespace(
        recipe=recipe,
        distribution=distribution,
        payloads=payloads,
        workspace=workspace,
        cache=cache,
        snapshot=snapshot,
    )


def test_pinned_upstream_distribution_retains_exact_model_and_full_vision():
    recipe = qwen.default_recipe()
    distribution = recipe.distribution
    assert distribution["repo_id"] == "unsloth/Qwen3.5-9B-GGUF"
    assert distribution["revision"] == "3885219b6810b007914f3a7950a8d1b469d598a5"
    assert distribution["expected_bytes"] == 6_598_688_544
    assert distribution["license"] == "Apache-2.0"
    assert {row["path"] for row in distribution["files"]} == {qwen.MODEL_FILE, qwen.PROJECTOR_FILE}
    assert recipe.data["output"]["preserve_multimodal"] is True
    assert recipe.data["conversion"]["producer"] == "Unsloth"


def test_frozen_recipe_uses_validated_launcher_payload_root(tmp_path, monkeypatch):
    source = qwen.default_recipe().path.parent.parent
    payload = tmp_path / "app"
    payload.mkdir()
    (payload / "pyproject.toml").write_text("")
    (payload / "AGENTS.md").write_text("")
    for directory in ("model-recipes", "model-distributions"):
        (payload / directory).mkdir()
        shutil.copyfile(
            source / directory / f"{qwen.RECIPE_ID}.toml",
            payload / directory / f"{qwen.RECIPE_ID}.toml",
        )
    monkeypatch.setattr(sys, "_MEIPASS", str(payload / "_internal"), raising=False)
    monkeypatch.setenv("GEER_WORKSPACE", str(payload))
    recipe = qwen.default_recipe()
    assert recipe.path == payload / "model-recipes" / f"{qwen.RECIPE_ID}.toml"
    assert recipe.distribution["producer"] == "Unsloth"


def test_gguf_assets_preserve_images_and_upstream_attribution_without_build_file(qwen_snapshot):
    env = qwen_snapshot
    manifest = initialize_assets(env.workspace, env.snapshot)
    assert manifest["format"] == "gguf"
    assert manifest["backend"] == "llama.cpp"
    assert model_family(manifest) == "qwen"
    assert model_alias(manifest) == env.recipe.data["display_name"]
    assert manifest["input_modalities"] == ["text", "image"]
    assert manifest["build"]["producer"] == "Unsloth"
    assert manifest["build"]["source"]["revision"] == env.distribution["revision"]
    assert not (env.snapshot / "geer-build-manifest.json").exists()
    assert verify_assets(env.workspace, verify_hashes=True)["input_modalities"] == ["text", "image"]
    (env.snapshot / qwen.PROJECTOR_FILE).unlink()
    with pytest.raises((AssetError, FileNotFoundError)):
        verify_assets(env.workspace)


def test_corrupt_gguf_hash_and_unapproved_snapshot_cannot_be_reused(qwen_snapshot):
    env = qwen_snapshot
    assert qwen.verify_published_snapshot(env.recipe, env.snapshot, env.cache)["verified"]
    (env.snapshot / qwen.MODEL_FILE).write_bytes(b"GGUFwrong")
    with pytest.raises(AssetError, match="SHA-256"):
        qwen.verify_published_snapshot(env.recipe, env.snapshot, env.cache)
    with pytest.raises(AssetError, match="exact approved"):
        qwen.verify_published_snapshot(env.recipe, env.snapshot.parent / "other", env.cache)


def test_discovery_plan_is_offline_and_does_not_trust_existing_files(qwen_snapshot, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("metadata discovery must stay offline")

    monkeypatch.setitem(
        sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=no_network)
    )
    env = qwen_snapshot
    plan = qwen.published_install_plan(env.recipe, env.cache)
    assert plan["download_bytes"] == env.distribution["expected_bytes"]
    assert plan["download_estimated"] is True


def test_downloads_only_approved_files_and_repairs_corruption(qwen_snapshot, monkeypatch):
    env = qwen_snapshot
    (env.snapshot / qwen.MODEL_FILE).write_bytes(b"GGUFwrong")
    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        return env.snapshot

    def repair(**kwargs):
        calls.append(kwargs)
        target = env.snapshot / kwargs["filename"]
        target.write_bytes(env.payloads[target.name])
        return target

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(
            snapshot_download=download,
            hf_hub_download=repair,
        ),
    )
    monkeypatch.setattr(qwen.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * GIB))
    assert qwen.install_published(env.recipe, env.cache)["verified"]
    assert calls[0]["allow_patterns"] == [qwen.MODEL_FILE, qwen.PROJECTOR_FILE]
    assert calls[1]["filename"] == qwen.MODEL_FILE
    assert calls[1]["force_download"] is True


def test_activation_rolls_back_qwen_links_assets_and_confirmed_choice(qwen_snapshot, monkeypatch):
    env = qwen_snapshot
    previous = env.workspace.models / "old"
    previous.mkdir(parents=True)
    env.workspace.models.joinpath("active").symlink_to(previous)
    env.workspace.state.mkdir(parents=True)
    env.workspace.manifest.write_text('{"original":true}\n')
    selection = models._selection_path(env.workspace)
    selection.write_text('{"schema_version":1,"family":"sage","confirmed":true}\n')
    monkeypatch.setattr(models, "running_t3_applications", lambda: [])
    monkeypatch.setattr(models, "stop_server", lambda _: None)

    def fail_settings(*args, **kwargs):
        raise AssetError("settings failure")

    monkeypatch.setattr("geer.llama_runtime.ensure_model_settings", fail_settings)
    with pytest.raises(AssetError, match="settings failure"):
        models._activate(
            env.workspace,
            {"family": "qwen"},
            env.recipe,
            qwen,
            env.snapshot,
            16 * GIB,
        )
    assert env.workspace.models.joinpath("active").resolve() == previous
    assert not env.workspace.models.joinpath("previous").exists()
    assert json.loads(env.workspace.manifest.read_text()) == {"original": True}
    assert json.loads(selection.read_text())["family"] == "sage"
    assert env.snapshot.joinpath(qwen.PROJECTOR_FILE).exists()


def test_successful_qwen_activation_saves_choice_and_exact_snapshot_reuse(
    qwen_snapshot, monkeypatch
):
    env = qwen_snapshot
    monkeypatch.setattr(models, "running_t3_applications", lambda: [])
    monkeypatch.setattr("geer.llama_runtime.ensure_model_settings", lambda *a, **k: {})
    result = models._activate(
        env.workspace,
        {"family": "qwen"},
        env.recipe,
        qwen,
        env.snapshot,
        16 * GIB,
    )
    assert result["active"] is True
    assert models._saved_family(env.workspace) == "qwen"
    again = models._activate(
        env.workspace,
        {"family": "qwen"},
        env.recipe,
        qwen,
        env.snapshot,
        16 * GIB,
    )
    assert again["reused"] is True
    assert verify_assets(env.workspace)["input_modalities"] == ["text", "image"]


def test_catalog_reuses_verified_qwen_and_download_does_not_activate(qwen_snapshot, monkeypatch):
    env = qwen_snapshot
    monkeypatch.setattr(
        models,
        "_dependency_plan",
        lambda _: {
            f"{name}_{field}": 0
            for name in ("runtime", "pi", "t3")
            for field in ("download_bytes", "required_free_bytes", "reusable")
        },
    )
    monkeypatch.setattr("geer.llama_runtime.dependency_plan", lambda _: {})
    models._confirm_choice(env.workspace, "ornith")
    plan = models.model_plan(env.workspace, family="qwen", total_memory_bytes=16 * GIB)
    assert plan["installed"] is True
    assert plan["download_bytes"] == 0
    result = models.install_model(
        env.workspace,
        family="qwen",
        activate=False,
        total_memory_bytes=16 * GIB,
    )
    assert result["activated"] is False
    assert models._saved_family(env.workspace) == "ornith"
    assert not env.workspace.models.joinpath("active").exists()
    (env.snapshot / qwen.MODEL_FILE).write_bytes(b"GGUFwrong")
    broken = models.model_plan(env.workspace, family="qwen", total_memory_bytes=16 * GIB)
    assert broken["installed"] is False
    assert broken["download_bytes"] == env.distribution["expected_bytes"]
