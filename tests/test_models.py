from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from geer.assets import Workspace
from geer.hardware import GIB, select_hardware_profile
from geer.models import install_model, model_plan, recipe_and_installer


def fake_model_installer(tmp_path: Path) -> SimpleNamespace:
    def load_recipe(path: Path) -> SimpleNamespace:
        mixed = "4-8bit" in path.name
        return SimpleNamespace(
            identifier=path.stem,
            data={"display_name": ("Geer Test 4/8-bit" if mixed else "Geer Test 6-bit")},
            distribution={
                "repo_id": "example/4-8bit" if mixed else "example/6bit",
                "revision": ("4" if mixed else "6") * 40,
                "expected_bytes": 20 if mixed else 27,
            },
        )

    return SimpleNamespace(
        BUILD_MANIFEST="geer-build-manifest.json",
        default_cache_dir=lambda: tmp_path / "cache",
        geer_home=lambda: tmp_path / "home",
        human_bytes=lambda value: f"{value} bytes",
        load_recipe=load_recipe,
    )


@pytest.mark.parametrize(
    ("memory_gib", "recipe_name"),
    (
        (32, "ornith-1.0-35b-4-8bit.toml"),
        (48, "ornith-1.0-35b-4-8bit.toml"),
        (64, "ornith-1.0-35b-6bit.toml"),
        (96, "ornith-1.0-35b-6bit.toml"),
        (128, "ornith-1.0-35b-6bit.toml"),
    ),
)
def test_recipe_selection_follows_the_hardware_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_gib: int,
    recipe_name: str,
) -> None:
    selected: list[Path] = []
    installer = SimpleNamespace(
        load_recipe=lambda path: selected.append(path) or object(),
    )
    monkeypatch.setattr("geer.models.load_model_build", lambda workspace: installer)

    recipe_and_installer(
        Workspace(tmp_path),
        select_hardware_profile(memory_gib * GIB),
    )

    assert selected[0].name == recipe_name


@pytest.mark.parametrize(
    ("memory_gib", "repo_id", "context_window"),
    (
        (32, "example/4-8bit", 65_536),
        (48, "example/4-8bit", 131_072),
        (64, "example/6bit", 262_144),
        (96, "example/6bit", 262_144),
        (128, "example/6bit", 262_144),
    ),
)
def test_model_plan_selects_the_matching_distribution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_gib: int,
    repo_id: str,
    context_window: int,
) -> None:
    installer = fake_model_installer(tmp_path)
    monkeypatch.setattr("geer.models.load_model_build", lambda workspace: installer)

    plan = model_plan(Workspace(tmp_path), total_memory_bytes=memory_gib * GIB)

    assert plan["repo_id"] == repo_id
    assert plan["context_window"] == context_window
    assert plan["kv_cache"] == "BF16"


def test_model_plan_reuses_only_the_selected_recipe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = fake_model_installer(tmp_path)
    active_model = tmp_path / "active-model"
    active_model.mkdir()
    (active_model / "geer-build-manifest.json").write_text(
        json.dumps({"recipe": "ornith-1.0-35b-6bit"})
    )
    active = tmp_path / "home/models/active"
    active.parent.mkdir(parents=True)
    active.symlink_to(active_model)
    monkeypatch.setattr("geer.models.load_model_build", lambda workspace: installer)

    assert (
        model_plan(Workspace(tmp_path), total_memory_bytes=128 * GIB)["reusable_active_model"]
        is True
    )
    assert (
        model_plan(Workspace(tmp_path), total_memory_bytes=48 * GIB)["reusable_active_model"]
        is False
    )


def test_install_model_applies_detected_memory_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    installer = SimpleNamespace(
        default_cache_dir=lambda: tmp_path / "cache",
        geer_home=lambda: tmp_path / ".geer",
        install_published=lambda recipe, cache: {"activation": {"reused": False}},
    )
    monkeypatch.setattr(
        "geer.models.recipe_and_installer",
        lambda value, profile: (object(), installer),
    )
    monkeypatch.setattr(
        "geer.models.initialize_assets",
        lambda *args, **kwargs: {"model_id": "geer-local"},
    )
    applied: dict[str, int | None] = {}

    def apply_settings(
        value: Workspace,
        *,
        total_memory_bytes: int | None = None,
    ) -> dict[str, object]:
        applied["total_memory_bytes"] = total_memory_bytes
        return {"models": {}}

    monkeypatch.setattr("geer.models.ensure_model_settings", apply_settings)

    install_model(workspace, total_memory_bytes=64 * GIB)

    assert applied["total_memory_bytes"] == 64 * GIB
