<hr />

<img align="left" alt="Geer" src="assets/logo.svg" width="42">
<h1>Geer</h1>
<span clear="both"/>

Free, private, and open source coding agent that challenges the frontier.

## At a glance

Geer = local model + [Pi](https://pi.dev) + [T3 Code](https://t3.codes/)

Experimental Qwen3.5-9B uses official [llama.cpp b11901](https://github.com/ggml-org/llama.cpp/releases/tag/b11901).
[Sage-A1.5 and Ornith 1.5](https://huggingface.co/ilyakam/models) use
[oMLX](https://github.com/jundot/omlx). All three connect through T3 Code's native
Pi driver.

## Minimum system requirements

- Any Apple Silicon M-series chip
- macOS 15 or newer
- 16 GiB of unified memory for experimental Qwen; 32 GiB for Sage or Ornith
- About 10 GB of free disk space for Qwen, 35 GB for mixed 4/8-bit MLX models,
  or 42 GB for 6-bit MLX models

Memory sizes use GiB, meaning 1,024³ bytes. Apple labels the corresponding Mac
memory configurations in GB.

Fresh-install disk estimates include dependencies and temporary space. Allow
more disk space for inference caches or to retain multiple model families. Setup
shows the remaining download, additional disk space needed, and total model
size separately.

## Installation

Download and launch `Geer-<version>-macOS-arm64.pkg` from the latest [GitHub release](https://github.com/ilyakam/geer/releases). Setup installs Pi and T3 Code automatically and reuses compatible installations.

To uninstall, run `geer uninstall` and optionally delete `~/.geer`.

## Model choice

Choose a family in the initial setup wizard. On a fresh installation, setup
preselects experimental Qwen3.5-9B for Macs with 16 GiB to less than 32 GiB of
unified memory. At 32 GiB or more, Sage-A1.5 is recommended, Ornith 1.5 remains
a maintained alternative, and Qwen is optional. Geer saves the choice after
activation succeeds and retains it on upgrades and reinstalls.

An explicit choice takes priority. Otherwise setup preserves a confirmed,
eligible choice, then a verified compatible active model. If neither
exists, setup reuses the sole verified current compatible cached family.
With no unique cached choice, it preselects the recommendation for this Mac.

Preview setup or choose a family explicitly:

```sh
geer setup --model sage --plan
geer setup --model qwen --plan
geer setup --model sage
```

Download and verify another family later without changing your active choice:

```sh
geer model list
geer model add ornith --plan
geer model add ornith
```

All three families, `sage`, `ornith`, and `qwen`, work in these commands.
`geer model install` and
`geer model setup` are aliases for the download-only `geer model add`. Remove
an inactive family with `geer model remove ornith`. Use
`--high-performance-download` on setup or model add for the higher-resource
transfer mode on a Mac with at least 64 GiB of unified memory.

Geer keeps one globally active model and retains model downloads and histories.
For a deliberate maintenance change, use `geer model use ornith`. Finish the
current turn and close T3 first, then reopen it afterward. Setup requests
consent before closing an open T3 app when activation requires it. Activation
retains the previous model for rollback; selecting the same verified active
artifact makes no change.

Geer chooses the variant and context cap from unified memory. All three families
retain image inputs in every profile. K means 1,024 tokens.

### Experimental Qwen3.5-9B

Geer downloads Unsloth's existing Q4_K_M GGUF and full F16 vision projector from
[this pinned upstream revision](https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/tree/3885219b6810b007914f3a7950a8d1b469d598a5).
Qwen created the original model, and Unsloth produced the GGUF conversion.
Geer does not train, distill, or convert this model.

Qwen remains experimental at every memory size.

| Unified memory | Variant | Configured total context cap |
| --- | --- | --- |
| 16 GiB to less than 24 GiB | Q4_K_M GGUF + F16 vision | 64K |
| 24 GiB or more | Q4_K_M GGUF + F16 vision | 128K, estimated |

These total windows include tools, images, history, and a 4,096-token generation
reserve. The 128K cap comes from a hybrid-cache memory estimate and has not been
physically qualified on a 24 GiB Mac. Useful context and latency still need
qualification for each hardware configuration. See the
[Qwen runtime and context notes](docs/agents-qwen-runtime.md) for the budget
formula and sources.

### Sage-A1.5 and Ornith 1.5

Both families require at least 32 GiB of unified memory and use the existing
MLX variants.

| Unified memory | Variant | Configured context cap |
| --- | --- | --- |
| 32 GiB to less than 48 GiB | Mixed 4/8-bit | 64K |
| 48 GiB to less than 64 GiB | Mixed 4/8-bit | 128K |
| 64 GiB or more | 6-bit | 256K |

These configured limits do not establish measured useful context or physical
qualification on every Mac.

### Published upstream scores

| Task and benchmark | Sage-A1.5 | Ornith 1.5 | Qwen3.5-9B |
| --- | ---: | ---: | ---: |
| Following instructions, IFBench | 81.6% | 44.6% | 64.5% |
| Reasoning across long documents, AA-LCR | 81.0% | 73.2% | 63.0% |
| Multi-step tasks with tools, VitaBench | 45.4% | 44.6% | 29.8% |

<details>
<summary>Sources and evaluation settings</summary>

- Sage-A1.5 & Ornith 1.5: Please see [Sage's upstream comparison, Table 1](https://arxiv.org/html/2610.28578v1#S3.T1)
- Qwen3.5-9B: Please see the language benchmark table in its [pinned model card](https://huggingface.co/Qwen/Qwen3.5-9B/blob/c202236235762e1c871ad0ccb60c8ee5ba337b9a/README.md#benchmark-results)

</details>

## Troubleshoot

- See all usage options: `geer --help`
- Something's not right: `geer doctor`

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for development,
testing, privacy, provenance, and compatibility requirements. You can build and
test an unsigned installer without an Apple Developer account. Participation is
governed by the [Code of Conduct](CODE_OF_CONDUCT.md), and security reports follow
[SECURITY.md](SECURITY.md).

## License

Geer's original code and documentation are available under the
[MIT License](LICENSE). Models, runtimes, harnesses, and other
third-party components retain their own licenses and terms.

Geer is an independent project and is not affiliated with or endorsed by
Pi's maintainers, T3 Tools, or Deep Reinforce AI.
