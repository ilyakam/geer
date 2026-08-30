#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#   "hf-xet==1.5.2",
#   "huggingface-hub==1.24.0",
#   "mlx==0.32.0",
#   "mlx-lm==0.31.3",
#   "mlx-vlm==0.6.3",
#   "safetensors==0.8.0",
# ]
# ///
"""Download, convert, and verify reproducible Geer model builds."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BUILD_MANIFEST = "geer-build-manifest.json"
DEFAULT_RECIPE = Path(__file__).resolve().parents[1] / "model-recipes" / "ornith-1.5-35b-6bit.toml"
SAFETY_MARGIN_BYTES = 10 * 1024**3
MAX_SAFETENSORS_HEADER_BYTES = 100 * 1024**2
ROUTED_EXPERTS_4BIT_PROTECTED_TEXT_8BIT = "routed_experts_4bit_protected_text_8bit"


class BuildError(RuntimeError):
    """Raised when a model build cannot proceed safely."""


@dataclass(frozen=True)
class Recipe:
    path: Path
    data: dict[str, Any]

    @property
    def identifier(self) -> str:
        return str(self.data["id"])

    @property
    def source(self) -> dict[str, Any]:
        return self.data["source"]

    @property
    def conversion(self) -> dict[str, Any]:
        return self.data["conversion"]

    @property
    def quantization(self) -> dict[str, Any]:
        return self.data["quantization"]

    @property
    def output(self) -> dict[str, Any]:
        return self.data["output"]

    @property
    def distribution(self) -> dict[str, Any]:
        inline = self.data.get("distribution")
        if isinstance(inline, dict):
            return inline
        path = self.path.parent.parent / "model-distributions" / f"{self.identifier}.toml"
        if not path.is_file():
            raise BuildError(f"distribution manifest is missing: {path}")
        with path.open("rb") as stream:
            distribution = tomllib.load(stream)
        if distribution.get("schema_version") != 1:
            raise BuildError(f"unsupported distribution schema in {path}")
        if distribution.get("recipe") != self.identifier:
            raise BuildError(f"distribution recipe does not match {self.identifier}")
        return distribution


def load_recipe(path: Path) -> Recipe:
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    if data.get("schema_version") != 1:
        raise BuildError(f"unsupported recipe schema in {path}")
    for section in ("source", "conversion", "quantization", "output"):
        if not isinstance(data.get(section), dict):
            raise BuildError(f"recipe is missing [{section}]")
    return Recipe(path=path.resolve(), data=data)


def geer_home() -> Path:
    configured = os.environ.get("GEER_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".geer"


def default_cache_dir() -> Path:
    return geer_home() / "cache" / "huggingface"


def default_output_dir(recipe: Recipe) -> Path:
    return geer_home() / "models" / "candidates" / recipe.identifier


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def human_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def recipe_summary(recipe: Recipe, cache_dir: Path, output_dir: Path) -> dict[str, Any]:
    expected = int(recipe.source["expected_bytes"])
    return {
        "recipe": recipe.identifier,
        "source_repo": recipe.source["repo_id"],
        "source_revision": recipe.source["revision"],
        "source_download_bytes": expected,
        "source_download_human": human_bytes(expected),
        "cache_dir": str(cache_dir),
        "output_dir": str(output_dir),
        "quantization": recipe.quantization,
        "activation": "manual",
    }


def require_disk_space(path: Path, required: int) -> None:
    probe = path
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < required:
        raise BuildError(
            f"insufficient free space at {probe}: "
            f"need {human_bytes(required)}, have {human_bytes(free)}"
        )


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    ensure_private_dir(path.parent)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(16 * 1024**2):
            digest.update(chunk)
    return digest.hexdigest()


def hash_files(root: Path, *, excluded: set[str] | None = None) -> list[dict[str, Any]]:
    excluded = excluded or set()
    records = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded or relative.startswith(".cache/"):
            continue
        records.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return records


def verify_safetensors_headers(root: Path) -> None:
    """Reject truncated or malformed safetensors before model conversion."""

    for path in sorted(root.rglob("*.safetensors")):
        try:
            with path.open("rb") as stream:
                raw_length = stream.read(8)
                if len(raw_length) != 8:
                    raise BuildError(f"safetensors header length is truncated: {path}")
                header_length = int.from_bytes(raw_length, "little")
                if header_length > MAX_SAFETENSORS_HEADER_BYTES:
                    raise BuildError(
                        f"safetensors header is unreasonably large ({header_length} bytes): {path}"
                    )
                header = stream.read(header_length)
                if len(header) != header_length:
                    raise BuildError(f"safetensors header is truncated: {path}")
                json.loads(header)
        except BuildError:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise BuildError(f"safetensors header is invalid: {path}") from error


def state_path(recipe: Recipe) -> Path:
    return geer_home() / "state" / "model-builds" / f"{recipe.identifier}.json"


def source_manifest_path(recipe: Recipe) -> Path:
    return geer_home() / "state" / "model-builds" / f"{recipe.identifier}-source.json"


def publication_template_dir(recipe: Recipe) -> Path:
    configured = Path(str(recipe.distribution["model_card_dir"]))
    if configured.is_absolute():
        return configured
    distribution_dir = recipe.path.parent.parent / "model-distributions"
    return (distribution_dir / configured).resolve()


def publication_files(recipe: Recipe) -> dict[str, Path]:
    templates = publication_template_dir(recipe)
    license_dir_value = recipe.distribution.get("license_dir")
    if license_dir_value is None:
        license_dir = templates
    else:
        configured = Path(str(license_dir_value))
        license_dir = (
            configured
            if configured.is_absolute()
            else (recipe.path.parent.parent / "model-distributions" / configured).resolve()
        )
    expected = {
        "README.md": templates / "README.md",
        "LICENSE": license_dir / "LICENSE",
        "LICENSE-QWEN": license_dir / "LICENSE-QWEN",
        "NOTICE": license_dir / "NOTICE",
        "geer-recipe.toml": recipe.path,
        "geer-source-manifest.json": source_manifest_path(recipe),
    }
    missing = [name for name, path in expected.items() if not path.is_file()]
    if missing:
        raise BuildError("publication metadata is missing: " + ", ".join(sorted(missing)))
    return expected


def copy_publication_files(recipe: Recipe, output_dir: Path) -> None:
    for name, source in publication_files(recipe).items():
        shutil.copyfile(source, output_dir / name)


def refresh_build_manifest(output_dir: Path) -> dict[str, Any]:
    manifest_path = output_dir / BUILD_MANIFEST
    if not manifest_path.is_file():
        raise BuildError(f"build manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = hash_files(output_dir, excluded={BUILD_MANIFEST})
    manifest["files"] = files
    manifest["bytes"] = sum(record["bytes"] for record in files)
    atomic_json(manifest_path, manifest)
    return manifest


def prepare_publication(recipe: Recipe, output_dir: Path) -> dict[str, Any]:
    verify_output(recipe, output_dir)
    copy_publication_files(recipe, output_dir)
    refresh_build_manifest(output_dir)
    return verify_output(recipe, output_dir)


def download_source(recipe: Recipe, cache_dir: Path) -> Path:
    require_disk_space(
        cache_dir,
        int(recipe.source["expected_bytes"]) + SAFETY_MARGIN_BYTES,
    )
    ensure_private_dir(cache_dir)

    from huggingface_hub import snapshot_download

    snapshot = Path(
        snapshot_download(
            repo_id=recipe.source["repo_id"],
            revision=recipe.source["revision"],
            cache_dir=cache_dir,
        )
    ).resolve()
    resolved_revision = snapshot.name
    if resolved_revision != recipe.source["revision"]:
        raise BuildError(
            f"download resolved to {resolved_revision}, expected {recipe.source['revision']}"
        )

    files = hash_files(snapshot)
    verify_safetensors_headers(snapshot)
    actual_bytes = sum(record["bytes"] for record in files)
    expected_bytes = int(recipe.source["expected_bytes"])
    if actual_bytes != expected_bytes:
        raise BuildError(f"source contains {actual_bytes} bytes, expected {expected_bytes}")

    manifest = {
        "schema_version": 1,
        "recipe": recipe.identifier,
        "repo_id": recipe.source["repo_id"],
        "revision": recipe.source["revision"],
        "license": recipe.source["license"],
        "license_evidence_url": recipe.source["license_evidence_url"],
        "bytes": actual_bytes,
        "files": files,
    }
    manifest_path = source_manifest_path(recipe)
    atomic_json(manifest_path, manifest)
    atomic_json(
        state_path(recipe),
        {
            "schema_version": 1,
            "recipe": recipe.identifier,
            "source_snapshot": str(snapshot),
            "source_manifest": str(manifest_path),
        },
    )
    return snapshot


def find_source(recipe: Recipe, cache_dir: Path) -> Path:
    state = state_path(recipe)
    if state.exists():
        data = json.loads(state.read_text(encoding="utf-8"))
        snapshot = Path(data["source_snapshot"])
        if snapshot.is_dir():
            return snapshot

    from huggingface_hub import snapshot_download

    try:
        return Path(
            snapshot_download(
                repo_id=recipe.source["repo_id"],
                revision=recipe.source["revision"],
                cache_dir=cache_dir,
                local_files_only=True,
            )
        ).resolve()
    except Exception as error:
        raise BuildError("source is not downloaded; run the download command first") from error


def verify_source(recipe: Recipe, snapshot: Path) -> dict[str, Any]:
    snapshot = snapshot.resolve(strict=True)
    if snapshot.name != recipe.source["revision"]:
        raise BuildError(
            f"source resolved to {snapshot.name}, expected {recipe.source['revision']}"
        )

    manifest_path = source_manifest_path(recipe)
    if not manifest_path.is_file():
        raise BuildError("source manifest is missing; run the download command first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_identity = {
        "recipe": recipe.identifier,
        "repo_id": recipe.source["repo_id"],
        "revision": recipe.source["revision"],
        "license": recipe.source["license"],
        "license_evidence_url": recipe.source["license_evidence_url"],
        "bytes": int(recipe.source["expected_bytes"]),
    }
    for key, expected in expected_identity.items():
        if manifest.get(key) != expected:
            raise BuildError(f"source manifest {key} does not match the recipe")

    verify_safetensors_headers(snapshot)
    actual_files = hash_files(snapshot)
    if manifest.get("files") != actual_files:
        raise BuildError("source files do not match the downloaded source manifest")
    return {
        "manifest": manifest,
        "sha256": sha256_file(manifest_path),
    }


def fuse_ornith_experts(
    weights: dict[str, Any],
    *,
    num_layers: int,
    num_experts: int,
    mx_module: Any,
) -> dict[str, Any]:
    """Fuse per-expert Ornith tensors into the batched layout expected by MLX."""

    for layer in range(num_layers):
        prefix = f"model.language_model.layers.{layer}.mlp"
        fused_gate_up = f"{prefix}.experts.gate_up_proj"
        fused_down = f"{prefix}.experts.down_proj"
        first_unfused = f"{prefix}.experts.0.gate_proj.weight"

        if fused_gate_up in weights and fused_down in weights:
            if first_unfused in weights:
                raise BuildError(f"layer {layer} mixes fused and unfused expert tensors")
            continue
        if first_unfused not in weights:
            raise BuildError(f"layer {layer} has neither fused nor unfused expert tensors")

        gate = []
        up = []
        down = []
        for expert in range(num_experts):
            expert_prefix = f"{prefix}.experts.{expert}"
            try:
                gate.append(weights.pop(f"{expert_prefix}.gate_proj.weight"))
                up.append(weights.pop(f"{expert_prefix}.up_proj.weight"))
                down.append(weights.pop(f"{expert_prefix}.down_proj.weight"))
            except KeyError as error:
                raise BuildError(
                    f"layer {layer}, expert {expert} is missing {error.args[0]}"
                ) from error

        gate_batch = mx_module.stack(gate, axis=0)
        up_batch = mx_module.stack(up, axis=0)
        weights[fused_gate_up] = mx_module.concatenate(
            [gate_batch, up_batch],
            axis=-2,
        )
        weights[fused_down] = mx_module.stack(down, axis=0)

    return weights


def install_ornith_expert_patch() -> None:
    import mlx.core as mx
    from mlx_vlm.models.qwen3_5_moe import Model

    original = Model.sanitize
    if getattr(original, "_geer_ornith_patch", False):
        return

    def sanitize(self: Any, weights: dict[str, Any]) -> dict[str, Any]:
        config = self.config.text_config
        fuse_ornith_experts(
            weights,
            num_layers=config.num_hidden_layers,
            num_experts=config.num_experts,
            mx_module=mx,
        )
        return original(self, weights)

    sanitize._geer_ornith_patch = True  # type: ignore[attr-defined]
    Model.sanitize = sanitize


def package_versions(recipe: Recipe) -> dict[str, str]:
    expected = {
        "mlx": recipe.conversion["mlx"],
        "mlx-lm": recipe.conversion["mlx_lm"],
        "mlx-vlm": recipe.conversion["mlx_vlm"],
        "huggingface-hub": recipe.conversion["huggingface_hub"],
        "hf-xet": recipe.conversion["hf_xet"],
        "safetensors": recipe.conversion["safetensors"],
    }
    actual = {name: importlib.metadata.version(name) for name in expected}
    mismatches = {
        name: {"expected": expected[name], "actual": version}
        for name, version in actual.items()
        if version != expected[name]
    }
    if mismatches:
        raise BuildError(f"conversion dependency mismatch: {mismatches}")
    return actual


def quantization_predicate(recipe: Recipe) -> Any | None:
    """Build the recipe's optional per-module quantization policy."""

    quantization = recipe.quantization
    strategy = quantization.get("strategy")
    if strategy is None:
        return None
    if strategy != ROUTED_EXPERTS_4BIT_PROTECTED_TEXT_8BIT:
        raise BuildError(f"unsupported quantization strategy: {strategy}")

    low_bits = int(quantization["bits"])
    high_bits = int(quantization["high_bits"])
    group_size = int(quantization["group_size"])
    mode = str(quantization["mode"])
    if (low_bits, high_bits) != (4, 8):
        raise BuildError("routed-expert mixed quantization requires bits=4 and high_bits=8")

    def predicate(path: str, _module: Any) -> bool | dict[str, Any]:
        # Preserve the multimodal tower in BF16. Geer's lower-bit policy applies
        # only to the routed expert matrices; all other eligible text matrices
        # stay at eight bits.
        if not path.startswith("language_model."):
            return False
        bits = low_bits if ".mlp.switch_mlp." in path else high_bits
        return {"group_size": group_size, "bits": bits, "mode": mode}

    return predicate


