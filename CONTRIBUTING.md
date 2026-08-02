# Contributing to Geer

Thank you for helping make private, local-first agentic coding accessible to
more developers.

Geer is an early proof of concept. Focused bug reports, compatibility results,
benchmarks, documentation, tests, and implementation contributions are
welcome. Please read the [Code of Conduct](CODE_OF_CONDUCT.md) before
participating.

## Before You Start

Open an issue before investing substantial work in:

- a new harness, desktop application, or driver adapter;
- a new model or Apple Silicon memory profile;
- inference runtime, cache-format, or persistent-service changes;
- model conversion or publication;
- changes to privacy, metrics, or network behavior; or
- broad architectural refactoring.

Early discussion helps establish scope, provenance, compatibility requirements,
and the hardware needed to verify the result. Small documentation corrections,
tests, and clearly bounded bug fixes do not require prior approval.

## Development Setup

Geer's current proof of concept requires an Apple Silicon Mac running macOS 13
or newer, Python 3.11 or newer, the Xcode command-line tools, and
[`uv`](https://docs.astral.sh/uv/). Install Apple's tools with
`xcode-select --install` if `xcrun` or `pkgbuild` is unavailable.

```sh
uv sync
uv run ruff check .
uv run pytest
```

### macOS Installer

Build an unsigned, self-contained Apple Silicon development installer with:

```sh
uv run python tools/build_release.py
```

The package is written to `dist/Geer-<version>-macOS-arm64.pkg`. It contains the
frozen Geer CLI, Semble, the native Geer Setup application, and the retained
terminal fallback. It exercises the same installation and guided onboarding
path as a release package. An unsigned local build does not require
an Apple Developer certificate, Keychain profile, or access to maintainer
infrastructure.

Install it on a disposable macOS account or test Mac when practical:

```sh
open dist/Geer-<version>-macOS-arm64.pkg
```

The local package can validate payload installation, first-run setup, retained
model reuse, T3 Code configuration, and uninstall behavior. Because it is
unsigned and not notarized, it cannot validate the release trust chain or
Gatekeeper behavior seen by users who download a public artifact. Signing,
notarization, stapling, and final Gatekeeper validation are performed only by
the release maintainer or future release automation.

After onboarding, run:

```sh
geer status
geer doctor
```

For an upgrade/reinstall test, quit T3 Code first. `geer uninstall` removes the
machine-wide package and T3 provider but deliberately retains `~/.geer` so the
next setup can verify and reuse downloaded model state. Do not run the optional
`rm -rf ~/.geer` cleanup command when testing retained-state behavior.

Run `tools/build_runtime.py` only when changing the pinned oMLX runtime archive.
That workflow uses an independently managed Python 3.13.7 runtime; contributors
may continue using any supported Python 3.11+ interpreter for source
development. Record the resulting archive's exact byte count and SHA-256 in
`src/geer/runtime.py` before building a package that references it. Build it
twice from clean temporary directories and require identical SHA-256 digests
before publication.

The reviewed model is exposed through a read-only link at
`~/.geer/models/active`. Never commit model weights or modify a pinned snapshot
in the shared Hugging Face cache. Model publication must use the reviewed,
hash-verified distribution workflow.

Useful local checks include:

```sh
uv run geer assets
uv run geer runtime
uv run geer retrieval-canary
uv run geer doctor
uv run geer stats
```

Some commands start a large local model and may require substantial memory.
State the exact checks you could run and do not present an unrun hardware test
as passing.

### Model Builds

Model sources and generated weights live outside the Git checkout. The
recipe-driven build tool downloads immutable upstream snapshots into the
shared Hugging Face cache under `~/.geer/cache/huggingface` and writes
inactive candidates under `~/.geer/models/candidates`.

Most development only needs the published conversion:

```sh
uv run --script tools/model_build.py install-plan
uv run --script tools/model_build.py install
```

The plan is non-mutating. Installation downloads an immutable Hugging Face
revision, verifies it against the included build manifest, preserves the
previous activation target, and activates the verified cache snapshot without
copying model bytes.

Preview a recipe without downloading anything:

```sh
uv run --script tools/model_build.py plan
```

Download, convert, and verify the default model:

```sh
uv run --script tools/model_build.py download
uv run --script tools/model_build.py convert
uv run --script tools/model_build.py verify
```

On a machine with at least 64 GB of unified memory, the download can use
Hugging Face Xet's higher-resource transfer mode:

```sh
uv run --script tools/model_build.py --high-performance-transfer download
```

Downloads and conversions are never activated automatically. Review the
generated provenance and hash manifests before changing
`~/.geer/models/active`. New profiles should add a reviewed recipe under
`model-recipes/` and tests for any required tensor-layout transformation.

Maintainers prepare and preview a converted candidate for publication with:

```sh
uv run --script tools/model_build.py prepare-publication
uv run --script tools/model_build.py publish-plan
```

Remote creation, upload, release, and distribution revision updates are separate
maintainer actions. Publication starts private, verifies remote file sizes and
hashes against the local build manifest, tags the immutable revision, and only
then changes repository visibility.

## Git Workflow

Geer uses HubFlow:

- production: `master`;
- integration: `develop`;
- feature branches: `feature/<name>`;
- release branches: `release/<version>`;
- hotfix branches: `hotfix/<name>`; and
- support branches: `support/<name>`.

Create work from `develop`, keep each pull request focused, and target
`develop`. HubFlow branch structure is required; the `git hf` extension is
optional. If it is installed, start a feature with:

```sh
git hf feature start <name>
```

Use Conventional Commits with a scope, such as:

```text
feat(runtime): add model health probe
docs(readme): document local verification
```

Do not include unrelated formatting or generated changes in the same pull
request. Releases use unprefixed SemVer tags such as `0.1.0` and follow
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Release branches
merge to `master` for production and back to `develop` for continued work.

## Pull Request Requirements

Before requesting review:

```sh
uv run ruff check .
uv run pytest
git diff --check
git status --short --branch
```

A contribution should also:

- add meaningful tests for changed behavior;
- update user-facing documentation and `CHANGELOG.md` when appropriate;
- preserve unrelated user configuration and provide rollback for persistent
  changes;
- keep development services on loopback unless the change explicitly and
  safely addresses remote access;
- document limitations and failure behavior;
- avoid false claims about untested harness, runtime, model, or macOS versions;
  and
- keep commits reviewable and free of credentials or private artifacts.

## Compatibility Evidence

A new harness or application surface is not supported merely because it
accepts a compatible API URL. Include:

- exact Geer, macOS, hardware, harness, runtime, and model revisions;
- configuration preview, application, verification, and rollback results;
- text, streaming, cancellation, and malformed-request behavior;
- at least one real repository read and one edit/test turn;
- tool-call and tool-result continuation behavior;
- model identity, context-limit, and token-accounting results;
- Semble retrieval behavior when supported; and
- restart and failure-recovery results appropriate to the surface.

For performance results, include model and quantization, Apple Silicon chip,
unified memory, prompt and completion sizes, cache state, time to first token,
total latency, throughput, and peak memory pressure. Compare equivalent
outputs; a shorter response is not evidence of faster decoding.

## Privacy and Security

Never commit, upload, or include in an issue or pull request:

- prompts, completions, or private repository contents;
- credentials, tokens, OAuth files, or license data;
- raw Claude Code or other harness request dumps;
- private prompt, KV, retrieval, or conversation caches;
- files from `.geer/` or `ai-refs/`;
- local model caches, candidates, or weights outside the reviewed publication
  workflow;
- proprietary prompts, tools, skills, workflows, hooks, plugins, or binaries.

Use synthetic fixtures and redacted logs. Diagnostic payload capture must be
explicit, time-bounded, owner-only, and excluded from Git.

Do not report a security vulnerability in a public issue. Follow
[SECURITY.md](SECURITY.md) for the private reporting process.

## Models and Third-Party Work

Geer's MIT license covers only Geer's original work. Preserve the licenses,
notices, authorship, and terms of models, runtimes, harnesses, datasets, and
other dependencies.

Model or conversion contributions must identify:

- upstream repository and immutable revision;
- complete license and model-lineage evidence;
- tokenizer, chat template, architecture, and source hashes;
- conversion and quantization tools and exact commands;
- output hashes and expected runtime; and
- reproducible quality, latency, and memory results.

Do not use product-specific private artifacts as distributable dependencies.

## Review

Maintainers may ask for changes to scope, tests, documentation, provenance,
privacy, or compatibility evidence. Review feedback is about protecting a
coherent and trustworthy project; ask questions when the requested outcome is
unclear.

By contributing, you agree that your contribution is licensed under the
project's [MIT License](LICENSE).
