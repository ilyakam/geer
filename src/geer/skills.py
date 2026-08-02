from __future__ import annotations

from pathlib import Path

from .assets import AssetError, Workspace

DEFAULT_CLAUDE_SKILLS = Path("~/.claude/skills").expanduser()
def ensure_user_skills_link(
    workspace: Workspace,
    skills_directory: Path = DEFAULT_CLAUDE_SKILLS,
) -> Path | None:
    try:
        skills_target = skills_directory.expanduser().resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None
    if not skills_target.is_dir():
        return None

    config = workspace.claude_config
    if config.is_symlink() or (config.exists() and not config.is_dir()):
        raise AssetError(f"refusing to replace unexpected Claude config path: {config}")
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.chmod(0o700)

    skills_link = config / "skills"
    if skills_link.is_symlink():
        try:
            current_target = skills_link.resolve(strict=True)
        except (FileNotFoundError, OSError):
            current_target = None
        if current_target != skills_target:
            skills_link.unlink()
    elif skills_link.exists():
        raise AssetError(
            f"refusing to replace unexpected Claude skills path: {skills_link}"
        )
    if not skills_link.is_symlink():
        skills_link.symlink_to(skills_target, target_is_directory=True)

    return skills_link
