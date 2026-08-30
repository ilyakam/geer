---
language:
- en
license: mit
library_name: mlx
pipeline_tag: image-text-to-text
base_model: ornith-ai/Ornith-1.5-35B-A3B
tags:
- geer
- mlx
- qwen3_5_moe
- coding
---

# Geer Ornith 1.5 35B-A3B (6-bit MLX)

This repository contains Geer's independently produced 6-bit MLX conversion
of [Ornith-1.5-35B-A3B][upstream]. It is intended for local agentic coding on
Apple Silicon through [Geer][geer]. The model identity is intentionally
transparent: this is Ornith-1.5-35B-A3B converted by Geer, not a new
foundation model.

## Build

- Upstream model: `ornith-ai/Ornith-1.5-35B-A3B`
- Upstream revision:
  `e4dfb35a93d4b6822a811a7676f3488514abe7e2`
- Architecture: `qwen3_5_moe`
- Quantization: affine 6-bit, group size 64
- Source payload: 71,928,654,040 bytes (approximately 67.0 GiB)
- Converted size: recorded in `geer-build-manifest.json`
- Conversion runtime: MLX 0.32.0, MLX-LM 0.31.3, MLX-VLM 0.6.3
- Hugging Face tooling: `huggingface-hub` 1.24.0, `hf-xet` 1.5.2
- Safetensors: 0.8.0

The repository includes:

- `geer-recipe.toml`, the complete pinned conversion recipe;
- `geer-source-manifest.json`, hashes for the downloaded BF16 source;
- `geer-build-manifest.json`, hashes for every converted output; and
- the model, tokenizer, chat template, processor configuration, licenses, and
  attribution notices required for review of the converted artifact.

The reproducible conversion tool and tensor-layout transformation are
available in the [Geer source repository][geer]. The upstream 1.5 checkpoint
contains native `mtp.*` tensors; the pinned MLX-VLM 0.6.3 conversion path
sanitizes those tensors out, so this candidate must be qualified before any
publication or activation claim.

## Use with Geer

Geer downloads a published repository only at an immutable revision, verifies
every file against `geer-build-manifest.json`, and activates the verified
Hugging Face snapshot without copying model bytes. This candidate is prepared
for review and is not activated automatically.

## Evaluation status

The upstream model card reports benchmark results for Ornith-1.5. Geer has not
reproduced those results for this MLX quantization, so upstream scores are not
measured results for this conversion. The repository metadata supports
multimodal inputs, but Geer qualification must separately verify image and
text serving; the current Geer workflow is validated for text-based agentic
coding.

## License and attribution

The pinned upstream model card declares Ornith-1.5-35B-A3B under the MIT
license and links to a license file. The pinned snapshot currently does not
include that file, so the exact upstream license text and any inherited
component obligations must be confirmed before redistribution. This candidate
preserves the Qwen Apache notice carried by the existing Geer Ornith model;
that notice is not a substitute for upstream legal review.

Geer is an independent project and is not affiliated with or endorsed by
Ornith AI, Deep Reinforce, Alibaba Cloud, or Google.

[geer]: https://github.com/ilyakam/geer
[upstream]: https://huggingface.co/ornith-ai/Ornith-1.5-35B-A3B/tree/e4dfb35a93d4b6822a811a7676f3488514abe7e2
