# Experiment history

These records capture what Geer tried, what the evidence establishes, and what
would justify trying again. Historical records were backfilled on 2026-10-22;
their filenames use the original experiment date. They describe the versions
and machines tested at that time. Current installation requirements remain in
[README.md](../../README.md).

## Before starting an experiment

Search this index and the dated records for the model's names and aliases,
artifact, quantization, runtime, and proposed technique. Maintainers should
also consult the working ledger in `geer-private/experiments/` when accessible.
Public contributors can work from these summaries; note when private history
could not be checked. Identify the changed assumption before repeating a
previous trial. A new revision may address an old blocker; a new announcement
alone is not evidence that it does.

## Record format

Use `docs/experiments/YYYY-MM-DD-NAME.md` for a public record. Keep the matching
working record and source provenance in the private ledger. Use the date the
experiment began, and add dated follow-ups when its conclusion changes.

Each record includes:

- The question and scope, distinguishing inspection, conversion, direct
  inference, harness validation, and physical-host qualification.
- Exact model and artifact revisions, quantization, runtime versions,
  hardware, memory, and settings relevant to the result. Mark missing details
  as unrecorded.
- Observed results and limitations. Distinguish a failed check from one that
  was never run, and a memory cap from a test on that size of machine.
- The decision and a concrete condition for revisiting it.
- Public evidence and provenance links. Keep license review separate from
  technical success.

Use outcomes such as **inspected only**, **failed**, **incomplete**, or
**passed scoped checks**. Record retirement or supersession separately so an
older success does not imply current support.

## Publication rules

New working entries start private. Prepare a public summary only after the
maintainer requests it, and publish it only after explicit review. Include
Geer's own technical findings and public evidence appropriate to that scope.
Keep private conversation references, private repository links, machine paths,
raw logs, prompts, model outputs, credentials, weights, and third-party source
copies out of public records. Preserve unresolved provenance and licensing
assessments in the private ledger until reviewed; do not turn them into public
allegations or imply redistribution approval from a successful test.

Retain negative findings and research-only outcomes. Link later trials to the
earlier record and explain which blocker or configuration changed. Update
this index when adding a record.

## Records

The dated records below are historical summaries, not new qualification runs.

| Record | Finding |
| --- | --- |
| [2026-07-24: Original Ornith 1.0 6-bit conversion](2026-07-24-ornith-1.0-6bit.md) | 6-bit conversion and bounded inference passed; later retired. |
| [2026-07-25: Pinned runtime and clean-VM validation](2026-07-25-runtime-vm.md) | Clean-VM integration passed; physical memory-tier behavior unproven. |
| [2026-07-27: Terminal-Bench product-path evaluation](2026-07-27-terminal-bench.md) | Frozen product-path run incomplete; no complete score. |
| [2026-07-30: Ornith 1.0 memory, context, and mixed quantization](2026-07-30-ornith-memory-profiles.md) | Mixed conversion and selection rules recorded; target-host checks incomplete. |
| [2026-08-15: Deferred Claude Code tool discovery](2026-08-15-claude-tool-discovery.md) | Deferred discovery reduced one first-prefill workload; historical harness. |
| [2026-08-15: Qwen3.8 MLX conversion, context, and interfaces](2026-08-15-qwen3.8-mlx.md) | 128 GB checks passed; smaller hosts and near-limit latency unqualified. |
| [2026-08-16: Qwen3.8 MTP exactness and proposal quantization](2026-08-16-qwen3.8-mtp.md) | Broader exact-output gate failed despite throughput gains. |
| [2026-08-18: Qwen3.8 3-bit profiles on a 16 GB M1](2026-08-18-qwen3.8-3bit-m1.md) | Limited M4 checks passed; physical M1 loads failed. |
| [2026-08-20: Ornith 1.5 MLX conversion and migration](2026-08-20-ornith-1.5-mlx.md) | Conversion and installed migration incorporated into 0.2.0. |
| [2026-08-25: Qwen3.6 MoE preflight for a 16 GB M1](2026-08-25-qwen3.6-m1-preflight.md) | Inspection only; bounded expert streaming unimplemented. |
| [2026-09-30: Pi harness integration](2026-09-30-pi-harness.md) | Real T3/Pi checks passed; incorporated into 0.3.0. |
| [2026-10-22: Sage-A1.5 qualification and model selection](2026-10-22-sage-a1.5-qualification.md) | Both Sage quantizations passed scoped M4 Max 128 GiB checks; 0.4.0 shipped both families with Sage recommended. |
