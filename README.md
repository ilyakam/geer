<hr />

<img align="left" alt="Geer" src="assets/logo.svg" width="42">
<h1>Geer</h1>
<span clear="both"/>

Free, private, and open source coding agent that challenges the frontier.

## At a glance

Geer = [Sage-A1.5 or Ornith 1.5](https://huggingface.co/ilyakam/models) (via [oMLX](https://github.com/jundot/omlx)) + [Pi](https://pi.dev) + [T3 Code](https://t3.codes/)

## Minimum system requirements

- Apple Silicon (M-series chip)
- macOS 15 or newer
- 32 GiB of unified memory
- About 35 GB of free disk space for mixed 4/8-bit models, or 42 GB for 6-bit models

Fresh-install disk estimates include dependencies and temporary space. Allow
more disk space for inference caches or to retain both model families. Setup
shows the remaining download, additional disk space needed, and total model
size separately.

## Installation

Download and launch `Geer-<version>-macOS-arm64.pkg` from the latest [GitHub release](https://github.com/ilyakam/geer/releases). Setup installs Pi and T3 Code automatically and reuses compatible installations.

To uninstall, run `geer uninstall` and optionally delete `~/.geer`.

## Model choice

Choose a family once in the initial setup wizard. Sage-A1.5 is recommended,
and Ornith 1.5 remains a maintained alternative. Geer saves the choice after
activation succeeds and retains it on upgrades and reinstalls.

An explicit choice takes priority. Otherwise setup preserves a confirmed,
eligible choice, then a verified compatible legacy active model. If neither
exists, setup reuses the sole verified current compatible cached family.
It selects Sage when both or neither are available.

Preview setup or choose a family explicitly:

```sh
geer setup --model sage --plan
geer setup --model sage
```

Download and verify the other family later without changing your active choice:

```sh
geer model list
geer model add ornith --plan
geer model add ornith
```

Either family works in these commands. `geer model install` and
`geer model setup` are aliases for the download-only `geer model add`. Remove
an inactive family with `geer model remove ornith`. Use
`--high-performance-download` on setup or model add for the higher-resource
transfer mode on a Mac with at least 64 GiB of unified memory.

Geer chooses the variant and context cap for either family from unified memory.
Both families retain image inputs in every profile.

| Unified memory | Variant | Configured context cap |
| --- | --- | --- |
| 32 GiB to less than 48 GiB | Mixed 4/8-bit | 64K |
| 48 GiB to less than 64 GiB | Mixed 4/8-bit | 128K |
| 64 GiB or more | 6-bit | 256K |

K means 1,024 tokens. These configured limits do not establish measured useful
context or physical qualification on every Mac.

Geer keeps one globally active model and retains both downloads and histories.
For a deliberate maintenance change, use `geer model use ornith`. Finish the
current turn and close T3 first, then reopen it afterward. Setup requests
consent before closing an open T3 app when activation requires it. Activation
retains the previous model for rollback; selecting the same verified active
artifact makes no change.

### Selected upstream results

These percentage scores come from [Sage's upstream comparison, Table 1](https://arxiv.org/html/2610.28578v1#S3.T1).
They are not scores measured on Geer's quantized models.

| Benchmark | Ornith 1.5 | Sage-A1.5 |
| --- | ---: | ---: |
| SWE-bench Pro, repository issue resolution | 59.6 | 61.2 |
| IFBench, strict prompt accuracy for instruction following | 44.6 | 81.6 |
| PinchBench-v1, general agent tasks | 88.6 | 90.0 |

IFBench is not a coding benchmark. SWE-bench Pro uses different harnesses and
context settings for the two models. The full upstream table also includes
DeepResearchBench II, where Ornith scores 48.6 and Sage scores 43.7 without
orchestration. See the [methodology and evaluation policy](docs/agents-model-evaluation.md#chooser-comparison)
for the comparison's limits.

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
