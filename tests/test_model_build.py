from __future__ import annotations

import hashlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest


def load_model_build():
    path = Path(__file__).parents[1] / "tools" / "model_build.py"
    spec = importlib.util.spec_from_file_location("geer_model_build", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeMX:
    @staticmethod
    def stack(values, *, axis):
        return ("stack", tuple(values), axis)

    @staticmethod
    def concatenate(values, *, axis):
        return ("concatenate", tuple(values), axis)


def unfused_weights():
    weights = {}
    for expert in range(2):
        prefix = f"model.language_model.layers.0.mlp.experts.{expert}"
        weights[f"{prefix}.gate_proj.weight"] = f"gate-{expert}"
        weights[f"{prefix}.up_proj.weight"] = f"up-{expert}"
        weights[f"{prefix}.down_proj.weight"] = f"down-{expert}"
    weights["unrelated"] = "preserved"
    return weights


def test_fuse_ornith_experts_batches_gate_up_and_down():
    model_build = load_model_build()
    weights = model_build.fuse_ornith_experts(
        unfused_weights(),
        num_layers=1,
        num_experts=2,
        mx_module=FakeMX,
    )

    prefix = "model.language_model.layers.0.mlp.experts"
    assert weights[f"{prefix}.gate_up_proj"] == (
        "concatenate",
        (
            ("stack", ("gate-0", "gate-1"), 0),
            ("stack", ("up-0", "up-1"), 0),
        ),
        -2,
    )
    assert weights[f"{prefix}.down_proj"] == (
        "stack",
        ("down-0", "down-1"),
        0,
    )
    assert weights["unrelated"] == "preserved"
    assert not any(".experts.0." in key for key in weights)


def test_fuse_ornith_experts_accepts_already_fused_weights():
    model_build = load_model_build()
    prefix = "model.language_model.layers.0.mlp.experts"
    weights = {
        f"{prefix}.gate_up_proj": "gate-up",
        f"{prefix}.down_proj": "down",
    }

    result = model_build.fuse_ornith_experts(
        weights,
        num_layers=1,
        num_experts=2,
        mx_module=FakeMX,
    )

    assert result == weights


def test_fuse_ornith_experts_rejects_incomplete_expert():
    model_build = load_model_build()
    weights = unfused_weights()
    del weights["model.language_model.layers.0.mlp.experts.1.down_proj.weight"]

    with pytest.raises(model_build.BuildError, match="layer 0, expert 1"):
        model_build.fuse_ornith_experts(
            weights,
            num_layers=1,
            num_experts=2,
            mx_module=FakeMX,
        )


def test_recipe_summary_keeps_activation_manual():
    model_build = load_model_build()
    recipe = model_build.load_recipe(
        Path(__file__).parents[1] / "model-recipes" / "ornith-1.0-35b-6bit.toml"
    )

    summary = model_build.recipe_summary(
        recipe,
        Path("/tmp/cache"),
        Path("/tmp/output"),
    )

    assert summary["source_revision"] == ("5df2ed3f675c7beaa490328cc70bb573b65fb660")
    assert summary["quantization"]["bits"] == 6
    assert summary["activation"] == "manual"


def test_routed_expert_mixed_quantization_policy():
    model_build = load_model_build()
    recipe = model_build.Recipe(
        path=Path("/tmp/recipe.toml"),
        data={
            "quantization": {
                "enabled": True,
                "strategy": "routed_experts_4bit_protected_text_8bit",
                "bits": 4,
                "high_bits": 8,
                "group_size": 64,
                "mode": "affine",
            }
        },
    )

    predicate = model_build.quantization_predicate(recipe)
    assert predicate is not None
    assert predicate("vision_tower.blocks.0.attn.qkv", object()) is False
    assert (
        predicate("language_model.model.layers.0.mlp.switch_mlp.gate_proj", object())["bits"] == 4
    )
    for path in (
        "language_model.model.embed_tokens",
        "language_model.model.layers.0.linear_attn.in_proj_qkv",
        "language_model.model.layers.3.self_attn.q_proj",
        "language_model.model.layers.0.mlp.gate",
        "language_model.model.layers.0.mlp.shared_expert.gate_proj",
        "language_model.lm_head",
    ):
        assert predicate(path, object()) == {
            "group_size": 64,
            "bits": 8,
            "mode": "affine",
        }


def test_verify_routed_expert_mixed_quantization_layout():
    model_build = load_model_build()
    recipe = model_build.Recipe(
        path=Path("/tmp/recipe.toml"),
        data={
            "quantization": {
                "enabled": True,
                "strategy": "routed_experts_4bit_protected_text_8bit",
                "bits": 4,
                "high_bits": 8,
                "group_size": 64,
                "mode": "affine",
            }
        },
    )
    config = {
        "quantization": {
            "group_size": 64,
            "bits": 4,
            "mode": "affine",
            "language_model.model.layers.0.mlp.switch_mlp.gate_proj": {
                "group_size": 64,
                "bits": 4,
                "mode": "affine",
            },
            "language_model.model.layers.0.self_attn.q_proj": {
                "group_size": 64,
                "bits": 8,
                "mode": "affine",
            },
        }
    }

    model_build.verify_quantization_layout(recipe, config)

    config["quantization"]["language_model.model.layers.0.self_attn.q_proj"]["bits"] = 4
    with pytest.raises(model_build.BuildError, match="bits do not match"):
        model_build.verify_quantization_layout(recipe, config)


def test_verify_source_rehashes_the_pinned_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    model_build = load_model_build()
    revision = "a" * 40
    recipe_path = tmp_path / "recipe.toml"
    recipe_path.write_text("")
    recipe = model_build.Recipe(
        path=recipe_path,
        data={
            "id": "test-model",
            "source": {
                "repo_id": "example/model",
                "revision": revision,
                "expected_bytes": 7,
                "license": "MIT",
                "license_evidence_url": "https://example.com/license",
            },
            "conversion": {},
            "quantization": {},
            "output": {},
        },
    )
    monkeypatch.setenv("GEER_HOME", str(tmp_path / "geer-home"))
    snapshot = tmp_path / revision
    snapshot.mkdir()
    (snapshot / "weights.bin").write_bytes(b"weights")
    manifest = {
        "schema_version": 1,
        "recipe": "test-model",
        "repo_id": "example/model",
        "revision": revision,
        "license": "MIT",
        "license_evidence_url": "https://example.com/license",
        "bytes": 7,
        "files": model_build.hash_files(snapshot),
    }
    manifest_path = model_build.source_manifest_path(recipe)
    model_build.atomic_json(manifest_path, manifest)

    result = model_build.verify_source(recipe, snapshot)

    assert result["manifest"] == manifest
    assert result["sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    (snapshot / "weights.bin").write_bytes(b"changed")
    with pytest.raises(model_build.BuildError, match="source files"):
        model_build.verify_source(recipe, snapshot)


def test_prepare_publication_copies_metadata_and_refreshes_hashes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    model_build = load_model_build()
    geer_home = tmp_path / "home"
    monkeypatch.setenv("GEER_HOME", str(geer_home))
    recipe_path = tmp_path / "recipe.toml"
    recipe_path.write_text("recipe")
    templates = tmp_path / "card"
    templates.mkdir()
    for name in ("README.md", "LICENSE", "LICENSE-QWEN", "NOTICE"):
        (templates / name).write_text(f"{name}\n")
    recipe = model_build.Recipe(
        path=recipe_path,
        data={
            "id": "test-model",
            "display_name": "Test Model",
            "source": {
                "repo_id": "example/source",
                "revision": "a" * 40,
                "expected_bytes": 1,
                "license": "MIT",
                "license_evidence_url": "https://example.com/license",
            },
            "conversion": {},
            "quantization": {
                "enabled": True,
                "bits": 6,
                "group_size": 64,
                "mode": "affine",
            },
            "output": {"expected_architecture": "test"},
            "distribution": {
                "repo_id": "example/model",
                "tag": "v1",
                "model_card_dir": str(templates),
            },
        },
    )
    source_manifest = model_build.source_manifest_path(recipe)
    source_manifest.parent.mkdir(parents=True)
    source_manifest.write_text("{}\n")

    output = tmp_path / "output"
    output.mkdir()
    (output / "README.md").write_text("placeholder\n")
    (output / "config.json").write_text(
        '{"model_type": "test", "quantization": {"bits": 6, "group_size": 64, "mode": "affine"}}\n'
    )
    (output / "model.safetensors.index.json").write_text(
        '{"weight_map": {"tensor": "weights.safetensors"}}\n'
    )
    (output / "weights.safetensors").write_bytes(b"weights")
    files = model_build.hash_files(output)
    model_build.atomic_json(
        output / model_build.BUILD_MANIFEST,
        {
            "schema_version": 1,
            "recipe": "test-model",
            "display_name": "Test Model",
            "source": {
                "repo_id": "example/source",
                "revision": "a" * 40,
                "license": "MIT",
                "license_evidence_url": "https://example.com/license",
            },
            "quantization": {
                "enabled": True,
                "bits": 6,
                "group_size": 64,
                "mode": "affine",
            },
            "files": files,
            "bytes": sum(record["bytes"] for record in files),
        },
    )

    result = model_build.prepare_publication(recipe, output)

    assert result["verified"] is True
    assert (output / "README.md").read_text() == "README.md\n"
    assert (output / "geer-recipe.toml").read_text() == "recipe"
    assert (output / "geer-source-manifest.json").read_text() == "{}\n"
    manifest = model_build.json.loads((output / model_build.BUILD_MANIFEST).read_text())
    assert {record["path"] for record in manifest["files"]} == {
        "LICENSE",
        "LICENSE-QWEN",
        "NOTICE",
        "README.md",
        "config.json",
        "geer-recipe.toml",
        "geer-source-manifest.json",
        "model.safetensors.index.json",
        "weights.safetensors",
    }
    (output / ".gitattributes").write_text("*.safetensors filter=lfs\n")
    assert model_build.verify_output(recipe, output)["verified"] is True


def test_activate_snapshot_keeps_previous_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    model_build = load_model_build()
    monkeypatch.setenv("GEER_HOME", str(tmp_path / "home"))
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    models = tmp_path / "home" / "models"
    models.mkdir(parents=True)
    (models / "active").symlink_to(old)

    result = model_build.activate_snapshot(new)

    assert (models / "active").resolve() == new
    assert (models / "previous").resolve() == old
    assert result["previous"] == str(old)


def test_activate_snapshot_refuses_unmanaged_rollback_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    model_build = load_model_build()
    monkeypatch.setenv("GEER_HOME", str(tmp_path / "home"))
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    models = tmp_path / "home" / "models"
    models.mkdir(parents=True)
    (models / "active").symlink_to(old)
    (models / "previous").write_text("unmanaged")

    with pytest.raises(model_build.BuildError, match="rollback path"):
        model_build.activate_snapshot(new)

    assert (models / "active").resolve() == old
    assert (models / "previous").read_text() == "unmanaged"


def test_publication_requires_exact_repo_confirmation(tmp_path: Path):
    model_build = load_model_build()
    recipe = model_build.Recipe(
        path=tmp_path / "recipe.toml",
        data={"distribution": {"repo_id": "example/model"}},
    )

    with pytest.raises(model_build.BuildError, match="confirm-repo-id"):
        model_build.require_publish_confirmation(recipe, "example/other")

    assert model_build.require_publish_confirmation(recipe, "example/model") == "example/model"


def test_published_install_plan_reports_uncached_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    model_build = load_model_build()
    recipe = model_build.Recipe(
        path=tmp_path / "recipe.toml",
        data={
            "distribution": {
                "repo_id": "example/model",
                "revision": "b" * 40,
            }
        },
    )
    files = [
        types.SimpleNamespace(file_size=10, will_download=True),
        types.SimpleNamespace(file_size=5, will_download=False),
    ]
    fake_hub = types.SimpleNamespace(snapshot_download=lambda **kwargs: files)
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)
    monkeypatch.setenv("GEER_HOME", str(tmp_path / "home"))

    result = model_build.published_install_plan(recipe, tmp_path / "cache")

    assert result["repo_id"] == "example/model"
    assert result["revision"] == "b" * 40
    assert result["download_bytes"] == 10
    assert result["installed_bytes"] == 15
    assert result["required_free_bytes"] == 10 + model_build.SAFETY_MARGIN_BYTES


def test_install_published_reuses_verified_active_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    model_build = load_model_build()
    model = tmp_path / "candidate"
    model.mkdir()
    active = tmp_path / "models" / "active"
    active.parent.mkdir()
    active.symlink_to(model)
    monkeypatch.setenv("GEER_HOME", str(tmp_path))
    monkeypatch.setattr(
        model_build,
        "verify_output",
        lambda recipe, output: {
            "verified": True,
            "output_dir": str(output),
        },
    )

    result = model_build.install_published(
        model_build.Recipe(
            path=tmp_path / "recipe.toml",
            data={
                "distribution": {
                    "repo_id": "example/model",
                    "revision": "b" * 40,
                }
            },
        ),
        tmp_path / "cache",
    )

    assert result["verified"] is True
    assert result["activation"]["reused"] is True
    assert result["activation"]["target"] == str(model)


def test_install_published_keeps_a_different_active_model_for_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    model_build = load_model_build()
    old = tmp_path / "old-model"
    new = tmp_path / "new-model"
    old.mkdir()
    new.mkdir()
    active = tmp_path / "home/models/active"
    active.parent.mkdir(parents=True)
    active.symlink_to(old)
    monkeypatch.setenv("GEER_HOME", str(tmp_path / "home"))

    def verify(recipe, output):
        if output.resolve() == old.resolve():
            raise model_build.BuildError("different recipe")
        return {"verified": True, "output_dir": str(output)}

    monkeypatch.setattr(model_build, "verify_output", verify)
    monkeypatch.setattr(
        model_build,
        "published_install_plan",
        lambda recipe, cache: {
            "repo_id": "example/new-model",
            "revision": "c" * 40,
            "required_free_bytes": 1,
        },
    )
    monkeypatch.setattr(model_build, "require_disk_space", lambda *args: None)
    fake_hub = types.SimpleNamespace(snapshot_download=lambda **kwargs: str(new))
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)

    result = model_build.install_published(
        model_build.Recipe(
            path=tmp_path / "recipe.toml",
            data={
                "distribution": {
                    "repo_id": "example/new-model",
                    "revision": "c" * 40,
                }
            },
        ),
        tmp_path / "cache",
    )

    assert result["activation"]["target"] == str(new.resolve())
    assert active.resolve() == new.resolve()
    assert (active.parent / "previous").resolve() == old.resolve()
