# Qwen3.5-9B (Q4_K_M GGUF + F16 vision)

Geer installs the existing third-party files from
[Unsloth's pinned repository](https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/tree/3885219b6810b007914f3a7950a8d1b469d598a5).
Qwen created the original model; Unsloth produced this GGUF conversion.
Geer performs no conversion, quantization, distillation, or training.

The [distribution manifest](../../model-distributions/qwen3.5-9b-q4-k-m.toml)
records immutable file sizes and SHA-256 hashes. It includes both the Q4_K_M
language model and the complete F16 vision projector. The model's embedded
tokenizer and chat template remain unchanged.

The [pinned upstream card](https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/blob/3885219b6810b007914f3a7950a8d1b469d598a5/README.md)
declares Apache-2.0 and identifies Qwen/Qwen3.5-9B as its base. The exact
conversion parent revision and quantizer recipe have not been independently
reconstructed. `LICENSE` preserves the [Qwen license file](https://huggingface.co/Qwen/Qwen3.5-9B/blob/c202236235762e1c871ad0ccb60c8ee5ba337b9a/LICENSE)
at revision `c202236235762e1c871ad0ccb60c8ee5ba337b9a`, upstream SHA-256
`bbedc3fda3305820b977265f01b8619d87570a6739de3a5582c3464840f1e57a`.
Only line endings are normalized to LF. That evidence pin does not assert
conversion ancestry. Geer's MIT license
does not replace the model license. Geer claims no endorsement from Qwen or
Unsloth.

This installation path is experimental. Configured context caps, source
architecture limits, and retention of vision are not claims of benchmark
quality or physical qualification on every eligible Mac. See the
[runtime notes](../../docs/agents-qwen-runtime.md) for context estimates and
validation requirements.