def verify_quantization_layout(recipe: Recipe, config: dict[str, Any]) -> None:
    """Verify that a converted config records the requested bit layout."""

    quantization = config.get("quantization")
    if not isinstance(quantization, dict):
        raise BuildError("converted config quantization metadata is missing")
    expected = recipe.quantization
    if int(quantization.get("bits", 0)) != int(expected["bits"]):
        raise BuildError("converted config default quantization bits do not match")
    if int(quantization.get("group_size", 0)) != int(expected["group_size"]):
        raise BuildError("converted config quantization group size does not match")
    if str(quantization.get("mode")) != str(expected["mode"]):
        raise BuildError("converted config quantization mode does not match")

    strategy = expected.get("strategy")
    if strategy is None:
        return
    if strategy != ROUTED_EXPERTS_4BIT_PROTECTED_TEXT_8BIT:
        raise BuildError(f"unsupported quantization strategy: {strategy}")

    layer_entries = {
        path: params for path, params in quantization.items() if isinstance(params, dict)
    }
    if not layer_entries:
        raise BuildError("mixed quantization metadata has no per-layer entries")
    low_bits = int(expected["bits"])
    high_bits = int(expected["high_bits"])
    routed = 0
    protected = 0
    for path, params in layer_entries.items():
        if not path.startswith("language_model."):
            raise BuildError(f"mixed quantization unexpectedly includes {path}")
        expected_bits = low_bits if ".mlp.switch_mlp." in path else high_bits
        if int(params.get("bits", 0)) != expected_bits:
            raise BuildError(f"mixed quantization bits do not match for {path}")
        if int(params.get("group_size", 0)) != int(expected["group_size"]):
            raise BuildError(f"mixed quantization group size does not match for {path}")
        if expected_bits == low_bits:
            routed += 1
        else:
            protected += 1
    if routed == 0 or protected == 0:
        raise BuildError("mixed quantization must include routed and protected matrices")


