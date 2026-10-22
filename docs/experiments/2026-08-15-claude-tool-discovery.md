# Deferred Claude Code tool discovery

Experiment period: 2026-08-15 through 2026-08-16.
Outcome: passed scoped protocol and live integration checks.
Disposition: historical; Geer 0.3.0 replaced the Claude Code harness with Pi.

## Question and scope

Could deferred MCP tool discovery reduce the first local prefill while
preserving visible reasoning, tool continuation, and retrieval?

## Configuration

- Claude Code 2.1.220 with ToolSearch enabled and summarized thinking display.
- oMLX 0.5.3 revision `31d07a7fdbbb499d4520188fb7e8f7bdbfe4c770`.
- Qwen3.8-27B 6-bit and the retained Ornith model; Apple M4 Max with 128 GB.
  The timing comparison used the Qwen path. The exact Ornith artifact
  revision was not attached to that comparison.

## Result

The recorded initial prompt fell from 22,080 to 13,347 tokens. First visible
reasoning fell from 118.6 to 63.3 seconds, approximately 47% in that canary.
Both model aliases completed deferred discovery and live retrieval. Adaptive
reasoning remained visible. These are single-configuration observations,
not a general latency estimate for other workloads or machines.

The protocol capture checked that MCP schemas were absent from the first
request and that deferred references could be resolved later. It was
distinct from a live model test. An unsigned package was rebuilt and
inspected, but that experiment did not install or publish its launcher change.

## Decision and revisit condition

Retain this as evidence for the historical Claude protocol. Revisit only for
a deliberate Claude integration or a changed discovery protocol, using an
exact harness-version capture followed by live retrieval and tool-result
continuation checks. Pi has its own tool and session behavior; this latency
result does not transfer automatically.

## Sources

- [Qwen model and runtime configuration](2026-08-15-qwen3.8-mlx.md).
- [Released Pi replacement](2026-09-30-pi-harness.md).

The capture contents and timing evidence remain private; this record retains
only content-free measurements and the historical compatibility boundary.
