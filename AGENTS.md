# Repository Guide

## Current System

Geer runs an agentic coding stack locally on Apple Silicon. T3 Code is the
proven desktop driver, Claude Code is the separately installed agent harness,
and Geer provides local inference, repository retrieval, lifecycle management,
and content-free metrics.

```text
T3 Code
   └── Geer provider and launcher
          └── Claude Code
                 ├── Anthropic Messages -> Geer oMLX -> local model
                 └── MCP -> Semble repository retrieval
```

The implementation is a Python package managed with `uv`. The package code is
under `src/geer/`; `tools/build_release.py` builds the self-contained macOS
installer and native setup application, `tools/build_runtime.py` builds the
versioned oMLX runtime, and `packaging/` contains the entrypoints, installer
scripts, component metadata, and SwiftUI setup source.

## Models and Runtime State

`~/.geer` holds downloaded model snapshots, the active-model symlink, the
pinned runtime, caches, isolated Claude configuration, logs, and metrics. Model
weights remain in the shared Hugging Face cache, and
`~/.geer/models/active` reuses the verified snapshot through a symlink.
Geer keeps Claude authentication and configuration isolated while explicitly
exposing the user's existing `~/.claude/skills` directory through a symlink in
the isolated configuration; it must link to that directory rather than copy or
redistribute user-installed skills. Do not expose skills through a generated
plugin: Claude Code namespaces plugin commands, and T3 Code does not surface
their unnamespaced aliases.
The engine records its authenticated loopback endpoint and launcher diagnostics
under `~/.geer/state`; lifecycle commands must discover that endpoint instead
of assuming a fixed port.

Model weights are not tracked as Git submodules. Runtime installation uses the
revision in `model-distributions/`, while `model-recipes/` and `model-cards/`
preserve the conversion recipe, licenses, notices, and provenance.

## Setup Architecture

The PKG installs machine-wide files and finishes promptly. Its `postinstall`
launches the packaged `Geer Setup.app` in the logged-in console user's launchd
and user context. The app is a presentation layer over the installed CLI: it
runs `/usr/local/bin/geer setup --frontend json-v1` and exchanges versioned
JSON Lines over standard input and output. It must not duplicate model,
runtime, retrieval, T3 configuration, validation, or setup-state logic.

`src/geer/onboarding.py` remains the canonical setup sequence and terminal
renderer. `src/geer/setup_protocol.py` is the graphical transport boundary.
Keep normal terminal output, prompts, defaults, `--yes`, `--skip-t3`,
`--high-performance-download`, and `--plan` behavior compatible when changing
the graphical path. Protocol changes must remain versioned and synchronized
with the Swift client and contract tests.

The native source lives under `packaging/macos/GeerSetup/`. Reuse
`assets/logo.svg` for in-window branding and generate the application icon from
that source during the public build. Keep decisions and footer actions outside
the bounded, scrollable diagnostics viewport so expanding logs cannot make
setup controls inaccessible.

## Working in This Repository

`README.md` describes the current supported behavior and setup flow. The public
commands are:

```sh
uv run geer setup --plan
uv run geer setup
uv run geer doctor
uv run geer stats
```

TL;DR:
- Use HubFlow: `git hf feature start <name>`, `git hf feature finish` when done
- Follow `@CONTRIBUTING.md` for development, package building, validation,
  privacy, and pull request requirements.
- Update `@CHANGELOG.md` for user-facing changes; keep it brief.
- Use unprefixed Semantic Versioning tags such as `0.1.0`; releases merge to
  `master` and back to `develop` through HubFlow.
- Maintainers may keep optional machine-specific instructions in the ignored
  `@ai-refs/AGENTS_PRIVATE.md`. Never assume that file exists, require it for a
  contribution, or copy its contents into tracked files or public output.

## Installation Validation

Use `@CONTRIBUTING.md` as the canonical public validation workflow. Contributors
must be able to build an unsigned development package and exercise onboarding
without maintainer certificates, Keychain profiles, private accounts, ignored
scripts, or machine-specific fixtures. Signing, notarization, and final
Gatekeeper acceptance are release concerns rather than contribution
prerequisites.

Use the tests under `tests/` for focused validation and verify the packaged,
frozen artifact whenever behavior differs from source execution. Lifecycle
tests should use a disposable macOS account or test machine when practical,
preserve rollback, scope process checks to the selected user, and never delete
retained model state unless cleanup is the behavior under test.

The setup application must remain non-relocatable in
`packaging/components.plist`; otherwise Installer may move it to a discovered
development copy and silently omit it from the intended payload path. Package
verification must check the bundle identifier, arm64 executable, icon,
installation path, and disabled relocation metadata.

Keep `packaging/geer-setup.command` unchanged as the functional fallback. The
package `postinstall` must try the setup app first by absolute path, as the
console user, then open the explicit `geer setup` `.command` through Launch
Services if the app is absent or its launch request fails. Do not name
Terminal.app, remove the `.command` fallback, or make retained setup state turn
onboarding into a status-only run. Always preserve manual `geer setup` as the
recovery surface.

Setup cancellation, retry, reinstall, upgrade, and ordinary uninstall must
preserve `~/.geer`, verified model snapshots, reusable caches, and the
previous active model unless the user explicitly requests complete cleanup.
Never move model acquisition or other long user-scoped work into privileged
installer scripts.

Keep public behavior claims tied to evidence from the current implementation.
Preserve the third-party licenses and notices alongside Geer's MIT-licensed
code and documentation.
