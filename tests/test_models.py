from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import geer.models as models
from geer.assets import AssetError, Workspace, verify_assets
from geer.hardware import GIB, select_hardware_profile


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    workspace = Workspace(Path(__file__).parents[1], tmp_path / "home")
    monkeypatch.setenv("GEER_HOME", str(workspace.runtime))
    monkeypatch.setattr("geer.hardware.memory_bytes", lambda: 128 * GIB)
    installer = models.load_model_build(workspace)
    load_recipe = installer.load_recipe
    published_plan = installer.published_install_plan

    def recipe(path):
        original = load_recipe(path)
        data = copy.deepcopy(original.data)
        family = "sage" if path.name.startswith("sage") else "ornith"
        variant = "4-8bit" if "4-8bit" in path.name else "6bit"
        data["distribution"] = {
            "repo_id": f"example/{family}-{variant}",
            "revision": ("a" if family == "sage" else "b") * 40,
            "expected_bytes": 100,
        }
        return installer.Recipe(original.path, data)

    monkeypatch.setattr(installer, "load_recipe", recipe)
    monkeypatch.setattr(models, "load_model_build", lambda _: installer)
    monkeypatch.setattr(
        installer,
        "published_install_plan",
        lambda *args: {
            "download_bytes": 17,
            "installed_bytes": 100,
        },
    )
    monkeypatch.setattr(models, "running_t3_applications", lambda: [])

    def no_pi(*a, **k):
        raise AssetError("not installed")

    monkeypatch.setattr(models, "compatible_pi", no_pi)
    monkeypatch.setattr(models, "find_t3", lambda: SimpleNamespace(found=False))
    monkeypatch.delenv("GEER_RUNTIME_ARCHIVE", raising=False)
    monkeypatch.delenv("GEER_PI_BIN", raising=False)
    monkeypatch.setattr(models, "_verified_archive", lambda *a: False)
    stopped = []
    monkeypatch.setattr(models, "stop_server", lambda _: stopped.append(True))
    models._VERIFIED.clear()
    cache = workspace.cache / "huggingface"

    def make_snapshot(family, variant="6bit", *, revision=None, candidate=False):
        prefix = models.MODEL_FAMILIES[family]
        model_recipe = recipe(workspace.root / "model-recipes" / f"{prefix}-{variant}.toml")
        target = installer.published_snapshot_path(model_recipe, cache)
        if revision:
            target = target.with_name(revision)
        if candidate:
            target = workspace.models / "candidates" / model_recipe.identifier
        target.mkdir(parents=True)
        quantization = {
            "bits": model_recipe.quantization["bits"],
            "group_size": 64,
            "mode": "affine",
        }
        if variant == "4-8bit":
            quantization.update(
                {
                    "language_model.layers.0.mlp.switch_mlp.gate_proj": {
                        "bits": 4,
                        "group_size": 64,
                    },
                    "language_model.layers.0.self_attn.q_proj": {"bits": 8, "group_size": 64},
                }
            )
        files = {
            "config.json": json.dumps(
                {
                    "model_type": "qwen3_5_moe",
                    "max_position_embeddings": 262_144,
                    "vision_config": {"model_type": "qwen3_5"},
                    "quantization": quantization,
                }
            ),
            "chat_template.jinja": "{{ messages }}",
            "tokenizer.json": "{}",
            "tokenizer_config.json": "{}",
            "weights.safetensors": "weights",
            "model.safetensors.index.json": json.dumps(
                {
                    "weight_map": {
                        "vision_tower.blocks.0.weight": "weights.safetensors",
                        "language_model.layers.0.weight": "weights.safetensors",
                    },
                }
            ),
        }
        for name, value in files.items():
            (target / name).write_text(value)
        records = installer.hash_files(target)
        (target / installer.BUILD_MANIFEST).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "recipe": model_recipe.identifier,
                    "display_name": model_recipe.data["display_name"],
                    "source": model_recipe.source,
                    "quantization": model_recipe.quantization,
                    "files": records,
                    "bytes": sum(item["bytes"] for item in records),
                }
            )
        )
        return target

    def activate(target):
        workspace.models.mkdir(parents=True, exist_ok=True)
        active = workspace.models / "active"
        active.unlink(missing_ok=True)
        active.symlink_to(target)

    def save(family, confirmed=True):
        workspace.state.mkdir(parents=True, exist_ok=True)
        models._selection_path(workspace).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "family": family,
                    "confirmed": confirmed,
                }
            )
        )

    return SimpleNamespace(
        workspace=workspace,
        installer=installer,
        make=make_snapshot,
        activate=activate,
        save=save,
        stopped=stopped,
        cache=cache,
        published_plan=published_plan,
    )


def row(catalog, family):
    return next(value for value in catalog["models"] if value["family"] == family)


