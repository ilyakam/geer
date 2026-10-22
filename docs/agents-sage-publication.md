# Sage-A1.5 conversion and publication

This runbook covers two independent Sage-A1.5 conversions under issue [#9][issue].
Both artifacts have been built and passed full output verification. The recipes
name their Hugging Face destinations but omit distribution revisions and
installation entries. Keep the current model defaults, selectors, active link,
and live runtime settings unchanged.

Read the experiment ledger and the accessible private Sage records before
resuming. Keep raw evidence and unreviewed experiment findings private under
the [ledger's publication rules](experiments/README.md#publication-rules).

## Coordination gates

The root coordinator owns candidate changes, inference, and upload. It assigns
the hardware lane before conversion or inference and uploads each artifact
when its authorized acceptance checks pass. Run the stages separately, once
per artifact, and retain its hashes, notices, and scoped acceptance evidence.

Use upstream benchmark reports as labeled quality proxies in the accepted
50% quality, 30% useful-context, 20% speed policy. Full benchmark reproduction
is outside this task. A conversion does not establish benchmark preservation.
Root owns the bounded inference checks and the publication decision for each
quantization. A 6-bit result cannot qualify the mixed artifact.

Limit the current qualification target to a physical M4 Max with 128 GiB.
Treat 32K useful context and 64K capacity as separate acceptance claims.
Native source context metadata does not qualify 128K or 262K. Root must review
artifact-specific T3/Pi image evidence and the pending full computer-use task;
tensor retention alone does not prove image serving or computer-use behavior.
Record completed results for each exact artifact before upload. Keep pending
checks marked incomplete and publish technical results only after maintainer
review.

## License evidence

The pinned Sage card and the inspected Agents-A1 and Qwen3.6 parent evidence
declare Apache-2.0. The model cards and NOTICE files retain immutable evidence
links and hashes. Sage and Agents-A1 have no standalone license or NOTICE in
their inspected snapshots. The publication assets supply the standard Apache text
and preserve Qwen's actual license and copyright statement. A missing
standalone file does not itself establish an additional restriction.

Apache-2.0 section 4 requires a license copy, prominent modification notices,
retention of relevant source notices, and preservation of applicable upstream
NOTICE attributions when present. Review the supplied LICENSE, LICENSE-QWEN,
NOTICE, and modification descriptions against the exact output before upload.
The source cards do not pin parent training revisions; the parent evidence
revisions document the license review rather than reconstructing training.

## Inspect without loading a model

Run from the repository root. These commands use only the Python standard
library and neither download weights nor start inference.

```sh
python3 tools/model_build.py --recipe model-recipes/sage-a1.5-35b-6bit.toml plan
python3 tools/model_build.py --recipe model-recipes/sage-a1.5-35b-4-8bit.toml plan
```

Both recipes pin the same BF16 source, expert transformation, and conversion
versions. The mixed recipe reuses the reviewed routed-expert 4-bit/protected
text 8-bit policy. The vision tower stays BF16 and the pinned MLX-VLM sanitizer
omits MTP. Neither recipe enables speculative decoding.

## Build or reuse, after root assigns the hardware lane

Choose one artifact. Repeat this stage independently for the other one.

```sh
quant=6bit
recipe="model-recipes/sage-a1.5-35b-$quant.toml"
repo_id="ilyakam/Geer-Sage-A1.5-35B-A3B-$quant-MLX"
candidate="${GEER_HOME:-$HOME/.geer}/models/candidates/sage-a1.5-35b-$quant"
```

### Promote an existing candidate's display label

Both existing candidates have a `candidate` display label in their original
build metadata. The checked-in recipes now use their final release labels.
This is a metadata-only promotion of the existing conversions. Root performs
it separately for each artifact before `prepare-publication`.

1. Run full `verify` with the original build recipe and its original display
   label. Archive that recipe, the build manifest, and all existing publication
   metadata privately outside the candidate directory. Retain the complete
   file list, byte counts, SHA-256 records, and verification result.
2. Compare the original and checked-in recipes. Require identical source,
   conversion, quantization, and output settings. The release label and
   reviewed publication destination/metadata paths are the intended changes.
3. Change only the top-level `display_name` in `geer-build-manifest.json` to
   the checked-in recipe's label. Compare the parsed manifest with the archive
   and require every other field to be identical, including all source and
   conversion metadata, file records, hashes, and byte counts. Leave every
   other candidate file unchanged. Do not refresh hashes to accept a mismatch.
4. Run full `verify` with the checked-in recipe. Require every file's size
   and SHA-256 to match the archived records. This proves the label changed
   without reconverting or changing weights or other output files.

The earlier `local-evaluation` destination and absolute metadata paths are
local publication settings, not published provenance. After label verification,
`prepare-publication` replaces the old card, licenses, NOTICE, and copied
recipe with the reviewed assets. It refreshes publication metadata hashes and
the aggregate byte count. Preserve the original metadata archive as the record
of the conversion before promotion.

### Build a missing candidate

For an authorized build when the candidate is absent, run:

```sh
uv run --script tools/model_build.py --recipe "$recipe" download
uv run --script tools/model_build.py --recipe "$recipe" convert
```

`download` reuses the immutable shared cache and creates a source manifest
for that recipe. It hashes the complete source even when cached. Do not copy
the 6-bit source manifest to the mixed state path, because its recipe identity
would be wrong. `convert` refuses an existing output; preserve an existing
candidate and choose a distinct `--output-dir` for an authorized rebuild.

### Prepare publication metadata

After verifying the existing candidate's label promotion or a new build, run:

```sh
uv run --script tools/model_build.py --recipe "$recipe" verify
uv run --script tools/model_build.py --recipe "$recipe" prepare-publication
uv run --script tools/model_build.py --recipe "$recipe" publish-plan
```

Review the complete source/build manifests and copied metadata. Require the
expected quantization policy, all native visual tensors, and no MTP tensors.
For either reused build, compare against the archived original. All weight
hashes and non-publication file records must remain identical. Source manifest
file records and all build-manifest fields other than `display_name`, `files`,
and aggregate `bytes` must also remain identical. Only the reviewed publication
files may have new hashes or sizes. Final publication sizes and hashes come
from the refreshed manifest, not from an earlier local candidate.
Upload only when root has recorded the artifact-specific acceptance result.
No activation or model installation is part of this runbook.

## Private upload, after root greenlights it

```sh
uv run --script tools/model_build.py --recipe "$recipe" publish --confirm-repo-id "$repo_id"
```

The tool requires a write token for the destination namespace, creates or
reuses a private model repository, and refuses an already-public destination.
Save its returned immutable `revision`. The recipes deliberately omit this
value until an actual artifact exists.

To verify that revision without changing a checked-in recipe, create a
private scratch copy. Set `uploaded_revision` to the exact SHA returned by
`publish`, then run:

```sh
publication_dir="${GEER_HOME:-$HOME/.geer}/state/sage-publication/$quant"
mkdir -p "$publication_dir"
chmod 700 "$publication_dir"
pinned_recipe="$publication_dir/recipe.toml"
python3 - "$recipe" "$uploaded_revision" "$pinned_recipe" <<'PY'
import re
import sys
from pathlib import Path

source, revision, target = sys.argv[1:]
if not re.fullmatch(r"[0-9a-f]{40}", revision):
    raise SystemExit("use the exact immutable revision returned by publish")
path = Path(target)
if path.exists():
    raise SystemExit("preserve the existing pinned recipe before another upload")
with path.open("x", encoding="utf-8") as stream:
    stream.write(Path(source).read_text() + f'\nrevision = "{revision}"\n')
path.chmod(0o600)
PY
uv run --script tools/model_build.py --recipe "$pinned_recipe" --output-dir "$candidate" verify-published
```

The scratch recipe is only for remote verification and release. Its relative
template paths resolve from the scratch directory, so use the original
checked-in recipe for metadata preparation. Verification compares the full
remote file list, byte counts, and SHA-256 values with the local manifest,
including downloaded hashes for files without remote LFS metadata.

## Tag, make public, and verify

After private verification passes, release with the same pinned scratch
recipe and the exact destination confirmation:

```sh
uv run --script tools/model_build.py --recipe "$pinned_recipe" --output-dir "$candidate" release-published --confirm-repo-id "$repo_id"
uv run --script tools/model_build.py --recipe "$pinned_recipe" --output-dir "$candidate" verify-published
```

`release-published` rechecks the private snapshot, tags its immutable revision,
makes the repository public, then verifies it again with public visibility
required. Stop on any mismatch. Retain the revision, tag, verification output,
and final manifest hash. A partial release failure requires inspecting the
actual remote state before retrying.

Only after public verification should a follow-up replace the unpinned inline
distribution tables with reviewed `model-distributions/` manifests containing
the real revision and final bytes. Default/model-selector changes require
their own reviewed integration change and qualified host scope.

[issue]: https://github.com/ilyakam/geer/issues/9