def convert_model(recipe: Recipe, source: Path, output_dir: Path) -> Path:
    if output_dir.exists():
        raise BuildError(f"output already exists: {output_dir}")
    ensure_private_dir(output_dir.parent)
    require_disk_space(output_dir.parent, int(recipe.source["expected_bytes"]))

    if recipe.conversion["transform"] != "qwen3_5_moe_unfused_experts":
        raise BuildError(f"unsupported transform: {recipe.conversion['transform']}")

    source_provenance = verify_source(recipe, source)
    versions = package_versions(recipe)
    install_ornith_expert_patch()

    from mlx_vlm.convert import convert

    quantization = recipe.quantization
    convert(
        hf_path=str(source),
        mlx_path=str(output_dir),
        quantize=bool(quantization["enabled"]),
        q_group_size=int(quantization["group_size"]),
        q_bits=int(quantization["bits"]),
        q_mode=str(quantization["mode"]),
        quant_predicate=quantization_predicate(recipe),
        dtype=str(recipe.conversion["dtype"]),
        trust_remote_code=False,
    )

    copy_publication_files(recipe, output_dir)
    files = hash_files(output_dir, excluded={BUILD_MANIFEST})
    atomic_json(
        output_dir / BUILD_MANIFEST,
        {
            "schema_version": 1,
            "recipe": recipe.identifier,
            "display_name": recipe.data["display_name"],
            "source": {
                "repo_id": recipe.source["repo_id"],
                "revision": recipe.source["revision"],
                "license": recipe.source["license"],
                "license_evidence_url": recipe.source["license_evidence_url"],
                "manifest_sha256": source_provenance["sha256"],
            },
            "conversion": {
                "transform": recipe.conversion["transform"],
                "dtype": recipe.conversion["dtype"],
                "packages": versions,
            },
            "quantization": recipe.quantization,
            "files": files,
            "bytes": sum(record["bytes"] for record in files),
            "activation": "manual",
        },
    )
    return output_dir