@pytest.mark.parametrize("family", ("sage", "ornith"))
@pytest.mark.parametrize(
    "memory,variant,context",
    (
        (32, "4-8bit", 65_536),
        (48, "4-8bit", 131_072),
        (64, "6bit", 262_144),
        (512, "6bit", 262_144),
    ),
)
def test_both_families_use_the_hardware_selected_recipe(
    inventory, family, memory, variant, context
):
    plan = models.model_plan(inventory.workspace, family=family, total_memory_bytes=memory * GIB)
    assert plan["id"] == plan["family"] == plan["profile"] == family
    assert plan["model_variant"] == variant
    assert plan["context_window"] == context
    assert plan["hardware_profile"].startswith(family)
    assert plan["eligible"] is True
    assert plan["installed"] is False
    assert plan["active"] is False
    assert plan["recommended"] is (family == "sage")
    assert plan["kv_cache"] == "BF16"


def test_fresh_catalog_offers_both_families_and_recommends_sage(inventory):
    catalog = models.model_catalog(inventory.workspace)
    assert catalog["selected"] == "sage"
    assert catalog["selection_source"] == "recommended"
    assert [value["id"] for value in catalog["models"]] == ["sage", "ornith"]
    assert not inventory.workspace.state.exists()


@pytest.mark.parametrize(
    "cached,selected",
    (((), "sage"), (("ornith",), "ornith"), (("sage",), "sage"), (("sage", "ornith"), "sage")),
)
def test_only_one_verified_current_cached_family_changes_the_default(inventory, cached, selected):
    for family in cached:
        inventory.make(family)
    catalog = models.model_catalog(inventory.workspace)
    assert catalog["selected"] == selected
    assert catalog["selection_source"] == ("cached" if len(cached) == 1 else "recommended")
    assert not inventory.workspace.state.exists()


def test_confirmed_choice_precedes_active_and_cache_but_explicit_family_wins(inventory):
    inventory.activate(inventory.make("ornith"))
    inventory.save("sage")
    catalog = models.model_catalog(inventory.workspace)
    assert catalog["selected"] == "sage"
    assert catalog["selection_source"] == "saved"
    assert row(catalog, "sage")["installed"] is False
    assert models.model_plan(inventory.workspace, family="ornith")["family"] == "ornith"
    assert models._saved_family(inventory.workspace) == "sage"


@pytest.mark.parametrize("saved", ("unconfirmed", "unknown", "corrupt", "list"))
def test_invalid_saved_choice_is_ignored(inventory, saved):
    inventory.make("ornith")
    inventory.save(
        "ornith" if saved == "unconfirmed" else "unknown", confirmed=saved != "unconfirmed"
    )
    if saved in {"corrupt", "list"}:
        models._selection_path(inventory.workspace).write_text("[" if saved == "corrupt" else "[]")
    catalog = models.model_catalog(inventory.workspace)
    assert catalog["selected"] == "ornith"
    assert catalog["selection_source"] == "cached"


@pytest.mark.parametrize("kind", ("candidate", "stale", "wrong_variant", "corrupt", "missing"))
def test_unverified_or_incompatible_folders_do_not_count_as_installed(inventory, kind):
    kwargs = {"candidate": True} if kind == "candidate" else {}
    if kind == "stale":
        kwargs["revision"] = "c" * 40
    if kind == "wrong_variant":
        kwargs["variant"] = "4-8bit"
    snapshot = inventory.make("ornith", **kwargs)
    if kind == "corrupt":
        (snapshot / "weights.safetensors").write_text("changed")
    if kind == "missing":
        (snapshot / "tokenizer.json").unlink()
    catalog = models.model_catalog(inventory.workspace)
    assert catalog["selected"] == "sage"
    assert row(catalog, "ornith")["installed"] is False
    assert row(catalog, "ornith")["download_bytes"] == 17


def test_verified_active_family_precedes_both_caches(inventory):
    inventory.make("sage")
    inventory.activate(inventory.make("ornith"))
    catalog = models.model_catalog(inventory.workspace)
    assert catalog["selected"] == "ornith"
    assert catalog["selection_source"] == "legacy_active"
    assert row(catalog, "ornith")["installed"] is True
    assert row(catalog, "ornith")["active"] is True
    assert row(catalog, "ornith")["download_bytes"] == 0
    assert row(catalog, "ornith")["activation_changes"] is False


def test_verified_legacy_active_is_only_a_family_hint(inventory):
    inventory.make("sage")
    inventory.activate(inventory.make("ornith", candidate=True))
    catalog = models.model_catalog(inventory.workspace)
    assert catalog["selected"] == "ornith"
    assert catalog["selection_source"] == "legacy_active"
    ornith = row(catalog, "ornith")
    assert ornith["installed"] is False
    assert ornith["active"] is False
    assert ornith["activation_changes"] is True
    assert ornith["active_model_installed"] is True
    assert ornith["download_bytes"] == 17


def test_corrupt_legacy_active_is_not_a_preference_hint(inventory):
    target = inventory.make("ornith", candidate=True)
    (target / "weights.safetensors").write_text("changed")
    inventory.activate(target)
    assert models.model_catalog(inventory.workspace)["selected"] == "sage"


