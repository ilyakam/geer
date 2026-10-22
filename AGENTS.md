# Agent guide

Follow [CONTRIBUTING.md](CONTRIBUTING.md) for development, HubFlow, validation,
privacy, and licensing. Keep [README.md](README.md) for user instructions and
this file for implementation guidance.

## Code map

Geer connects T3 Code's `grok` ACP driver to Pi RPC. Pi uses the local oMLX
engine for inference and Semble over MCP for repository retrieval.

- `src/geer/` is the Python package, managed with `uv`.
- `src/geer/onboarding.py` owns setup and the terminal interface.
  `src/geer/setup_protocol.py` defines the versioned JSON Lines protocol for
  the SwiftUI client in `packaging/macos/GeerSetup/`.
- `tools/build_release.py` builds the frozen CLI and macOS installer;
  `tools/build_runtime.py` builds the pinned oMLX runtime.
- `model-distributions/` pins installation revisions. `model-recipes/` and
  `model-cards/` record conversion provenance and licenses.

## Model selection goal

Choose the best practical local model and variant for each Mac's chip and
unified memory. The target covers all M-series configurations with at least
16 GB, including M1 through M6 and configurations up to 512 GB. Exclude 8 GB
machines for now. This is a qualification goal; current support still follows
the implementation and measured evidence.

Budget memory for macOS, T3, Pi, retrieval, inference, and a useful context
window for sustained agentic, computer-use, and coding tasks. Require at least
32K total context, including tools, history, images, and generation reserve.
Loading weights alone does not establish suitability. Preserve image inputs
in every profile of an image-capable model and verify them through Pi/T3;
stripping vision to fit a smaller Mac does not meet the model-selection goal.

For model comparison and qualification, follow
[the evaluation policy](docs/agents-model-evaluation.md). After capability and
memory checks pass, use 50% reported benchmark quality, 30% verified useful
context, and 20% measured speed as a provisional ranking. Keep reported
quality, estimates, and physical-host measurements distinct.

Use oMLX and Ornith as the current comparison baseline. Prefer another model
or inference server when measured results establish a better fit and the
compatibility checks in CONTRIBUTING.md prove the migration and recovery path.
Keep model and runtime selection specific to the hardware configuration.

## Experiment history

Before model, quantization, runtime, or benchmark work, search
[the experiment ledger](docs/experiments/README.md) and matching dated records.
Consult `geer-private/experiments/` when accessible; flag incomplete history
when it is unavailable. Compare exact artifacts and previous blockers, and
explain what changed before repeating a trial. Record outcomes and concrete
revisit conditions. Keep working research private and publish summaries only
after explicit maintainer review, following the ledger's publication rules.

## Guardrails

- Preserve `~/.geer`, caches, and the active model across cancellation, retry,
  reinstall, upgrades, and ordinary uninstall unless complete cleanup is
  requested. Keep `~/.geer/models/active` linked to a verified snapshot;
  change models through the reviewed build and activation workflow.
- Discover the authenticated loopback endpoint under `~/.geer/state`.
  Keep metadata probes independent of inference startup.
  Keep protocol stdout as JSON Lines and diagnostics on stderr.
- Advertise the active model's real ID and name through initialize
  `_meta.modelState`. Require Full access before interactive tools run;
  keep T3 title/commit-text sessions tool-free.
- Keep client MCP credentials in private session directories and strip ambient
  cloud credentials from Pi. Use Pi JSONL history for resumption and complete
  turns on `agent_settled` so queued work and compaction finish before replying.
- Keep Geer's own skills in `~/.geer/skills`. Borrow existing user skill
  directories through Pi's `--skill` paths without copying files or changing
  other harnesses. Users expect installed skills to be discovered; native Pi
  references make their sources easy to trace without another setup prompt.
  Preserve Pi's invocation rules and normal project directory migrations.
- Download and verify Pi and T3 Code from the pins in `packaging/`; keep them
  synchronized with their distribution modules and preserve upstream notices.
- Require a compatible T3 app and verified Geer provider before setup is ready.
  Seed Geer defaults only for new profiles; preserve existing preferences.
  Never let `--yes` silently quit T3 to change settings.
- Keep setup logic in the CLI. Synchronize protocol changes with the Swift
  client and contract tests, and preserve terminal setup behavior.
- Keep privileged installer scripts short and launch setup as the console
  user. Downloads and other long operations belong in user-scoped setup.
  Retain the legacy launcher through installation; retire its provider only
  in the backed-up transaction that configures Pi after inference validation.
- Keep the setup app non-relocatable in `packaging/components.plist`.
  Retain `packaging/geer-setup.command` as the Launch Services fallback and
  manual `geer setup` as recovery.
- Verify the frozen package when behavior differs from source execution.
  Test integration changes in T3 Code with a real repository read/edit/test
  turn; use ignored synthetic fixtures and check the resulting files.
- Record user-facing changes briefly under `[Unreleased]` in `CHANGELOG.md`.
