# Terminal-Bench product-path evaluation

Experiment date: 2026-07-27.
Outcome: incomplete; no complete benchmark score established.
Disposition: preserve the frozen configuration and unfinished-run boundary.

## Question and scope

How would the local Ornith model perform through Geer's actual coding-agent
harness and repository retrieval, rather than a substitute inference-only
agent? Harbor supplied tasks to the Claude Code product path. T3 desktop
automation was outside this benchmark.

## Configuration

- Dataset: `terminal-bench/terminal-bench-2-1`, revision
  `sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a`.
- Harbor 0.20.0, Claude Code 2.1.220, oMLX 0.5.3, Semble 0.5.2.
- Ornith 1.0 6-bit revision `bd7b8b380f792d3b8f254d8747767da22f686262`;
  retrieval model revision `e9d2a44ca6a05ac6685f3b23709ea57eb7352d5b`.
- One attempt per task, concurrency one, no automatic retries; official
  resource and verifier settings retained. The plan covered 89 tasks, with
  Rosetta and nested-QEMU execution handled separately.
- Exact host chip and RAM were not established in the retained benchmark
  methodology and checkpoint. They must not be inferred from other trials.

## Result

The run stopped at a partial checkpoint. Some completed task results were
valid, while verifier timeouts made others infrastructure-invalid. The run
did not obtain a valid official result for all 89 tasks. No final aggregate
or leaderboard-comparable score is reported here.

The adapter and frozen method were retained. Neither a partial-task ratio
nor an upstream BF16 score establishes this Geer stack's complete score.

## Decision and revisit condition

Resume this historical configuration only with enough time for all tasks,
recovery of infrastructure-invalid trials, and complete verifier accounting.
Keep invalid infrastructure outcomes separate from valid model failures.
For the current Pi product, define a new pinned harness configuration and
record it as a follow-up rather than combining its results with this run.

## Sources

- [Harbor framework](https://github.com/harbor-framework/harbor).
- [Original model experiment](2026-07-24-ornith-1.0-6bit.md).

This summary records Geer's own frozen-run methodology and incomplete
outcome. It includes no benchmark tasks, trajectories, or verifier payloads.
