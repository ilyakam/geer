from __future__ import annotations

import json
from pathlib import Path

from geer.assets import Workspace
from geer.retrieval import (
    SEMBLE_VERSION,
    retrieval_canary,
    retrieval_mcp_config,
    semble_command,
)


def test_retrieval_mcp_config_is_private_and_pinned(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    server = retrieval_mcp_config(workspace)["mcpServers"]["semble"]

    assert Path(server["command"]).name == "semble"
    assert server["args"] == ["--content", "all"]
    assert server["env"]["SEMBLE_CACHE_LOCATION"] == str(workspace.retrieval_cache)
    assert workspace.retrieval_cache.stat().st_mode & 0o777 == 0o700
    assert SEMBLE_VERSION == "0.5.2"


def test_semble_command_preserves_packaged_dispatch_symlink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executable = tmp_path / "geer"
    executable.touch(mode=0o755)
    command = tmp_path / "semble"
    command.symlink_to("geer")
    monkeypatch.setattr("geer.retrieval.sys.executable", str(executable))

    result = semble_command()

    assert result == command
    assert result.is_symlink()
    assert result.name == "semble"


def test_retrieval_canary_finds_every_expected_path(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    fixture = Path(__file__).parent / "fixtures" / "retrieval-canary"

    result = retrieval_canary(workspace, fixture)

    assert result["status"] == "ok"
    assert all(query["passed"] for query in result["queries"])
    saved = json.loads(workspace.retrieval_metrics.read_text())
    assert saved["provider"] == "semble"
    assert workspace.retrieval_metrics.stat().st_mode & 0o777 == 0o600
