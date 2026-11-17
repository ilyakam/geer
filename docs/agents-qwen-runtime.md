# Experimental Qwen runtime

Read this before changing Qwen acquisition, serving, or context limits.
User instructions belong in [README.md](../README.md).

## Pinned inputs and ownership

Geer installs Unsloth's existing Qwen3.5-9B Q4_K_M GGUF and complete F16
projector. It does not train, distill, or convert them. The exact repository
revision, sizes, hashes, and declared lineage live in
`model-distributions/qwen3.5-9b-q4-k-m.toml`. The exact conversion parent and
quantizer recipe have not been independently reconstructed.

`packaging/llama-runtime.json` pins the official, unpatched llama.cpp b11901
Apple Silicon release and its complete file/link inventory. Do not substitute
an experiment binary. Preserve the upstream licenses in both downloads.
The release requires macOS 13.3 or newer; reject older hosts before acquisition
or activation. This does not lower Geer's documented macOS 15 recommendation.

`qwen_distribution.py` installs and verifies the two approved files without
inventing Geer conversion provenance. `llama_distribution.py` verifies and
installs the runtime. `llama_runtime.py` owns serving and memory supervision;
`runtime.py` dispatches from the verified active model. Before activation,
setup explicitly requests `bootstrap_runtime(..., backend="llama.cpp")`.
The omitted backend retains the existing oMLX bootstrap behavior.

Keep the active-model link, saved choice, T3 preferences, and histories intact.
Pi/T3 retain tool dispatch, cancellation, settlement, and JSONL resumption.
Metadata discovery must not start inference. The llama.cpp server and its
supervisor receive no cloud, Pi, or MCP credentials. A failed or unknown memory
sample stops the owned server; warnings alone do not. Critical pressure or
more than 1 GiB additional used swap also stops it.

## Configured context and estimates

Qwen uses Q4_0 K/V, four recurrent checkpoints, full F16 vision, and a
4,096-token generation reserve within the total window. Configure 65,536
tokens below 24 GiB and 131,072 from 24 GiB. The larger cap is an estimate,
not a measured useful window or a promise of physical fit on 24 GiB hardware.
Eligibility across M-series chips does not qualify every configuration.

The official [Qwen configuration](https://huggingface.co/Qwen/Qwen3.5-9B/blob/c202236235762e1c871ad0ccb60c8ee5ba337b9a/config.json)
has 24 recurrent layers and eight full-attention layers, four K/V heads, and
head width 256. The [Transformers architecture documentation](https://huggingface.co/docs/transformers/model_doc/qwen3_5)
and [Gated Delta Networks paper](https://arxiv.org/abs/2412.06464) explain why
recurrent state does not grow with history like an attention K/V cache.

At the [pinned runtime source](https://github.com/ggml-org/llama.cpp/tree/5320c87ff0d994845b44ce7976f10055ec697447),
Q4_0 stores 32 values in 18 bytes (`ggml/src/ggml-common.h`). One slot's
attention K/V payload per position is therefore:

```text
8 layers × 2 K/V × 4 heads × 256 values × 18/32 bytes = 9,216 bytes
64K: 576 MiB; 128K: 1,152 MiB; increase: 576 MiB
```

This excludes headers, allocator capacity, graph buffers, vision, and the host.
The hybrid cache also retains roughly 50.25 MiB of live F32 recurrent state;
four populated partial checkpoints add roughly 201 MiB, excluding overhead.
Check `src/llama-memory-hybrid.cpp` and `src/llama-kv-cache.cpp` when changing
these assumptions.

Metal's F16 K/V staging component grows from 256 MiB at 64K to 512 MiB at
128K per full-attention node. A 128-query F16 mask grows from 16 to 32 MiB.
See `ggml/src/ggml-metal/ggml-metal-ops.cpp`. These component sizes cannot be
summed into a measured peak: buffer lifetimes and reuse matter. The extra
8 GiB on a 24 GiB Mac also serves macOS, T3, Pi, retrieval, and vision.
128K is a conservative starting estimate relative to the model's native 256K;
it still requires a real 24 GiB host test.

## Validation and release boundaries

Test source and frozen payload discovery, verified cache reuse, activation
rollback, metadata without inference, authenticated readiness, owned shutdown,
and retained histories. Exercise a real native read/edit/test turn, images,
retrieval, skills, background generation, cancellation, and resumption on M1.
Measure latency before setting an acceptance target. Keep temporary fixtures
and raw transcripts private and outside Git.

Build and inspect the unsigned PKG locally. Developer ID signing,
notarization, and Gatekeeper validation belong to the release Mac. Existing
Sage/Ornith live regressions need a compatible larger-memory host; MBP16 does
not substitute for physical 24 GiB qualification.
