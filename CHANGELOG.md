# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-10-23

### Added

- Experiment history with results, limitations, and conditions for revisiting.
- Forward images to supported models through Pi
- Pinned Sage-A1.5 6-bit and mixed 4/8-bit conversion recipes, model cards,
  attribution evidence, and publication instructions.
- Hardware-aware Sage-A1.5 and Ornith 1.5 choices in the initial terminal and
  native setup wizard, with verified local availability, download and disk
  estimates, and selected upstream benchmark results.

### Changed

- Recommend Sage-A1.5 while preserving eligible confirmed choices, verified
  legacy activation, or a sole verified current compatible cached family.
  Both families retain images and use the existing memory profiles.
- Retain the confirmed model choice on upgrades and reinstalls. Model commands
  support both families; `model add` downloads and verifies without changing
  the active choice. Explicit activation retains histories and rollback.
- Upgrade the pinned Pi harness to official stable 1.4.0.
- Use T3's native Pi driver for Geer, preserving local background generation
  and migrating managed model selections without the Grok version warning.
- Pin fresh T3 installations to stable 0.1.2 and retain compatible Nightly apps.

### Fixed

- Preserve native Pi thinking preferences across setup and status checks.
- Accept ACP stdio MCP servers from current T3 Code alongside HTTP servers.

## [0.3.0] - 2026-10-01

### Added

- ACP adapter for T3 Code.
- Pi harness with automatic installation.
- Automatic discovery of existing skills across harnesses.

### Changed

- Require and automatically install T3 Code.

### Removed

- Claude Code harness.

## [0.2.0] - 2026-08-30

### Changed

- Upgrade from Ornith 1.0 to 1.5
- T3 Code model selector and set favorite to Ornith 1.5

## [0.1.0] - 2026-08-02

### Added

- Native, resumable Geer Setup window with structured consent, progress,
  diagnostics, and the existing terminal workflow retained as a fallback.
- Reproducible mixed 4/8-bit Ornith conversion recipe and publication metadata.
- Automatic 6-bit or mixed 4/8-bit model selection with 64K, 128K, or 256K
  context and BF16 KV cache for 32–128 GB Apple Silicon Macs.
- Custom system prompt about the model, interface, and harness.
- User-installed Claude Code skills available as slash commands in Geer-powered
  T3 Code sessions.
- Reproducible Ornith-to-MLX build and published 6-bit model.
- Guided, resumable setup with size, integrity, and MLX GPU checks.
- Verified active-model reuse without another network download.
- Versioned runtime downloads cached under `~/.geer/cache`.
- Signed macOS package with default-terminal onboarding that reruns explicit
  setup after retained-state reinstalls.
- Prebuilt runtime download with integrity, progress, speed, and ETA.
- Normalized model, server, integration, health, and usage commands.
- Guided uninstall with standard-account authorization and cleanup guidance.
- Loopback oMLX serving through a separately installed Claude Code CLI.
- T3 Code provider with one Geer model and reversible configuration.
- Semble repository retrieval with private indexes and canaries.
- Reliable Semble startup and actionable retrieval setup errors.
- T3 Code nightly detection with separate initialization guidance.
- Dedicated Claude-compatible launcher for T3 Code health and sessions.
- Subscription-free Claude initialization against the local Geer endpoint.
- Per-user endpoints avoid port conflicts and retain launcher diagnostics.
- Runtime upgrades preserve downloaded models and caches.
- Prompt caching, lifecycle controls, and content-free metrics.
- Diagnostics and tests for install, tool use, rollback, and reboot.
- End-to-end onboarding validation in an isolated macOS account.
- Contributor documentation, licensing, and Geer artwork.

[0.4.0]: https://github.com/ilyakam/geer/compare/0.3.0...0.4.0
[0.3.0]: https://github.com/ilyakam/geer/compare/0.2.0...0.3.0
[0.2.0]: https://github.com/ilyakam/geer/compare/0.1.0...0.2.0
[0.1.0]: https://github.com/ilyakam/geer/releases/tag/0.1.0
