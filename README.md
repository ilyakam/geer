<hr />

<img align="left" alt="Geer" src="assets/logo.svg" width="42">
<h1>Geer</h1>
<span clear="both"/>

Free, private, and open source coding agent that challenges the frontier.

## At a Glance

Geer = [Ornith 1.5](https://huggingface.co/ilyakam/models) (via [oMLX](https://github.com/jundot/omlx)) + [Pi](https://pi.dev) + [T3 Code](https://t3.codes/) (via Grok ACP)

## Minimum System Requirements

- Apple Silicon (M-series chip)
- macOS 13
- 32 GB of unified memory
- 35 GB of disk space

## Installation

Download and launch `Geer-<version>-macOS-arm64.pkg` from the latest [GitHub release](https://github.com/ilyakam/geer/releases). Setup installs Pi and T3 Code automatically and reuses compatible installations.

To uninstall, run `geer uninstall` and optionally delete `~/.geer`.

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