def verify_output(recipe: Recipe, output_dir: Path) -> dict[str, Any]:
    manifest_path = output_dir / BUILD_MANIFEST
    if not manifest_path.is_file():
        raise BuildError(f"build manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("recipe") != recipe.identifier:
        raise BuildError("build manifest recipe does not match")
    if manifest.get("display_name") != recipe.data["display_name"]:
        raise BuildError("build manifest display name does not match")
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise BuildError("build manifest source is missing")
    for key in ("repo_id", "revision", "license", "license_evidence_url"):
        if source.get(key) != recipe.source[key]:
            raise BuildError(f"build manifest source {key} does not match")
    if manifest.get("quantization") != recipe.quantization:
        raise BuildError("build manifest quantization does not match")

    config_path = output_dir / "config.json"
    index_path = output_dir / "model.safetensors.index.json"
    if not config_path.is_file() or not index_path.is_file():
        raise BuildError("converted config or weight index is missing")

    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("model_type") != recipe.output["expected_architecture"]:
        raise BuildError(f"unexpected model type: {config.get('model_type')}")
    verify_quantization_layout(recipe, config)

    expected_files = {record["path"]: record for record in manifest["files"]}
    actual_files = {
        record["path"]: record
        for record in hash_files(
            output_dir,
            excluded={BUILD_MANIFEST, ".gitattributes"},
        )
    }
    if set(actual_files) != set(expected_files):
        raise BuildError("converted file list does not match the build manifest")
    for relative, record in expected_files.items():
        path = output_dir / relative
        if not path.is_file():
            raise BuildError(f"converted file is missing: {relative}")
        if path.stat().st_size != record["bytes"]:
            raise BuildError(f"converted file size changed: {relative}")
        if sha256_file(path) != record["sha256"]:
            raise BuildError(f"converted file hash changed: {relative}")

    return {
        "recipe": recipe.identifier,
        "output_dir": str(output_dir),
        "bytes": manifest["bytes"],
        "files": len(expected_files),
        "model_type": config["model_type"],
        "verified": True,
        "activation": "manual",
    }


def publication_summary(recipe: Recipe, output_dir: Path) -> dict[str, Any]:
    verified = verify_output(recipe, output_dir)
    repo_id = str(recipe.distribution["repo_id"])
    namespace = repo_id.partition("/")[0]
    return {
        **verified,
        "repo_id": repo_id,
        "namespace": namespace,
        "visibility": "private until verified",
        "tag": str(recipe.distribution["tag"]),
        "local_model_copy": str(output_dir),
        "upload_cache": str(geer_home() / "cache" / "xet"),
    }


def require_publish_confirmation(recipe: Recipe, confirmation: str) -> str:
    repo_id = str(recipe.distribution["repo_id"])
    if confirmation != repo_id:
        raise BuildError(f"publication requires --confirm-repo-id {repo_id}")
    return repo_id


def hugging_face_api() -> Any:
    from huggingface_hub import HfApi

    api = HfApi()
    identity = api.whoami()
    access = identity.get("auth", {}).get("accessToken", {})
    if access.get("role") != "write":
        raise BuildError("the active Hugging Face token does not have write access")
    return api


def publish_private(
    recipe: Recipe,
    output_dir: Path,
    confirmation: str,
) -> dict[str, Any]:
    repo_id = require_publish_confirmation(recipe, confirmation)
    summary = publication_summary(recipe, output_dir)
    api = hugging_face_api()
    identity = api.whoami()
    if identity.get("name") != summary["namespace"]:
        raise BuildError(
            f"authenticated as {identity.get('name')}, expected {summary['namespace']}"
        )

    try:
        from huggingface_hub.errors import RepositoryNotFoundError

        try:
            existing = api.model_info(repo_id)
        except RepositoryNotFoundError:
            existing = None
        if existing is not None and not existing.private:
            raise BuildError(
                "refusing to upload through the private publication command "
                "because the model repository is already public"
            )
        api.create_repo(repo_id=repo_id, repo_type="model", private=True, exist_ok=True)
        api.upload_folder(
            repo_id=repo_id,
            repo_type="model",
            folder_path=output_dir,
            ignore_patterns=[".cache/**"],
            commit_message="Publish verified Geer MLX conversion",
            commit_description=(
                "Upload the independently converted model, reproducible recipe, "
                "source and build manifests, model card, licenses, and notices."
            ),
        )
        info = api.model_info(repo_id, files_metadata=True)
    except BuildError:
        raise
    except Exception as error:
        raise BuildError(f"Hugging Face upload failed: {error}") from error
    if not info.private:
        raise BuildError("new model repository is unexpectedly public")
    return {
        **summary,
        "revision": info.sha,
        "private": info.private,
        "url": f"https://huggingface.co/{repo_id}",
    }


def distribution_revision(recipe: Recipe) -> str:
    revision = recipe.distribution.get("revision")
    if not isinstance(revision, str) or len(revision) != 40:
        raise BuildError("recipe does not contain a pinned distribution revision")
    return revision


def verify_published(
    recipe: Recipe,
    output_dir: Path,
    *,
    require_private: bool | None = None,
) -> dict[str, Any]:
    local = verify_output(recipe, output_dir)
    repo_id = str(recipe.distribution["repo_id"])
    revision = distribution_revision(recipe)

    from huggingface_hub import HfApi, hf_hub_download

    api = HfApi()
    try:
        info = api.model_info(repo_id, revision=revision, files_metadata=True)
    except Exception as error:
        raise BuildError(f"cannot inspect published model: {error}") from error
    if info.sha != revision:
        raise BuildError(f"published revision resolved to {info.sha}, expected {revision}")
    if require_private is not None and info.private is not require_private:
        expected = "private" if require_private else "public"
        raise BuildError(f"published model is not {expected}")

    manifest = json.loads((output_dir / BUILD_MANIFEST).read_text(encoding="utf-8"))
    expected = {record["path"]: record for record in manifest["files"]}
    expected[BUILD_MANIFEST] = {
        "path": BUILD_MANIFEST,
        "bytes": (output_dir / BUILD_MANIFEST).stat().st_size,
        "sha256": sha256_file(output_dir / BUILD_MANIFEST),
    }
    remote = {sibling.rfilename: sibling for sibling in info.siblings}
    allowed_extra = {".gitattributes"}
    unexpected = set(remote) - set(expected) - allowed_extra
    missing = set(expected) - set(remote)
    if unexpected or missing:
        raise BuildError(
            f"published file list mismatch: missing={sorted(missing)}, "
            f"unexpected={sorted(unexpected)}"
        )

    metadata_cache = geer_home() / "cache" / "publication-verification"
    verified_files = 0
    for relative, record in expected.items():
        sibling = remote[relative]
        if sibling.size != record["bytes"]:
            raise BuildError(f"published file size changed: {relative}")
        lfs = getattr(sibling, "lfs", None)
        remote_sha256 = getattr(lfs, "sha256", None)
        if remote_sha256 is not None:
            if remote_sha256 != record["sha256"]:
                raise BuildError(f"published file hash changed: {relative}")
        else:
            downloaded = Path(
                hf_hub_download(
                    repo_id=repo_id,
                    revision=revision,
                    filename=relative,
                    cache_dir=metadata_cache,
                )
            )
            if sha256_file(downloaded) != record["sha256"]:
                raise BuildError(f"published file hash changed: {relative}")
        verified_files += 1

    return {
        **local,
        "repo_id": repo_id,
        "revision": revision,
        "private": info.private,
        "remote_files_verified": verified_files,
        "url": f"https://huggingface.co/{repo_id}/tree/{revision}",
    }


def release_published(
    recipe: Recipe,
    output_dir: Path,
    confirmation: str,
) -> dict[str, Any]:
    repo_id = require_publish_confirmation(recipe, confirmation)
    verify_published(recipe, output_dir, require_private=True)
    api = hugging_face_api()
    tag = str(recipe.distribution["tag"])
    revision = distribution_revision(recipe)
    try:
        api.create_tag(
            repo_id=repo_id,
            repo_type="model",
            tag=tag,
            revision=revision,
            tag_message="First verified Geer MLX conversion",
            exist_ok=True,
        )
        api.update_repo_settings(repo_id, repo_type="model", private=False)
    except Exception as error:
        raise BuildError(f"cannot publish verified model: {error}") from error
    released = verify_published(recipe, output_dir, require_private=False)
    released["tag"] = tag
    return released


def published_install_plan(recipe: Recipe, cache_dir: Path) -> dict[str, Any]:
    repo_id = str(recipe.distribution["repo_id"])
    revision = distribution_revision(recipe)
    from huggingface_hub import snapshot_download

    try:
        files = snapshot_download(
            repo_id=repo_id,
            revision=revision,
            cache_dir=cache_dir,
            dry_run=True,
        )
    except Exception as error:
        raise BuildError(f"cannot plan published model download: {error}") from error
    download_bytes = sum(item.file_size for item in files if item.will_download)
    required_free_bytes = download_bytes + SAFETY_MARGIN_BYTES
    return {
        "repo_id": repo_id,
        "revision": revision,
        "download_bytes": download_bytes,
        "download_human": human_bytes(download_bytes),
        "required_free_bytes": required_free_bytes,
        "required_free_human": human_bytes(required_free_bytes),
        "installed_bytes": sum(item.file_size for item in files),
        "installed_human": human_bytes(sum(item.file_size for item in files)),
        "cache_dir": str(cache_dir),
        "active_link": str(geer_home() / "models" / "active"),
        "activation": "after download and full hash verification",
    }


def activate_snapshot(snapshot: Path) -> dict[str, Any]:
    models = geer_home() / "models"
    ensure_private_dir(models)
    active = models / "active"
    previous = models / "previous"
    temporary = models / f".active-{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(snapshot)

    old_target: str | None = None
    if active.is_symlink():
        old_target = str(active.resolve(strict=True))
        if previous.exists() and not previous.is_symlink():
            temporary.unlink()
            raise BuildError(f"refusing to replace non-symlink rollback path: {previous}")
        previous_tmp = models / f".previous-{os.getpid()}"
        if previous_tmp.exists() or previous_tmp.is_symlink():
            previous_tmp.unlink()
        previous_tmp.symlink_to(old_target)
        os.replace(previous_tmp, previous)
    elif active.exists():
        temporary.unlink()
        raise BuildError(f"refusing to replace non-symlink activation path: {active}")
    os.replace(temporary, active)
    return {
        "active": str(active),
        "target": str(snapshot),
        "previous": old_target,
    }


def retire_ornith_1_0(previous: str | None) -> dict[str, Any]:
    """Remove the retired Ornith 1.0 activation after a verified upgrade."""

    if previous is None:
        return {"retired": False}

    target = Path(previous).resolve(strict=False)
    manifest_path = target / BUILD_MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"retired": False, "reason": "previous manifest unavailable"}
    recipe = manifest.get("recipe")
    if not isinstance(recipe, str) or not recipe.startswith("ornith-1.0-"):
        return {"retired": False, "recipe": recipe}

    models = geer_home() / "models"
    previous_link = models / "previous"
    if previous_link.is_symlink() and previous_link.resolve(strict=False) == target:
        previous_link.unlink()

    removed_path: str | None = None
    if target.is_relative_to(models):
        shutil.rmtree(target)
        removed_path = str(target)
    else:
        cache = default_cache_dir().resolve(strict=False)
        repository = target.parent.parent
        if repository.parent == cache and repository.name.startswith(
            "models--ilyakam--Geer-Ornith-1.0-"
        ):
            shutil.rmtree(repository)
            removed_path = str(repository)

    return {"retired": True, "recipe": recipe, "removed_path": removed_path}


