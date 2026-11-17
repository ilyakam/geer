from __future__ import annotations

import asyncio
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .assets import AssetError, Workspace

SEMBLE_VERSION = "0.5.2"
SEMBLE_MODEL = "minishlab/potion-code-16M-v2"
RETRIEVAL_SYSTEM_PROMPT = (
    "Use mcp__semble__search and mcp__semble__find_related first when discovering "
    "unfamiliar repository code, "
    "behavior, symbols, or architecture. Navigate directly to returned file and line "
    "locations. Use grep only for exhaustive literal confirmation, not to repeat the "
    "same discovery search."
)


def semble_command() -> Path:
    command = Path(sys.executable).with_name("semble")
    if not command.is_file():
        raise AssetError("Semble is missing; run `uv sync`")
    # The release bundles Semble as a symlink to Geer's executable and dispatches
    # from argv[0]. Keep the symlink name intact so the child starts in Semble mode.
    return command


def retrieval_mcp_config(workspace: Workspace) -> dict[str, Any]:
    workspace.retrieval_cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace.retrieval_cache.chmod(0o700)
    return {
        "mcpServers": {
            "semble": {
                "command": str(semble_command()),
                "args": ["--content", "all"],
                "exposure": "direct",
                "timeout": 600,
                "env": {
                    "SEMBLE_CACHE_LOCATION": str(workspace.retrieval_cache),
                    "PYINSTALLER_RESET_ENVIRONMENT": "1",
                },
            }
        }
    }


def retrieval_status(workspace: Workspace) -> dict[str, Any]:
    try:
        from semble import __version__
    except ImportError as error:
        raise AssetError("Semble is missing; run `uv sync`") from error
    if __version__ != SEMBLE_VERSION:
        raise AssetError(f"expected Semble {SEMBLE_VERSION}, found {__version__}")
    config = retrieval_mcp_config(workspace)["mcpServers"]["semble"]
    return {
        "provider": "semble",
        "version": __version__,
        "model": SEMBLE_MODEL,
        "executable": config["command"],
        "cache_directory": config["env"]["SEMBLE_CACHE_LOCATION"],
        "content": "all",
        "mcp_tools": ["search", "find_related"],
    }


def retrieval_canary(workspace: Workspace, fixture: Path | None = None) -> dict[str, Any]:
    try:
        from semble import ContentType, SembleIndex, __version__
    except ImportError as error:
        raise AssetError("Semble is missing; run `uv sync`") from error
    if __version__ != SEMBLE_VERSION:
        raise AssetError(f"expected Semble {SEMBLE_VERSION}, found {__version__}")

    fixture = fixture or workspace.root / "tests" / "fixtures" / "retrieval-canary"
    if not fixture.is_dir():
        raise AssetError(f"retrieval canary fixture is missing: {fixture}")

    workspace.retrieval_cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace.retrieval_cache.chmod(0o700)
    with _environment("SEMBLE_CACHE_LOCATION", str(workspace.retrieval_cache)):
        started = time.perf_counter()
        index = SembleIndex.from_path(
            fixture,
            content=(ContentType.CODE, ContentType.DOCS, ContentType.CONFIG),
        )
        index_ms = (time.perf_counter() - started) * 1000

        cases = [
            ("rare_literal", "callback-v7", "src/router.py", 1),
            (
                "semantic_flow",
                "validate an incoming event before persisting its receipt",
                "src/service.py",
                3,
            ),
            ("symbol", "ReceiptLedger", "src/ledger.py", 1),
            (
                "dependency",
                "where the validated payload is passed into ledger storage",
                "src/service.py",
                3,
            ),
            (
                "documentation",
                "how duplicate delivery attempts are handled",
                "docs/retrieval.md",
                3,
            ),
        ]
        outcomes: list[dict[str, Any]] = []
        query_ms: list[float] = []
        for name, query, expected, max_rank in cases:
            query_started = time.perf_counter()
            results = index.search(query, top_k=5, max_snippet_lines=10)
            query_ms.append((time.perf_counter() - query_started) * 1000)
            paths = [result.chunk.file_path for result in results]
            rank = paths.index(expected) + 1 if expected in paths else None
            outcomes.append(
                {
                    "name": name,
                    "expected": expected,
                    "rank": rank,
                    "max_rank": max_rank,
                    "passed": rank is not None and rank <= max_rank,
                }
            )

        passed = all(outcome["passed"] for outcome in outcomes)
        record = {
            "captured_at": datetime.now(UTC).isoformat(),
            "provider": "semble",
            "version": __version__,
            "model": SEMBLE_MODEL,
            "status": "ok" if passed else "failed",
            "fixture": "retrieval-canary",
            "indexed_files": index.stats.indexed_files,
            "chunks": index.stats.total_chunks,
            "loaded_from_disk": index.loaded_from_disk,
            "index_ms": round(index_ms, 3),
            "query_p50_ms": round(statistics.median(query_ms), 3),
            "queries": outcomes,
        }
    incremental = asyncio.run(_incremental_mcp_canary(workspace, fixture))
    record["incremental_update"] = incremental
    passed = passed and incremental["passed"]
    record["status"] = "ok" if passed else "failed"
    _append_json_line(workspace.retrieval_metrics, record)
    if not passed:
        failures = [item["name"] for item in outcomes if not item["passed"]]
        if not incremental["passed"]:
            failures.append(
                "incremental_update "
                f"(rank={incremental['rank']}, attempts={incremental['attempts']}, "
                f"tool_error={incremental['tool_error']})"
            )
        failed = ", ".join(failures)
        raise AssetError(f"Semble retrieval canary failed: {failed}")
    return record


