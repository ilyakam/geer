# Model evaluation

Use this policy to choose which model, quantization, or runtime deserves a
trial and to qualify the exact profile on a particular Mac. Keep those
decisions separate. Full local benchmark reproduction is not required for
screening candidates.

## Eligibility

Apply the hardware, context, and image requirements in [AGENTS.md](../AGENTS.md)
and the compatibility checks in [CONTRIBUTING.md](../CONTRIBUTING.md).
Require correct task behavior and safe memory at a minimum of 32,768 total
tokens, including tools, history, image tokens, and generation reserve.
Tool continuation, stopping, retrieval, cancellation/resumption, and the real
Pi/T3 repository read/edit/test turn must pass. For image-capable models,
verify screenshot interpretation through Pi/T3 in every selected profile.

A critical regression excludes the exact failing profile. Failure at a larger
context tier does not exclude a separately verified smaller tier. Preserve
incomplete checks as incomplete; a timeout is not a passing result.

## Ranking

After eligibility checks, use 50% reported quality, 30% verified useful context,
and 20% measured speed as a provisional ranking. Compare profiles only on the
same physical chip and memory configuration. Keep the underlying measurements
beside the score, and use a qualitative shortlist when the evidence cannot
support a common numerical comparison.

For reported quality, freeze a small common benchmark panel before comparing
candidates. Average scores within each benchmark family, then give families
equal weight. Compare matching benchmark versions and metrics, and record
harness differences and reused reports. Use published percentages directly;
convert other bounded metrics only when their documented scale permits it.
Missing or incompatible results remain unknown. Do not silently change the
panel per candidate or use candidate-relative minimum/maximum normalization.

Upstream quality is a model-level proxy for each conversion. Do not invent
quantization penalties. Every exact artifact must pass the same small sanity
suite; passing it establishes scoped behavior, not the upstream benchmark score.

Score verified useful context as 32K = 50, 64K = 75, and 128K or greater = 100,
where K means 1,024 tokens. Larger windows receive no extra initial ranking
credit; record their measured capacity. A tier needs correct completion
without silent truncation, OOM, sustained memory pressure, or unacceptable
latency. Above 32K, use a representative task that benefits from retaining the
additional material. Padding alone proves capacity, not usefulness.

For speed, freeze a measured reference on that host. Use Ornith where it is
eligible, otherwise the first eligible comparator. Use identical fixtures,
output budgets, sampling and thinking settings, and cache conditions. Measure
decode tokens/s primarily; report fresh-prompt time to first token and prefill
tokens/s alongside it. Separate model loading from time to first token.

For a numerical comparison on the common 32K workload, use:

```text
f(r) = 100 * r / (1 + r)
Speed = 0.8 * f(decode / reference_decode)
      + 0.2 * f(reference_TTFT / TTFT)
Total = 0.50 * Quality + 0.30 * Context + 0.20 * Speed
```

Prefill remains separately reported because it already contributes to TTFT.
Use the initial measurements to agree on acceptable fresh-32K TTFT and minimum
decode rate before calling a profile qualified. The request timeout below
bounds investigation; it is not an interactive usability standard.

## Bounded trials

Cap the initial two-profile inference/integration comparison at 90 minutes
total, with at most 45 minutes per profile including loading. Stop individual
requests after three minutes. Allow one retry only for an identified
infrastructure fault, within the same total budget. Record unfinished checks
and stop when the budget expires. Downloads, conversion, and adapter
implementation are separate operations with their own stated bounds.

Start with short capability checks, then run two matched requests at short
context and two at 32K. Attempt 64K only after passing; attempt 128K only when a
task justifies it and time remains. Change quantization or runtime when an
observed memory, context, speed, or compatibility result supplies a reason.
Compare runtime optimizations with unchanged target weights and fixtures;
compare a new quantization against the pinned baseline.

Record exact revisions, physical hardware, cache state, prompt/completion
sizes, latency, throughput, and peak memory pressure and swap. Distinguish
reported upstream quality, inspected/estimated results, passed scoped checks,
and physical qualification of the exact chip/RAM/profile. A memory-capped
test on a larger Mac does not qualify a smaller physical host. Keep working
results and publication decisions in the experiment ledger.
