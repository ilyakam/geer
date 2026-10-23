from __future__ import annotations

import json

from geer.model_benchmarks import benchmark_comparison


def test_selected_upstream_scores_and_json_contract() -> None:
    comparison = json.loads(json.dumps(benchmark_comparison(), allow_nan=False))

    assert set(comparison) == {"title", "note", "source_url", "methodology", "rows"}
    for field in ("title", "note", "source_url", "methodology"):
        assert isinstance(comparison[field], str)
        assert comparison[field]
    assert comparison["source_url"] == "https://arxiv.org/html/2610.28578v1#S3.T1"
    assert [
        (row["id"], row["label"], row["ornith"], row["sage"])
        for row in comparison["rows"]
    ] == [
        ("swe-bench-pro", "SWE-bench Pro", 59.6, 61.2),
        ("ifbench", "IFBench strict prompt accuracy", 44.6, 81.6),
        ("pinchbench-v1", "PinchBench-v1 general agent tasks", 88.6, 90.0),
    ]
    for row in comparison["rows"]:
        assert set(row) == {"id", "label", "description", "ornith", "sage"}
        assert row["description"]


def test_comparison_retains_interpretation_limits() -> None:
    comparison = benchmark_comparison()

    assert "not Geer's quantized-model scores" in comparison["note"]
    methodology = comparison["methodology"]
    assert "OpenHands at 256K" in methodology
    assert "Mini-SWE-Agent at 200K with compaction" in methodology
    assert "inferred" in methodology
    assert "not a per-run manifest" in methodology
    assert "48.6 for Ornith and 43.7 for Sage without orchestration" in methodology
    assert "quality panel" not in methodology
    assert "50/30/20" not in methodology
    instruction_row = next(row for row in comparison["rows"] if row["id"] == "ifbench")
    assert "Strict prompt accuracy" in instruction_row["description"]
    assert "not a coding score" in instruction_row["description"]


def test_callers_cannot_change_shared_comparison_metadata() -> None:
    comparison = benchmark_comparison()
    comparison["rows"][0]["sage"] = 0.0
    comparison["rows"].clear()
    comparison["note"] = "changed by renderer"

    fresh = benchmark_comparison()

    assert len(fresh["rows"]) == 3
    assert fresh["rows"][0]["sage"] == 61.2
    assert fresh["note"] != "changed by renderer"
