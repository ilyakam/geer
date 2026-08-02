from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_MODEL_ID = "geer-local"
DEFAULT_MODEL_ALIAS = "Geer Ornith 1.0 35B-A3B (MLX)"
DEFAULT_MODEL_SOURCE = Path("~/.geer/models/active").expanduser()
BUILD_MANIFEST = "geer-build-manifest.json"
REQUIRED_FILES = {
    BUILD_MANIFEST,
    "chat_template.jinja",
    "config.json",
    "model.safetensors.index.json",
    "tokenizer.json",
    "tokenizer_config.json",
}
OPTIONAL_FILES = {
    "generation_config.json",
    "preprocessor_config.json",
    "video_preprocessor_config.json",
    "vocab.json",
}


class AssetError(RuntimeError):
    """Raised when local model assets are incomplete or unsafe."""


def model_alias(manifest: dict[str, Any]) -> str:
    build = manifest.get("build")
    if isinstance(build, dict) and isinstance(build.get("display_name"), str):
        return build["display_name"]
    quantization = manifest.get("quantization")
    if isinstance(quantization, dict) and isinstance(quantization.get("bits"), int):
        return f"Geer Ornith 1.0 35B-A3B ({quantization['bits']}-bit MLX)"
    return DEFAULT_MODEL_ALIAS


@dataclass(frozen=True)
class Workspace:
    root: Path
    home: Path | None = None

    @property
    def runtime(self) -> Path:
        return self.home or self.root / ".geer"

    @property
    def cache(self) -> Path:
        return self.runtime / "cache"

    @property
    def models(self) -> Path:
        return self.runtime / "models"

    @property
    def state(self) -> Path:
        return self.runtime / "state"

    @property
    def manifest(self) -> Path:
        return self.state / "assets.json"

    @property
    def requests(self) -> Path:
        return self.state / "requests.jsonl"

    @property
    def runtime_metrics(self) -> Path:
        return self.state / "runtime.jsonl"

    @property
    def setup_state(self) -> Path:
        return self.state / "setup.json"

    @property
    def retrieval_cache(self) -> Path:
        return self.runtime / "semble-cache"

    @property
    def retrieval_metrics(self) -> Path:
        return self.state / "retrieval.jsonl"

    @property
    def server_pid(self) -> Path:
        return self.state / "server.pid"

    @property
    def server_log(self) -> Path:
        return self.state / "server.log"

    @property
    def server_endpoint(self) -> Path:
        return self.state / "endpoint.json"

    @property
    def server_lock(self) -> Path:
        return self.state / "server.lock"

    @property
    def launcher_log(self) -> Path:
        return self.state / "launcher.json"

    @property
    def api_key(self) -> Path:
        return self.state / "api-key"

    @property
    def claude_config(self) -> Path:
        return self.runtime / "claude"

    @property
    def omlx_state(self) -> Path:
        return self.runtime / "omlx-state"

    @property
    def omlx_model_settings(self) -> Path:
        return self.omlx_state / "model_settings.json"


def find_workspace(start: Path | None = None) -> Workspace:
    configured = os.environ.get("GEER_WORKSPACE")
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if (candidate / "pyproject.toml").is_file() and (candidate / "AGENTS.md").is_file():
            return Workspace(candidate, _configured_home())
        raise AssetError(f"GEER_WORKSPACE is not a Geer repository: {candidate}")
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "AGENTS.md").is_file():
            return Workspace(candidate, _configured_home())
    raise AssetError("run this command from the Geer repository")


