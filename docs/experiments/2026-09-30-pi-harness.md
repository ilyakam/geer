# Pi harness integration

Experiment period: 2026-09-30 through 2026-10-01.
Outcome: passed scoped harness and installer checks.
Disposition: incorporated into Geer 0.3.0, replacing the Claude Code harness.

## Question and scope

Could T3 Code drive the local Ornith model through Pi while retaining coding
tools, Semble retrieval, skills, cancellation, and resumable sessions?

## Configuration

- Model: Geer's Ornith 1.5 35B-A3B 6-bit MLX artifact, revision
  `c3d5e9008c891ef96da42630ef8b484aef696669`.
- Inference: oMLX 0.5.3, revision
  `31d07a7fdbbb499d4520188fb7e8f7bdbfe4c770`.
- Harness: Pi 0.99.1, revision
  `d86654abb8862e201933517d6f1fce9f88dd117f`, official Darwin arm64 binary.
- Interface: Geer's ACP-to-Pi RPC adapter, T3 Code 0.0.44 and a compatible
  0.0.43 Nightly installation. T3's `grok` driver carried ACP; it did not
  change the inference model to a Grok model.
- Hardware: Apple M4 Max with 128 GB unified memory.

## Result

Real T3 turns exercised repository reads, edits, test execution, selected
skills with supporting scripts, and session resumption. Both Semble retrieval
tools returned successful results. The integration retained Geer's separate
configuration and borrowed user skills through Pi's native paths.

Verification exposed cancellation during delayed MCP startup and compaction,
plus timeout-recovery issues. The final change included fixes and regression
coverage. Its recorded validation passed 368 tests, Ruff, package checks,
and native/frozen application checks. Geer 0.3.0 shipped a signed, notarized,
and stapled installer.

These are results for the pinned Ornith/Pi configuration. They do not qualify
other model IDs, reasoning profiles, smaller Macs, or future Pi/T3 versions.

## Decision and revisit condition

Use Pi for the released Geer integration. Revisit when Pi's RPC contract,
T3's ACP behavior, or the selected model changes. Repeat actual T3
read/edit/test, retrieval, cancellation, and resumption checks after such a
change; a direct inference request is insufficient evidence for the full
integration.

## Sources

- [Pi manifest](../../packaging/pi-runtime.json),
  [T3 manifest](../../packaging/t3-desktop.json), and
  [third-party notices](../../THIRD_PARTY_NOTICES.md).
- [Pi integration and validation](https://github.com/ilyakam/geer/pull/5).
- [Geer 0.3.0 release](https://github.com/ilyakam/geer/releases/tag/0.3.0).
