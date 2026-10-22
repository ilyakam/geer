# Pinned runtime and clean-VM validation

Experiment period: 2026-07-25 through 2026-07-30.
Outcome: passed scoped clean-VM setup and integration checks.
Disposition: historical runtime-delivery baseline; not physical-64-GB proof.

## Question and scope

Could a clean Apple Silicon environment install the pinned local stack and
use it through T3, including retrieval and retained-state recovery?

## Configuration

- macOS 15.6.1 ARM64 Tart VM with 64 GiB assigned memory and a virtual Apple
  GPU. The host's exact chip and memory were not attached to these VM results.
- oMLX 0.5.3, revision `31d07a7fdbbb499d4520188fb7e8f7bdbfe4c770`;
  the historical archive recorded 322,252,599 bytes and SHA-256
  `b07264fbe797ad88bf3c9109522538f1ac264782231446c12031aa6bc6ab7f00`.
- Claude Code 2.1.220, T3 Code 0.0.28, Semble 0.5.2, and `uv` 0.11.32.
  The model was the then-current Ornith 1.0 6-bit path; a separate immutable
  model snapshot ID was not retained alongside these VM timing observations.

## Result

The VM completed setup, model loading, retrieval, a T3 repository-read turn,
and restart/reinstall checks. A clean-machine install from pinned source
encountered a developer-tools prerequisite. Runtime delivery changed to a
prebuilt archive of that oMLX revision. No alternate inference engine was
evaluated by that packaging change.

The record contains bounded request and T3 turn timings, but insufficient
matched workload and cache details for a performance comparison. A VM with
64 GiB assigned memory does not establish behavior on a physical 64 GB Mac.
The archive hash above identifies this historical build, not the runtime
archive used by every subsequent release.

## Decision and revisit condition

Keep a prebuilt, integrity-checked runtime as the clean-install baseline.
Repeat isolated setup, retrieval, restart, and retained-state checks when
runtime pins or packaging change. Require actual target-host measurements
before inferring physical memory-tier support. Current Pi integration needs
its own [harness checks](2026-09-30-pi-harness.md).

## Sources

- [Pinned inference source](https://github.com/jundot/omlx/tree/31d07a7fdbbb499d4520188fb7e8f7bdbfe4c770).
- [Historical runtime packaging](https://github.com/ilyakam/geer/blob/0.1.0/tools/build_runtime.py)
  and [initial public release](https://github.com/ilyakam/geer/releases/tag/0.1.0).