def _configured_home() -> Path | None:
    configured = os.environ.get("GEER_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AssetError(f"cannot read valid JSON from {path}: {error}") from error
    if not isinstance(value, dict):
        raise AssetError(f"expected a JSON object in {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def discover_model_files(source: Path) -> list[Path]:
    source = source.expanduser().resolve(strict=True)
    if not source.is_dir():
        raise AssetError(f"model source is not a directory: {source}")

    missing = sorted(name for name in REQUIRED_FILES if not (source / name).is_file())
    if missing:
        raise AssetError(f"model source is missing required files: {', '.join(missing)}")

    index = _load_json(source / "model.safetensors.index.json")
    weight_map = index.get("weight_map")
    if not isinstance(weight_map, dict) or not weight_map:
        raise AssetError("model index does not contain a non-empty weight_map")

    shard_names = set(weight_map.values())
    if not all(isinstance(name, str) and Path(name).name == name for name in shard_names):
        raise AssetError("model index contains an unsafe shard path")

    selected_names = REQUIRED_FILES | OPTIONAL_FILES | set(shard_names)
    selected = [source / name for name in sorted(selected_names) if (source / name).is_file()]
    missing_shards = sorted(name for name in shard_names if not (source / name).is_file())
    if missing_shards:
        raise AssetError(f"model source is missing indexed shards: {', '.join(missing_shards)}")
    return selected


def initialize_assets(
    workspace: Workspace,
    source: Path = DEFAULT_MODEL_SOURCE,
    model_id: str = DEFAULT_MODEL_ID,
) -> dict[str, Any]:
    if not model_id or model_id in {".", ".."} or "/" in model_id:
        raise AssetError("model ID must be one safe path component")

    source = source.expanduser().resolve(strict=True)
    files = discover_model_files(source)
    model_dir = workspace.models / model_id
    workspace.models.mkdir(parents=True, exist_ok=True)
    workspace.state.mkdir(parents=True, exist_ok=True)

    if model_dir.is_symlink() or (model_dir.exists() and not model_dir.is_dir()):
        raise AssetError(f"refusing to replace unexpected asset path: {model_dir}")
    model_dir.mkdir(exist_ok=True)

    selected_names = {path.name for path in files}
    unexpected = sorted(
        (path for path in model_dir.iterdir() if path.name not in selected_names),
        key=lambda path: path.name,
    )
    unmanaged = [path.name for path in unexpected if not path.is_symlink()]
    if unmanaged:
        raise AssetError(f"asset directory contains unmanaged entries: {', '.join(unmanaged)}")
    for stale_link in unexpected:
        stale_link.unlink()

    entries: list[dict[str, Any]] = []
    for source_file in files:
        target = model_dir / source_file.name
        if target.is_symlink():
            try:
                matches = target.resolve(strict=True) == source_file.resolve(strict=True)
            except FileNotFoundError:
                matches = False
            if not matches:
                temporary_link = target.with_name(f".{target.name}.geer-{os.getpid()}")
                temporary_link.unlink(missing_ok=True)
                temporary_link.symlink_to(source_file)
                os.replace(temporary_link, target)
        elif target.exists():
            raise AssetError(f"refusing to replace non-symlink asset: {target}")
        else:
            target.symlink_to(source_file)

        entries.append(
            {
                "name": source_file.name,
                "bytes": source_file.stat().st_size,
                "sha256": _sha256(source_file),
            }
        )

    config = _load_json(source / "config.json")
    index = _load_json(source / "model.safetensors.index.json")
    build = _load_json(source / BUILD_MANIFEST)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "model_id": model_id,
        "source": str(source),
        "linked_model": str(model_dir),
        "source_version": _read_optional_text(source / "version.txt"),
        "architecture": config.get("architectures"),
        "model_type": config.get("model_type"),
        "context_length": _context_length(config),
        "quantization": {
            key: value
            for key, value in config.get("quantization", {}).items()
            if not isinstance(value, dict)
        },
        "indexed_weight_bytes": index.get("metadata", {}).get("total_size"),
        "build": build,
        "files": entries,
    }
    temporary = workspace.manifest.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, workspace.manifest)
    return manifest


def verify_assets(
    workspace: Workspace,
    *,
    verify_hashes: bool = False,
) -> dict[str, Any]:
    """Validate activated links and sizes, with optional full SHA-256 checks."""
    if not workspace.manifest.is_file():
        raise AssetError(
            f"Geer setup is incomplete for this account; "
            f"{workspace.manifest} is missing. Run `geer setup`."
        )
    manifest = _load_json(workspace.manifest)
    source = Path(str(manifest.get("source", ""))).resolve(strict=True)
    linked_model = Path(str(manifest.get("linked_model", ""))).resolve(strict=True)
    if linked_model == source:
        raise AssetError("expected an isolated model directory containing file symlinks")

    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise AssetError("asset manifest has no files")

    link_root = Path(str(manifest["linked_model"]))
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise AssetError("asset manifest contains an invalid entry")
        link = link_root / entry["name"]
        if not link.is_symlink():
            raise AssetError(f"expected a symlink: {link}")
        resolved = link.resolve(strict=True)
        expected = (source / entry["name"]).resolve(strict=True)
        if resolved != expected:
            raise AssetError(f"asset escapes the declared source: {link}")
        if resolved.stat().st_size != entry.get("bytes"):
            raise AssetError(f"asset size changed: {resolved}")
        if verify_hashes and _sha256(resolved) != entry.get("sha256"):
            raise AssetError(f"asset hash changed: {resolved}")
    return manifest


def _read_optional_text(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def _context_length(config: dict[str, Any]) -> int | None:
    direct = config.get("max_position_embeddings")
    if isinstance(direct, int):
        return direct
    text_config = config.get("text_config")
    if isinstance(text_config, dict):
        nested = text_config.get("max_position_embeddings")
        if isinstance(nested, int):
            return nested
    return None
