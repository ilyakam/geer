from __future__ import annotations

from pathlib import Path

import pytest

from geer.assets import AssetError, Workspace
from geer.skills import ensure_user_skills_link


def test_missing_user_skills_do_not_create_link(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    assert ensure_user_skills_link(workspace, tmp_path / "missing") is None
    assert not workspace.claude_config.exists()


def test_user_skills_link_exposes_existing_skills_in_isolated_config(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    shared = tmp_path / "shared-skills"
    shared.mkdir()
    provider_skills = tmp_path / "claude-skills"
    provider_skills.symlink_to(shared, target_is_directory=True)

    skills_link = ensure_user_skills_link(workspace, provider_skills)

    assert skills_link == workspace.claude_config / "skills"
    assert skills_link.is_symlink()
    assert skills_link.resolve() == shared
    assert workspace.claude_config.stat().st_mode & 0o777 == 0o700
    assert ensure_user_skills_link(workspace, provider_skills) == skills_link


def test_user_skills_link_updates_a_stale_link(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    skills_link = ensure_user_skills_link(workspace, first)
    assert skills_link is not None

    ensure_user_skills_link(workspace, second)

    assert skills_link.resolve() == second


def test_user_skills_link_preserves_unexpected_content(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    skills = tmp_path / "skills"
    skills.mkdir()
    unexpected = workspace.claude_config / "skills"
    unexpected.mkdir(parents=True)

    with pytest.raises(AssetError, match="unexpected Claude skills path"):
        ensure_user_skills_link(workspace, skills)
