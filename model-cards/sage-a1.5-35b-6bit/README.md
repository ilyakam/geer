---
language:
- en
license: apache-2.0
library_name: mlx
pipeline_tag: image-text-to-text
base_model: InternScience/Sage-A1.5
tags:
- geer
- mlx
- qwen3_5_moe
- coding
---

# Geer Sage-A1.5 35B-A3B (6-bit MLX)

This is Geer's independent 6-bit MLX conversion of
[Sage-A1.5][upstream] from Shanghai AI Laboratory. It retains the model's
native vision tower and processor assets.

## Build and changes

- Source: `InternScience/Sage-A1.5`
- Immutable source revision: `0e3205cf441cf01df0c0d238cc4738d0e43b4ca7`
- BF16 source payload: 71,931,809,355 bytes, including the source MTP file
- Architecture: `qwen3_5_moe`
- Conversion: `qwen3_5_moe_unfused_experts` in [Geer's build tool][build]
- Conversion packages: MLX 0.32.0, MLX-LM 0.31.3, MLX-VLM 0.6.3,
  huggingface-hub 1.24.0, hf-xet 1.5.2, safetensors 0.8.0
- Converted size and file hashes: recorded by `geer-build-manifest.json`

Affine 6-bit, group size 64, for eligible language-model matrices.
The vision tower, norms, and other non-quantized parameters remain BF16.
The transformation fuses the source's individually stored routed experts for
MLX-VLM. Its pinned sanitizer omits the source `mtp.*` tensors. This artifact
therefore does not supply native MTP or claim speculative-decoding support.

Geer changes the weight layout and precision, rewrites conversion metadata,
and supplies this card, licenses, and attribution notices. MLX-VLM may also
normalize tokenizer and processor metadata. `NOTICE` identifies these changes.
The source and build manifests record the exact original and output files,
including the tokenizer, chat template, configurations, and tensors.

The artifact includes the pinned `geer-recipe.toml`, complete
`geer-source-manifest.json`, and `geer-build-manifest.json`. The completed
conversion passed full file-size and SHA-256 verification. Reproduce it with
Geer's `download`, `convert`, and `verify` commands using the included recipe.

## Evaluation and use

Upstream benchmark reports describe the source model. They are reported
quality proxies, not measured results for this quantization. Geer does not
claim that conversion preserves any benchmark score.

Image inputs are native to the model. Full serving and computer-use task
qualification remains incomplete. Retaining vision tensors alone does not
verify image serving or application compatibility. Compatibility checks use
Geer-pinned oMLX 0.5.3, revision
`31d07a7fdbbb499d4520188fb7e8f7bdbfe4c770`; results apply only to the tested
artifact and application path.

The source configuration's 262,144-token context limit is not a qualified
useful-context or latency result. This artifact does not establish support
for a 16 GB Mac or another untested chip and memory configuration.

## License and provenance

The [pinned source card][source-card] declares `apache-2.0` and describes
Qwen3.6-35B-A3B and Agents-A1 as its bases. The pinned Sage snapshot has no
separate LICENSE or NOTICE file. `LICENSE` supplies the standard Apache
License 2.0 text. `LICENSE-QWEN` preserves the Qwen license and attribution.
`NOTICE` records the declared lineage, evidence, and Geer's modifications.
Geer's MIT project license does not replace the model's Apache license.

The evidence below identifies source files independently of local paths.
The full source manifest supplies the remaining hashes.

| Source file | SHA-256 |
| --- | --- |
| README.md | `6018232fad99dd25660741525f2a76d8258dfef6620e930a33e4bbf4a9d0c914` |
| config.json | `93a4693fa9d8392fbfccd4b3c9873f4bfdcb14fdede978b123d07d19675efe99` |
| model.safetensors.index.json | `c49171ab39575c4b9b1accc2d8525aaba82626f2f1374bd0ed5ae86458d1c6ba` |
| chat_template.jinja | `0080393e13df7322bed792a78e75daf8a0374a80928996e5cf3cfd2ab4c1e1be` |
| tokenizer.json | `87a7830d63fcf43bf241c3c5242e96e62dd3fdc29224ca26fed8ea333db72de4` |
| tokenizer_config.json | `d403d02cf04f618863360c5f6d9043683d54544bab6da2d2e365fbcb089f30ce` |

The source describes training lineage; it does not identify immutable parent
weight revisions. The pinned parent evidence in `NOTICE` documents the
license review, not a claim that those parent snapshots reproduce Sage's
training. Geer does not assert rights to upstream trademarks or endorsement
by Shanghai AI Laboratory, the Agents-A1 contributors, or Alibaba Cloud.

[upstream]: https://huggingface.co/InternScience/Sage-A1.5/tree/0e3205cf441cf01df0c0d238cc4738d0e43b4ca7
[source-card]: https://huggingface.co/InternScience/Sage-A1.5/blob/0e3205cf441cf01df0c0d238cc4738d0e43b4ca7/README.md
[build]: https://github.com/ilyakam/geer/blob/develop/tools/model_build.py
