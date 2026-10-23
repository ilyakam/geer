from __future__ import annotations


def benchmark_comparison() -> dict[str, object]:
    """Return display metadata for the selected upstream model comparison."""
    return {
        "title": "Selected upstream results",
        "note": "Scores are percentages reported upstream, not Geer's quantized-model scores.",
        "source_url": "https://arxiv.org/html/2610.28578v1#S3.T1",
        "methodology": (
            "SWE-bench Pro uses OpenHands at 256K for Ornith and Mini-SWE-Agent at 200K "
            "with compaction for Sage. Sage reuses original reports where available and "
            "evaluates missing results with its own protocol. Ornith's IFBench and "
            "PinchBench lineage is inferred from that policy, not a per-run manifest. "
            "The full comparison also reports DeepResearchBench II at 48.6 for Ornith "
            "and 43.7 for Sage without orchestration."
        ),
        "rows": [
            {
                "id": "swe-bench-pro",
                "label": "SWE-bench Pro",
                "description": "Repository issue resolution.",
                "ornith": 59.6,
                "sage": 61.2,
            },
            {
                "id": "ifbench",
                "label": "IFBench strict prompt accuracy",
                "description": (
                    "Strict prompt accuracy for instruction following, not a coding score."
                ),
                "ornith": 44.6,
                "sage": 81.6,
            },
            {
                "id": "pinchbench-v1",
                "label": "PinchBench-v1 general agent tasks",
                "description": "General agent tasks.",
                "ornith": 88.6,
                "sage": 90.0,
            },
        ],
    }