async def _incremental_mcp_canary(
    workspace: Workspace,
    fixture: Path,
) -> dict[str, Any]:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as error:
        raise AssetError("Semble MCP support is missing; run `uv sync`") from error

    workspace.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace.state.chmod(0o700)
    with tempfile.TemporaryDirectory(dir=workspace.state) as temporary:
        repository = Path(temporary) / "repo"
        shutil.copytree(fixture, repository)
        parameters = StdioServerParameters(
            command=str(semble_command()),
            args=["--content", "all"],
            env={
                **os.environ,
                "PYINSTALLER_RESET_ENVIRONMENT": "1",
                "SEMBLE_CACHE_LOCATION": str(workspace.retrieval_cache),
            },
        )
        with tempfile.TemporaryFile(mode="w+") as errors:
            try:
                async with stdio_client(parameters, errlog=errors) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        warmup_started = time.perf_counter()
                        warmup = await session.call_tool(
                            "search",
                            {
                                "query": "callback-v7",
                                "repo": str(repository),
                                "top_k": 3,
                            },
                        )
                        if warmup.isError:
                            raise AssetError("Semble failed to build the initial canary index")
                        warmup_seconds = time.perf_counter() - warmup_started
                        changed = repository / "src" / "fresh.py"
                        changed.write_text(
                            "def NEBULA_INCREMENTAL_SENTINEL():\n"
                            '    return "fresh-index-content"\n'
                        )
                        # Semble 0.5.2 delays revalidation by three times the
                        # initial build duration. The warmup bounds that duration.
                        incremental = await _wait_for_incremental_search(
                            session, repository, timeout=max(5.0, 3 * warmup_seconds + 1.0)
                        )
            except Exception as error:
                errors.seek(0)
                detail = errors.read().strip()
                message = "Semble stopped during repository retrieval verification"
                if detail:
                    message = f"{message}:\n{detail}"
                raise AssetError(message) from error
    return incremental


async def _wait_for_incremental_search(
    session: Any, repository: Path, *, timeout: float
) -> dict[str, Any]:
    started = time.perf_counter()
    deadline = started + timeout
    rank = None
    attempts = 0
    tool_error = False
    while time.perf_counter() < deadline:
        attempts += 1
        try:
            response = await asyncio.wait_for(
                session.call_tool(
                    "search",
                    {
                        "query": "NEBULA_INCREMENTAL_SENTINEL",
                        "repo": str(repository),
                        "top_k": 3,
                    },
                ),
                timeout=deadline - time.perf_counter(),
            )
        except TimeoutError:
            break
        tool_error = bool(response.isError)
        if tool_error:
            break
        results = json.loads(response.content[0].text).get("results", [])
        rank = next(
            (
                number
                for number, result in enumerate(results, 1)
                if result.get("file_path") == "src/fresh.py"
            ),
            None,
        )
        if rank == 1:
            break
        await asyncio.sleep(min(0.1, max(0.0, deadline - time.perf_counter())))
    return {
        "passed": not tool_error and rank == 1,
        "rank": rank,
        "attempts": attempts,
        "tool_error": tool_error,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
    }


@contextmanager
def _environment(name: str, value: str) -> Iterator[None]:
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def _append_json_line(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
    path.chmod(0o600)