def install_published(recipe: Recipe, cache_dir: Path) -> dict[str, Any]:
    active = geer_home() / "models" / "active"
    if active.is_symlink() and active.exists():
        snapshot = active.resolve()
        try:
            verified = verify_output(recipe, snapshot)
        except BuildError:
            pass
        else:
            return {
                **verified,
                "repo_id": str(recipe.distribution["repo_id"]),
                "revision": distribution_revision(recipe),
                "activation": {
                    "active": str(active),
                    "target": str(snapshot),
                    "previous": None,
                    "reused": True,
                },
            }
    elif active.is_symlink():
        active.unlink()

    plan = published_install_plan(recipe, cache_dir)
    require_disk_space(cache_dir, int(plan["required_free_bytes"]))
    ensure_private_dir(cache_dir)
    from huggingface_hub import snapshot_download

    try:
        snapshot = Path(
            snapshot_download(
                repo_id=plan["repo_id"],
                revision=plan["revision"],
                cache_dir=cache_dir,
            )
        ).resolve()
    except Exception as error:
        raise BuildError(f"published model download failed: {error}") from error
    verified = verify_output(recipe, snapshot)
    activation = activate_snapshot(snapshot)
    activation["retired_previous"] = retire_ornith_1_0(activation["previous"])
    return {
        **verified,
        "repo_id": plan["repo_id"],
        "revision": plan["revision"],
        "activation": activation,
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    command.add_argument("--cache-dir", type=Path)
    command.add_argument("--output-dir", type=Path)
    command.add_argument(
        "--high-performance-transfer",
        "--high-performance-download",
        dest="high_performance_transfer",
        action="store_true",
        help="let Xet use more CPU, memory, and bandwidth (64 GB+ Macs)",
    )
    subcommands = command.add_subparsers(dest="command", required=True)
    subcommands.add_parser("plan", help="show paths, revisions, and expected sizes")
    subcommands.add_parser("download", help="download and hash the pinned BF16 source")
    subcommands.add_parser("convert", help="convert a downloaded source snapshot")
    subcommands.add_parser("verify", help="verify every generated output hash")
    subcommands.add_parser(
        "prepare-publication",
        help="add model-card, license, recipe, and provenance files to a build",
    )
    subcommands.add_parser(
        "publish-plan",
        help="verify and preview the private Hugging Face publication",
    )
    publish = subcommands.add_parser(
        "publish",
        help="create a private model repository and upload the verified build",
    )
    publish.add_argument("--confirm-repo-id", required=True)
    subcommands.add_parser(
        "verify-published",
        help="verify the pinned Hugging Face revision against local hashes",
    )
    release = subcommands.add_parser(
        "release-published",
        help="tag and make the verified Hugging Face model public",
    )
    release.add_argument("--confirm-repo-id", required=True)
    subcommands.add_parser(
        "install-plan",
        help="show the pinned preconverted model download and disk commitment",
    )
    subcommands.add_parser(
        "install",
        help="download, verify, and atomically activate the published model",
    )
    subcommands.add_parser("all", help="download, convert, and verify")
    return command


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_XET_CACHE", str(geer_home() / "cache" / "xet"))
    if options.high_performance_transfer:
        os.environ["HF_XET_HIGH_PERFORMANCE"] = "1"
    recipe = load_recipe(options.recipe)
    cache_dir = (options.cache_dir or default_cache_dir()).expanduser()
    output_dir = (options.output_dir or default_output_dir(recipe)).expanduser()

    try:
        if options.command == "plan":
            result = recipe_summary(recipe, cache_dir, output_dir)
        elif options.command == "download":
            result = {"source_snapshot": str(download_source(recipe, cache_dir))}
        elif options.command == "convert":
            source = find_source(recipe, cache_dir)
            result = {"output_dir": str(convert_model(recipe, source, output_dir))}
        elif options.command == "verify":
            result = verify_output(recipe, output_dir)
        elif options.command == "prepare-publication":
            result = prepare_publication(recipe, output_dir)
        elif options.command == "publish-plan":
            result = publication_summary(recipe, output_dir)
        elif options.command == "publish":
            result = publish_private(
                recipe,
                output_dir,
                options.confirm_repo_id,
            )
        elif options.command == "verify-published":
            result = verify_published(recipe, output_dir)
        elif options.command == "release-published":
            result = release_published(
                recipe,
                output_dir,
                options.confirm_repo_id,
            )
        elif options.command == "install-plan":
            result = published_install_plan(recipe, cache_dir)
        elif options.command == "install":
            result = install_published(recipe, cache_dir)
        elif options.command == "all":
            source = download_source(recipe, cache_dir)
            convert_model(recipe, source, output_dir)
            result = verify_output(recipe, output_dir)
        else:
            raise AssertionError("unreachable")
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
