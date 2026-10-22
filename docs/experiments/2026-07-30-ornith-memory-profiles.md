# Ornith 1.0 memory, context, and mixed quantization

Experiment period: 2026-07-30 through 2026-08-01.
Outcome: conversion and profile selection recorded; target-host checks incomplete.
Disposition: superseded by the Ornith 1.5 profiles.

## Question and scope

Could context limits and protected mixed quantization make the existing
Ornith model useful on smaller-memory Macs? The work combined memory
observations on a large host with conversion and setup-selection changes.

## Configuration

- Upstream: `deepreinforce-ai/Ornith-1.0-35B`, revision
  `5df2ed3f675c7beaa490328cc70bb573b65fb660`.
- Baseline: affine 6-bit. Mixed recipe: 4-bit routed expert projections,
  8-bit eligible remaining language matrices, group size 64; retained
  unquantized components including vision in BF16.
- Conversion pins: MLX 0.32.0, MLX-LM 0.31.3, MLX-VLM 0.6.3.
- Initial mixed distribution: revision
  `1ed82bfd974f3d5ccc3cbfbd044a6357f1b09c03`, with a recorded distribution
  total of 21,643,965,955 bytes. Later 0.1.0 pins used a different snapshot.
- Memory-study host: M4 Max, 128 GB unified memory; oMLX 0.5.3, BF16 KV.
  No physical 32, 48, or 64 GB qualification is established by this record.

## Result

The memory study reported about 27.5 GB actual and 28.42 GB estimated MLX
memory for a single-model load. The KV estimate was approximately 80 KiB per
token. Requests reached roughly 113K tokens; the full 262,144-token window
was not validated.

An initial selection rule kept 6-bit weights with 128K context at 64 GB.
The later mixed-profile implementation selected 4/8-bit with 64K at 32 GB,
4/8-bit with 128K at 48 GB, and 6-bit with 256K at 64 GB and above.
Recipes, manifests, and tests establish those selection rules. They do not
establish comparative quality or real-host memory safety at each tier.

The backfill was limited to the retained implementation and model card for
the mixed profile. Additional hardware measurements were not established.

## Decision and revisit condition

Retain both the earlier context rule and the later policy change so future
work can distinguish configuration from qualification. Use current Ornith 1.5
for new product work. Revisit a lower-memory tier only with the exact
artifact on that physical host, measuring quality, context, latency, pressure,
and swap against a pinned baseline.

## Sources

- [Historical mixed recipe](https://github.com/ilyakam/geer/blob/0.1.0/model-recipes/ornith-1.0-35b-4-8bit.toml)
  and [0.1.0 distribution pin](https://github.com/ilyakam/geer/blob/0.1.0/model-distributions/ornith-1.0-35b-4-8bit.toml).
- [Ornith 1.5 follow-up](2026-08-20-ornith-1.5-mlx.md).