def test_verified_cache_is_not_rehashed_on_each_catalog_read_and_mutation_invalidates(
    inventory, monkeypatch
):
    target = inventory.make("ornith")
    calls = []
    verify = inventory.installer.verify_output
    monkeypatch.setattr(
        inventory.installer, "verify_output", lambda *a: calls.append(True) or verify(*a)
    )
    assert row(models.model_catalog(inventory.workspace), "ornith")["installed"] is True
    assert row(models.model_catalog(inventory.workspace), "ornith")["installed"] is True
    assert calls == [True]
    shard = target / "weights.safetensors"
    stat = shard.stat()
    shard.write_text("changed")
    os.utime(shard, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert row(models.model_catalog(inventory.workspace), "ornith")["installed"] is False
    assert calls == [True, True]


def test_partial_download_plan_reports_only_missing_bytes(inventory):
    plan = models.model_plan(inventory.workspace)
    assert plan["download_bytes"] == 17
    assert plan["installed_bytes"] == 100
    assert plan["download_estimated"] is False
    inventory.make("sage")
    cached = models.model_plan(inventory.workspace, family="sage")
    assert cached["download_bytes"] == 0
    assert cached["installed_bytes"] == 100
    assert plan["required_free_bytes"] - cached["required_free_bytes"] == (
        17 + inventory.installer.SAFETY_MARGIN_BYTES
    )


def test_offline_plan_is_an_explicit_estimate_not_an_installation(inventory, monkeypatch):
    def offline(*args):
        raise inventory.installer.BuildError("offline")

    monkeypatch.setattr(inventory.installer, "published_install_plan", offline)
    plan = models.model_plan(inventory.workspace)
    assert plan["download_bytes"] == plan["installed_bytes"] == 100
    assert plan["download_estimated"] is True
    assert plan["installed"] is False


def test_unsupported_hardware_marks_both_families_ineligible_and_preserves_state(inventory):
    inventory.save("ornith")
    catalog = models.model_catalog(inventory.workspace, total_memory_bytes=16 * GIB)
    assert all(not value["eligible"] for value in catalog["models"])
    assert all("32 GB" in value["reason"] for value in catalog["models"])
    with pytest.raises(AssetError, match="32 GB"):
        models.install_model(inventory.workspace, family="ornith", total_memory_bytes=16 * GIB)
    assert models._saved_family(inventory.workspace) == "ornith"


@pytest.mark.parametrize("family", ("sage", "ornith"))
@pytest.mark.parametrize("name_kind", ("recipe", "display_name", "repo_id"))
def test_explicit_variant_alias_cannot_bypass_hardware(inventory, family, name_kind):
    recipe, _ = models.recipe_and_installer(
        inventory.workspace, select_hardware_profile(128 * GIB), family=family
    )
    name = {
        "recipe": recipe.identifier,
        "display_name": recipe.data["display_name"],
        "repo_id": recipe.distribution["repo_id"],
    }[name_kind]
    # The synthetic repo name is not a public alias, so use the actual public name.
    if name_kind == "repo_id":
        label = "Sage-A1.5" if family == "sage" else "Ornith-1.5"
        name = f"ilyakam/Geer-{label}-35B-A3B-6bit-MLX"
    models.require_model_name(name)
    with pytest.raises(AssetError, match="incompatible"):
        models.install_model(inventory.workspace, family=name, total_memory_bytes=32 * GIB)
    with pytest.raises(AssetError, match="incompatible"):
        models.use_model(inventory.workspace, name, total_memory_bytes=32 * GIB)


def test_unknown_family_is_rejected():
    with pytest.raises(AssetError, match="unknown model"):
        models.require_model_name("third-model")


def test_download_only_add_preserves_active_preference_assets_settings_and_history(
    inventory, monkeypatch
):
    previous = inventory.make("ornith")
    inventory.activate(previous)
    inventory.save("ornith")
    inventory.workspace.manifest.write_text("existing assets")
    inventory.workspace.omlx_model_settings.parent.mkdir()
    inventory.workspace.omlx_model_settings.write_text("existing settings")
    history = inventory.workspace.pi_config / "sessions" / "history.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text("keep history")
    before = {
        path: path.read_bytes()
        for path in (
            inventory.workspace.manifest,
            inventory.workspace.omlx_model_settings,
            models._selection_path(inventory.workspace),
            history,
        )
    }
    acquired = []

    def download(recipe, cache, *, activate):
        assert recipe.identifier == "sage-a1.5-35b-6bit"
        assert activate is False
        assert (inventory.workspace.models / "active").resolve() == previous
        acquired.append(True)
        return {"verified": True, "output_dir": str(inventory.make("sage"))}

    monkeypatch.setattr(inventory.installer, "install_published", download)
    monkeypatch.setattr(models, "running_t3_applications", lambda: pytest.fail("T3 guard"))
    result = models.install_model(inventory.workspace, family="sage", activate=False)
    assert acquired == [True]
    assert result["activated"] is False
    assert inventory.stopped == []
    assert (inventory.workspace.models / "active").resolve() == previous
    assert {path: path.read_bytes() for path in before} == before


def test_initial_activation_can_run_with_t3_open_and_saves_choice_after_success(
    inventory, monkeypatch
):
    target = inventory.make("ornith")
    monkeypatch.setattr(models, "running_t3_applications", lambda: pytest.fail("initial T3 guard"))
    result = models.install_model(inventory.workspace, family="ornith")
    assert result["model"] == "ornith"
    assert result["activated"] is True
    assert models._saved_family(inventory.workspace) == "ornith"
    assert (inventory.workspace.models / "active").resolve() == target
    assert inventory.stopped == []
    assert "Ornith" in result["model_settings"]["models"]["geer-local"]["model_alias"]


def test_use_requires_quit_for_an_existing_active_target_and_never_downloads(
    inventory, monkeypatch
):
    previous = inventory.make("ornith")
    inventory.activate(previous)
    inventory.make("sage")
    monkeypatch.setattr(models, "running_t3_applications", lambda: [Path("/Applications/T3.app")])
    monkeypatch.setattr(
        inventory.installer, "install_published", lambda *a, **k: pytest.fail("download")
    )
    with pytest.raises(AssetError, match="Finish your chats and quit T3 Code"):
        models.use_model(inventory.workspace, "sage")
    assert inventory.stopped == []
    assert (inventory.workspace.models / "active").resolve() == previous
    assert not models._selection_path(inventory.workspace).exists()


def test_install_rejects_t3_before_download_when_existing_activation_would_change(
    inventory, monkeypatch
):
    inventory.activate(inventory.make("ornith"))
    monkeypatch.setattr(models, "running_t3_applications", lambda: [Path("T3.app")])
    monkeypatch.setattr(
        inventory.installer, "install_published", lambda *a, **k: pytest.fail("download")
    )
    with pytest.raises(AssetError, match="quit T3 Code"):
        models.install_model(inventory.workspace, family="sage")


def test_same_verified_active_artifact_is_noop_even_with_t3_open(inventory, monkeypatch):
    inventory.make("ornith")
    models.use_model(inventory.workspace, "ornith")
    before = inventory.workspace.omlx_model_settings.read_bytes()
    monkeypatch.setattr(models, "running_t3_applications", lambda: pytest.fail("same-model guard"))
    result = models.use_model(inventory.workspace, "ornith")
    assert result["reused"] is True
    assert inventory.stopped == []
    assert inventory.workspace.omlx_model_settings.read_bytes() == before


def test_family_change_retains_previous_model_shared_cache_and_histories(inventory):
    old = inventory.make("ornith")
    new = inventory.make("sage")
    models.use_model(inventory.workspace, "ornith")
    history = inventory.workspace.pi_config / "sessions" / "history.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text("history")
    result = models.use_model(inventory.workspace, "sage")
    assert result["active"] is True
    assert inventory.stopped == [True]
    assert (inventory.workspace.models / "active").resolve() == new
    assert (inventory.workspace.models / "previous").resolve() == old
    assert history.read_text() == "history"
    assert old.is_dir()
    assert models._saved_family(inventory.workspace) == "sage"


@pytest.mark.parametrize("failure", ("assets", "settings", "choice"))
def test_activation_failure_restores_links_assets_settings_and_choice(
    inventory, monkeypatch, failure
):
    old = inventory.make("ornith")
    new = inventory.make("sage")
    models.use_model(inventory.workspace, "ornith")
    original = {
        path: path.read_bytes()
        for path in (
            inventory.workspace.manifest,
            inventory.workspace.omlx_model_settings,
            models._selection_path(inventory.workspace),
        )
    }
    links = {
        path.name: os.readlink(path)
        for path in (inventory.workspace.models / "geer-local").iterdir()
    }
    method = {
        "assets": "initialize_assets",
        "settings": "ensure_model_settings",
        "choice": "_confirm_choice",
    }[failure]

    def fail(*a, **k):
        raise AssetError("synthetic activation failure")

    monkeypatch.setattr(models, method, fail)
    with pytest.raises(AssetError, match="synthetic"):
        models.use_model(inventory.workspace, "sage")
    assert (inventory.workspace.models / "active").resolve() == old
    assert not (inventory.workspace.models / "previous").exists()
    assert {path: path.read_bytes() for path in original} == original
    assert {
        path.name: os.readlink(path)
        for path in (inventory.workspace.models / "geer-local").iterdir()
    } == links
    assert new.is_dir()
    assert verify_assets(inventory.workspace)["source"] == str(old)


def test_failed_acquisition_preserves_existing_active_choice(inventory, monkeypatch):
    old = inventory.make("ornith")
    models.use_model(inventory.workspace, "ornith")

    def fail(*a, **k):
        raise inventory.installer.BuildError("synthetic download failure")

    monkeypatch.setattr(inventory.installer, "install_published", fail)
    with pytest.raises(inventory.installer.BuildError, match="download failure"):
        models.install_model(inventory.workspace, family="sage")
    assert (inventory.workspace.models / "active").resolve() == old
    assert models._saved_family(inventory.workspace) == "ornith"
    assert inventory.stopped == []


def test_remove_inactive_rollback_family_preserves_active_runtime_and_shared_cache(
    inventory, monkeypatch
):
    old = inventory.make("ornith")
    inventory.make("sage")
    models.use_model(inventory.workspace, "ornith")
    models.use_model(inventory.workspace, "sage")
    stopped = inventory.stopped.copy()
    manifest = inventory.workspace.manifest.read_bytes()
    monkeypatch.setattr(models, "running_t3_applications", lambda: pytest.fail("inactive guard"))
    monkeypatch.setattr(
        "huggingface_hub.scan_cache_dir", lambda *a: pytest.fail("rollback deletion")
    )
    result = models.remove_model(inventory.workspace, "ornith")
    assert result["cache_retained_for_rollback"] is True
    assert old.is_dir()
    assert inventory.stopped == stopped
    assert inventory.workspace.manifest.read_bytes() == manifest
    assert models._saved_family(inventory.workspace) == "sage"


def test_remove_active_family_stops_before_unlink_and_deletes_only_requested_revision(
    inventory, monkeypatch
):
    target = inventory.make("ornith")
    other = inventory.make("sage")
    models.use_model(inventory.workspace, "ornith")
    deleted = []
    repo = SimpleNamespace(
        repo_id="example/ornith-6bit", revisions=[SimpleNamespace(commit_hash="b" * 40)]
    )
    info = SimpleNamespace(
        repos=[repo],
        delete_revisions=lambda revision: SimpleNamespace(execute=lambda: deleted.append(revision)),
    )
    monkeypatch.setattr("huggingface_hub.scan_cache_dir", lambda *a: info)

    def stop(_):
        assert (inventory.workspace.models / "active").resolve() == target
        inventory.stopped.append(True)

    monkeypatch.setattr(models, "stop_server", stop)
    result = models.remove_model(inventory.workspace, "ornith")
    assert result["cache_reclaimed"] is True
    assert deleted == ["b" * 40]
    assert inventory.stopped == [True]
    assert not (inventory.workspace.models / "active").is_symlink()
    assert other.is_dir()


def test_remove_refuses_unmanaged_assets_before_stopping_runtime(inventory):
    inventory.make("ornith")
    models.use_model(inventory.workspace, "ornith")
    unmanaged = inventory.workspace.models / "geer-local" / "notes.txt"
    unmanaged.write_text("retain")
    with pytest.raises(AssetError, match="unmanaged"):
        models.remove_model(inventory.workspace, "ornith")
    assert unmanaged.read_text() == "retain"
    assert inventory.stopped == []


@pytest.mark.parametrize("family", ("sage", "ornith"))
@pytest.mark.parametrize("memory", (32, 128))
def test_release_recipes_have_real_pins_and_retain_images(tmp_path, monkeypatch, family, memory):
    monkeypatch.setenv("GEER_HOME", str(tmp_path / "home"))
    workspace = Workspace(Path(__file__).parents[1], tmp_path / "home")
    recipe, installer = models.recipe_and_installer(
        workspace,
        select_hardware_profile(memory * GIB, family=family),
        family=family,
    )
    assert installer.distribution_revision(recipe)
    assert recipe.distribution["expected_bytes"] > 0
    assert recipe.output["preserve_multimodal"] is True


def test_existing_shared_dependencies_reduce_additional_space_without_changing_footprint(
    inventory,
    monkeypatch,
):
    before = models.model_plan(inventory.workspace, family="sage")
    executable = inventory.workspace.runtime / "omlx/bin/omlx"
    executable.parent.mkdir(parents=True)
    executable.write_text("synthetic executable")
    (inventory.workspace.runtime / "runtime.json").write_text(
        json.dumps(
            {
                "version": models.OMLX_VERSION,
                "revision": models.OMLX_REVISION,
                "archive_sha256": models.RUNTIME_ARCHIVE_SHA256,
            }
        )
    )
    monkeypatch.setenv("GEER_PI_BIN", "/synthetic/pi")
    monkeypatch.setattr(models, "compatible_pi", lambda *a, **k: "/synthetic/pi")
    monkeypatch.setattr(models, "find_t3", lambda: SimpleNamespace(found=True))
    after = models.model_plan(inventory.workspace, family="sage")
    assert after["installed_bytes"] == before["installed_bytes"] == 100
    assert after["runtime_installed_bytes"] == before["runtime_installed_bytes"]
    assert after["pi_installed_bytes"] == before["pi_installed_bytes"]
    assert after["t3_installed_bytes"] == before["t3_installed_bytes"]
    assert after["required_free_bytes"] == 17 + after["temporary_bytes"]
    assert before["required_free_bytes"] - after["required_free_bytes"] == sum(
        before[f"{name}_required_free_bytes"] for name in ("runtime", "pi", "t3")
    )
    assert all(after[f"{name}_download_bytes"] == 0 for name in ("runtime", "pi", "t3"))
    assert "estimate" in after["temporary_note"]


def test_retained_verified_archives_reduce_only_missing_download_bytes(inventory, monkeypatch):
    before = models.model_plan(inventory.workspace)
    monkeypatch.setattr(models, "_verified_archive", lambda *a: True)
    after = models.model_plan(inventory.workspace)
    assert before["required_free_bytes"] - after["required_free_bytes"] == (
        models.RUNTIME_ARCHIVE_BYTES + models.PI_ARCHIVE_BYTES
    )
    assert after["runtime_required_free_bytes"] == models.RUNTIME_INSTALLED_BYTES
    assert after["pi_required_free_bytes"] == models.PI_EXTRACTED_BYTES
    assert after["installed_bytes"] == before["installed_bytes"]


def test_wrong_runtime_revision_is_charged_as_missing(inventory):
    command = inventory.workspace.runtime / "omlx/bin/omlx"
    command.parent.mkdir(parents=True)
    command.write_text("synthetic executable")
    (inventory.workspace.runtime / "runtime.json").write_text(
        json.dumps(
            {
                "version": models.OMLX_VERSION,
                "revision": "wrong",
                "archive_sha256": models.RUNTIME_ARCHIVE_SHA256,
            }
        )
    )
    plan = models.model_plan(inventory.workspace)
    assert plan["runtime_reusable"] is False
    assert plan["runtime_required_free_bytes"] == (
        models.RUNTIME_ARCHIVE_BYTES + models.RUNTIME_INSTALLED_BYTES
    )


def test_archive_reuse_requires_full_digest_and_invalidates_on_same_size_corruption(tmp_path):
    import hashlib

    archive = tmp_path / "archive.tar.gz"
    archive.write_bytes(b"verified archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    models._VERIFIED.clear()
    assert models._verified_archive(archive, archive.stat().st_size, digest) is True
    assert models._verified_archive(archive, archive.stat().st_size, digest) is True
    stat = archive.stat()
    archive.write_bytes(b"modified archive")
    os.utime(archive, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert models._verified_archive(archive, archive.stat().st_size, digest) is False


def test_staged_pi_on_path_does_not_reduce_managed_bootstrap_space(inventory, monkeypatch):
    monkeypatch.setattr(models, "compatible_pi", lambda *a, **k: "/staged/pi")
    plan = models.model_plan(inventory.workspace)
    assert plan["pi_reusable"] is False
    assert plan["pi_required_free_bytes"] == models.PI_ARCHIVE_BYTES + models.PI_EXTRACTED_BYTES


def test_verified_managed_pi_reuses_full_check_until_files_change(inventory, monkeypatch):
    directory = inventory.workspace.runtime / "pi-runtime/pi"
    directory.mkdir(parents=True)
    executable = directory / "pi"
    executable.write_text("synthetic Pi")
    calls = []
    monkeypatch.setattr(models, "pi_installed", lambda path: calls.append(path) or True)
    first = models.model_plan(inventory.workspace)
    second = models.model_plan(inventory.workspace)
    assert first["pi_reusable"] is second["pi_reusable"] is True
    assert first["pi_required_free_bytes"] == 0
    assert calls == [directory]
    executable.write_text("changed Pi")
    models.model_plan(inventory.workspace)
    assert calls == [directory, directory]


def test_unverified_zero_byte_hub_result_is_an_estimate(inventory, monkeypatch):
    target = inventory.make("ornith")
    (target / "weights.safetensors").write_text("changed")
    monkeypatch.setattr(
        inventory.installer,
        "published_install_plan",
        lambda *a: {
            "download_bytes": 0,
            "download_estimated": False,
        },
    )
    plan = models.model_plan(inventory.workspace, family="ornith")
    assert plan["installed"] is False
    assert plan["download_bytes"] == plan["installed_bytes"] > 0
    assert plan["download_estimated"] is True


@pytest.mark.parametrize("bad", ([], None, {"files": []}))
def test_malformed_inactive_manifest_does_not_block_healthy_explicit_family(inventory, bad):
    inventory.make("sage")
    inactive = inventory.make("ornith")
    (inactive / inventory.installer.BUILD_MANIFEST).write_text(json.dumps(bad))
    catalog = models.model_catalog(inventory.workspace)
    assert row(catalog, "sage")["installed"] is True
    assert row(catalog, "ornith")["installed"] is False
    assert models.use_model(inventory.workspace, "sage")["model"] == "sage"


@pytest.fixture
def hub_cache(inventory, monkeypatch):
    target = inventory.make("ornith")
    source = {path.name: path.read_bytes() for path in target.iterdir()}
    requested = []
    normal_downloads = []

    def snapshot_download(**kwargs):
        assert kwargs["repo_id"] == "example/ornith-6bit"
        assert kwargs["revision"] == "b" * 40
        if kwargs.get("dry_run"):
            return [
                SimpleNamespace(file_size=len(content), will_download=not (target / name).is_file())
                for name, content in source.items()
            ]
        normal_downloads.append(kwargs)
        assert not kwargs.get("force_download")
        for name, content in source.items():
            if not (target / name).exists():
                (target / name).write_bytes(content)
        return str(target)

    def hf_hub_download(*, filename, repo_id, revision, cache_dir, force_download):
        assert repo_id == "example/ornith-6bit"
        assert revision == "b" * 40
        assert cache_dir == inventory.cache
        assert force_download is True
        requested.append(filename)
        (target / filename).write_bytes(source[filename])
        return str(target / filename)

    monkeypatch.setattr("huggingface_hub.snapshot_download", snapshot_download)
    monkeypatch.setattr("huggingface_hub.hf_hub_download", hf_hub_download)
    monkeypatch.setattr(inventory.installer, "published_install_plan", inventory.published_plan)
    return SimpleNamespace(
        target=target, source=source, requested=requested, normal_downloads=normal_downloads
    )


@pytest.mark.parametrize("corrupt", ("weights.safetensors", "geer-build-manifest.json"))
def test_rejected_exact_cache_repairs_only_bad_files_and_preserves_active_and_rollback(
    inventory,
    hub_cache,
    monkeypatch,
    corrupt,
):
    good = inventory.make("sage")
    inventory.activate(good)
    inventory.save("sage")
    previous = inventory.make("ornith", candidate=True)
    (inventory.workspace.models / "previous").symlink_to(previous)
    shard = hub_cache.target / corrupt
    shard.write_bytes(b"changed" if corrupt == "weights.safetensors" else b"[]")
    untouched = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in hub_cache.target.iterdir()
        if path.name not in {corrupt, inventory.installer.BUILD_MANIFEST}
    }
    plan = models.model_plan(inventory.workspace, family="ornith")
    assert plan["installed"] is False
    assert plan["download_bytes"] > 0
    assert plan["download_estimated"] is True
    result = models.install_model(inventory.workspace, family="ornith", activate=False)
    assert result["installed"]["verified"] is True
    assert result["activated"] is False
    assert hub_cache.requested == (
        [inventory.installer.BUILD_MANIFEST, "weights.safetensors"]
        if corrupt == "weights.safetensors"
        else [inventory.installer.BUILD_MANIFEST]
    )
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in untouched} == untouched
    assert (inventory.workspace.models / "active").resolve() == good
    assert (inventory.workspace.models / "previous").resolve() == previous
    assert models._saved_family(inventory.workspace) == "sage"
    assert inventory.stopped == []
    assert row(models.model_catalog(inventory.workspace), "ornith")["installed"] is True


@pytest.mark.parametrize("missing", ("weights.safetensors", "tokenizer.json"))
def test_partial_cache_uses_normal_resumable_download_without_force(inventory, hub_cache, missing):
    (hub_cache.target / missing).unlink()
    plan = models.model_plan(inventory.workspace, family="ornith")
    assert plan["download_bytes"] == len(hub_cache.source[missing])
    assert plan["download_estimated"] is False
    result = models.install_model(inventory.workspace, family="ornith", activate=False)
    assert result["installed"]["verified"] is True
    assert len(hub_cache.normal_downloads) == 1
    assert hub_cache.requested == []


def test_partial_cache_estimate_covers_missing_file_and_same_size_corrupt_shard(
    inventory, hub_cache
):
    (hub_cache.target / "tokenizer.json").unlink()
    shard = hub_cache.target / "weights.safetensors"
    original_size = shard.stat().st_size
    shard.write_text("changed")
    assert shard.stat().st_size == original_size
    plan = models.model_plan(inventory.workspace, family="ornith")
    assert plan["installed"] is False
    assert plan["download_estimated"] is True
    assert plan["download_bytes"] >= sum(
        len(hub_cache.source[name])
        for name in (
            "tokenizer.json",
            "weights.safetensors",
            inventory.installer.BUILD_MANIFEST,
        )
    )
    assert plan["required_free_bytes"] >= plan["download_bytes"] + plan["temporary_bytes"]
    result = models.install_model(inventory.workspace, family="ornith", activate=False)
    assert result["installed"]["verified"] is True
    assert hub_cache.requested == [inventory.installer.BUILD_MANIFEST, "weights.safetensors"]
    assert len(hub_cache.normal_downloads) == 1


def test_repair_rejects_unsafe_remote_manifest_before_downloading_file_paths(inventory, hub_cache):
    (hub_cache.target / "weights.safetensors").write_text("changed")
    remote = json.loads(hub_cache.source[inventory.installer.BUILD_MANIFEST])
    remote["files"][0]["path"] = "../escape"
    hub_cache.source[inventory.installer.BUILD_MANIFEST] = json.dumps(remote).encode()
    with pytest.raises(inventory.installer.BuildError, match="unsafe"):
        models.install_model(inventory.workspace, family="ornith", activate=False)
    assert hub_cache.requested == [inventory.installer.BUILD_MANIFEST]
    assert not (hub_cache.target.parent / "escape").exists()
    assert (hub_cache.target / "weights.safetensors").read_text() == "changed"


def test_removal_of_verified_legacy_active_unlinks_managed_state_and_retains_files(
    inventory,
    monkeypatch,
):
    candidate = inventory.make("ornith", candidate=True)
    inventory.activate(candidate)
    models.initialize_assets(inventory.workspace, candidate)
    inventory.workspace.setup_state.write_text("setup")
    rollback = inventory.make("sage")
    previous = inventory.workspace.models / "previous"
    previous.symlink_to(rollback)
    before = {path: path.read_bytes() for path in candidate.iterdir()}
    monkeypatch.setattr(
        "huggingface_hub.scan_cache_dir", lambda *a: pytest.fail("legacy cache deletion")
    )

    def stop(_):
        assert (inventory.workspace.models / "active").resolve() == candidate
        inventory.stopped.append(True)

    monkeypatch.setattr(models, "stop_server", stop)
    result = models.remove_model(inventory.workspace, "ornith")
    assert result["removed"] is result["active_removed"] is True
    assert result["cache_reclaimed"] is False
    assert inventory.stopped == [True]
    assert not (inventory.workspace.models / "active").is_symlink()
    assert not inventory.workspace.manifest.exists()
    assert not inventory.workspace.setup_state.exists()
    assert not (inventory.workspace.models / "geer-local").exists()
    assert {path: path.read_bytes() for path in candidate.iterdir()} == before
    assert previous.resolve() == rollback
    assert rollback.is_dir()


def test_legacy_removal_requires_t3_quit_before_any_mutation(inventory, monkeypatch):
    candidate = inventory.make("ornith", candidate=True)
    inventory.activate(candidate)
    models.initialize_assets(inventory.workspace, candidate)
    before = inventory.workspace.manifest.read_bytes()
    monkeypatch.setattr(models, "running_t3_applications", lambda: [Path("T3.app")])
    with pytest.raises(AssetError, match="quit T3 Code"):
        models.remove_model(inventory.workspace, "ornith")
    assert inventory.stopped == []
    assert (inventory.workspace.models / "active").resolve() == candidate
    assert inventory.workspace.manifest.read_bytes() == before


def test_legacy_removal_preserves_unrelated_asset_family(inventory):
    candidate = inventory.make("ornith", candidate=True)
    inventory.activate(candidate)
    unrelated = inventory.make("sage")
    models.initialize_assets(inventory.workspace, unrelated)
    before = inventory.workspace.manifest.read_bytes()
    result = models.remove_model(inventory.workspace, "ornith")
    assert result["active_removed"] is True
    assert inventory.workspace.manifest.read_bytes() == before
    assert verify_assets(inventory.workspace)["source"] == str(unrelated)


def test_absent_family_removal_does_not_claim_a_removal(inventory):
    result = models.remove_model(inventory.workspace, "ornith")
    assert result["removed"] is False
    assert result["active_removed"] is False
    assert inventory.stopped == []


@pytest.mark.parametrize(
    "malformation",
    ("files_object", "record_array", "bad_size", "bad_hash", "duplicate", "bad_total"),
)
def test_build_manifest_structure_is_rejected_with_build_error(inventory, malformation):
    target = inventory.make("ornith")
    manifest_path = target / inventory.installer.BUILD_MANIFEST
    manifest = json.loads(manifest_path.read_text())
    if malformation == "files_object":
        manifest["files"] = {}
    elif malformation == "record_array":
        manifest["files"] = [[]]
    elif malformation == "bad_size":
        manifest["files"][0]["bytes"] = "123"
    elif malformation == "bad_hash":
        manifest["files"][0]["sha256"] = []
    elif malformation == "duplicate":
        manifest["files"].append(manifest["files"][0])
    else:
        manifest["bytes"] += 1
    manifest_path.write_text(json.dumps(manifest))
    recipe = inventory.installer.load_recipe(
        inventory.workspace.root / "model-recipes/ornith-1.5-35b-6bit.toml"
    )
    with pytest.raises(inventory.installer.BuildError, match="build manifest"):
        inventory.installer.verify_output(recipe, target)


@pytest.mark.parametrize("filename", ("config.json", "model.safetensors.index.json"))
def test_nonobject_config_and_index_raise_build_error(inventory, filename):
    target = inventory.make("ornith")
    (target / filename).write_text("[]")
    recipe = inventory.installer.load_recipe(
        inventory.workspace.root / "model-recipes/ornith-1.5-35b-6bit.toml"
    )
    with pytest.raises(inventory.installer.BuildError, match="must be a JSON object"):
        inventory.installer.verify_output(recipe, target)


def test_scoped_repair_preserves_unexpected_files_for_review(inventory, hub_cache):
    extra = hub_cache.target / "personal-notes.txt"
    extra.write_text("retain these notes")
    with pytest.raises(inventory.installer.BuildError, match="unexpected files; preserve"):
        models.install_model(inventory.workspace, family="ornith", activate=False)
    assert extra.read_text() == "retain these notes"
    assert hub_cache.requested == [inventory.installer.BUILD_MANIFEST]
