---
language:
- en
license: mit
library_name: mlx
pipeline_tag: image-text-to-text
base_model: deepreinforce-ai/Ornith-1.0-35B
tags:
- geer
- mlx
- qwen3_5_moe
- coding
---

# Geer Ornith 1.0 35B-A3B (4/8-bit MLX)

This repository contains Geer's independently produced mixed 4/8-bit MLX
conversion of [Deep Reinforce AI's Ornith-1.0-35B][upstream]. It is intended
for local agentic coding on Apple Silicon through [Geer][geer].

The model identity is intentionally transparent: this is Ornith-1.0-35B,
converted by the Geer project. It is not a new foundation model.

## Build

- Upstream model: `deepreinforce-ai/Ornith-1.0-35B`
- Upstream revision:
  `5df2ed3f675c7beaa490328cc70bb573b65fb660`
- Architecture: `qwen3_5_moe`
- Quantization: affine mixed 4/8-bit, group size 64
- Four-bit tensors: routed expert gate, up, and down projections
- Eight-bit tensors: other eligible language-model matrices, including
  embeddings, attention and Gated DeltaNet projections, routers, shared
  experts, and the language-model head
- BF16 tensors: vision tower, norms, and other non-quantized parameters
- Converted payload: 21,643,961,289 bytes (approximately 20.2 GiB)
- Conversion runtime: MLX 0.32.0, MLX-LM 0.31.3, MLX-VLM 0.6.3
- Hugging Face tooling: `huggingface-hub` 1.24.0, `hf-xet` 1.5.2
- Safetensors: 0.8.0

The repository includes:

- `geer-recipe.toml`, the complete pinned conversion recipe;
- `geer-source-manifest.json`, hashes for the downloaded BF16 source;
- `geer-build-manifest.json`, hashes for every converted output; and
- the model, tokenizer, chat template, processor configuration, licenses, and
  attribution notices required to use the converted artifact.

The reproducible conversion tool and tensor-layout transformation are
available in the [Geer source repository][geer].

## Use with Geer

Geer downloads this repository at an immutable revision, verifies every file
against `geer-build-manifest.json`, and activates the verified Hugging Face
snapshot without copying the model into another directory. Geer 0.1.0 selects
this conversion on 32 GB and 48 GB Macs, with 64K and 128K context windows
respectively and BF16 KV cache.

## Evaluation status

Deep Reinforce AI publishes results for the upstream BF16 Ornith model in its
[model card][upstream]. Geer has not yet reproduced the upstream benchmark
suite for this mixed quantization, so upstream scores should not be treated as
measured results for this conversion. Although the upstream architecture and
repository metadata support image-and-text inputs, Geer 0.1.0 has validated
this conversion only for text-based agentic coding workflows.

## License and attribution

Deep Reinforce AI declares Ornith-1.0-35B under the MIT license. Its model card
states that Ornith-1.0-35B was post-trained on Qwen 3.5; the applicable Qwen
Apache License 2.0 text and attribution are preserved here. See `LICENSE`,
`LICENSE-QWEN`, and `NOTICE` before using or redistributing the model.

Geer is an independent project and is not affiliated with or endorsed by Deep
Reinforce AI or Alibaba Cloud.

[geer]: https://github.com/ilyakam/geer
[upstream]: https://huggingface.co/deepreinforce-ai/Ornith-1.0-35B/tree/5df2ed3f675c7beaa490328cc70bb573b65fb660
