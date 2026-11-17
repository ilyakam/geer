from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from geer.assets import Workspace
from geer.retrieval import (
    SEMBLE_VERSION,
    _wait_for_incremental_search,
    retrieval_canary,
    retrieval_mcp_config,
    semble_command,
)


def test_retrieval_mcp_config_is_private_and_pinned(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    server = retrieval_mcp_config(workspace)["mcpServers"]["semble"]

    assert Path(server["command"]).name == "semble"
    assert server["args"] == ["--content", "all"]
    assert server["exposure"] == "direct"
    assert server["timeout"] == 600
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


def _search_response(*paths: str, error: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        isError=error,
        content=[SimpleNamespace(text=json.dumps({"results": [{"file_path": p} for p in paths]}))],
    )


@pytest.fixture
def incremental_clock(monkeypatch):
    clock = [0.0]

    async def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr("geer.retrieval.time.perf_counter", lambda: clock[0])
    monkeypatch.setattr("geer.retrieval.asyncio.sleep", sleep)
    return clock


def test_incremental_search_waits_for_refresh_and_rank_one(incremental_clock) -> None:
    responses = iter(
        [
            _search_response("src/router.py"),
            _search_response("src/router.py", "src/fresh.py"),
            _search_response("src/fresh.py"),
        ]
    )

    async def call_tool(name, arguments):
        assert name == "search"
        assert arguments["repo"] == "/fixture"
        return next(responses)

    result = asyncio.run(
        _wait_for_incremental_search(
            SimpleNamespace(call_tool=call_tool), Path("/fixture"), timeout=1.0
        )
    )

    assert result["passed"]
    assert result["rank"] == 1
    assert result["attempts"] == 3
    assert result["latency_ms"] == pytest.approx(200.0)


def test_incremental_search_fails_if_rank_one_never_appears(incremental_clock) -> None:
    async def call_tool(name, arguments):
        return _search_response("src/router.py", "src/fresh.py")

    result = asyncio.run(
        _wait_for_incremental_search(
            SimpleNamespace(call_tool=call_tool), Path("/fixture"), timeout=0.25
        )
    )

    assert not result["passed"]
    assert result["rank"] == 2
    assert result["attempts"] == 3
    assert result["latency_ms"] == pytest.approx(250.0)


def test_incremental_search_fails_immediately_on_tool_error() -> None:
    async def call_tool(name, arguments):
        return _search_response(error=True)

    result = asyncio.run(
        _wait_for_incremental_search(
            SimpleNamespace(call_tool=call_tool), Path("/fixture"), timeout=1.0
        )
    )

    assert not result["passed"]
    assert result["tool_error"]
    assert result["attempts"] == 1


def test_incremental_search_cancels_a_stalled_request() -> None:
    cancelled = []

    async def call_tool(name, arguments):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    result = asyncio.run(
        _wait_for_incremental_search(
            SimpleNamespace(call_tool=call_tool), Path("/fixture"), timeout=0.01
        )
    )

    assert not result["passed"]
    assert result["rank"] is None
    assert result["attempts"] == 1
    assert cancelled == [True]
