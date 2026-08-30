# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[Unreleased]: https://github.com/ilyakam/geer/compare/0.2.0...HEAD
[0.2.0]: https://github.com/ilyakam/geer/compare/0.1.0...0.2.0
[0.1.0]: https://github.com/ilyakam/geer/releases/tag/0.1.0
