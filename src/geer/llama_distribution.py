"""Install the pinned official llama.cpp runtime without a build toolchain."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

from .assets import AssetError, Workspace
from .hardware import UnsupportedHardwareError, require_qwen_macos

LLAMA_VERSION = "b11901"
LLAMA_REVISION = "5320c87ff0d994845b44ce7976f10055ec697447"
LLAMA_MARKER = "geer-llama-runtime.json"
LLAMA_ARCHIVE_ROOT = f"llama-{LLAMA_VERSION}"


def llama_distribution_manifest(workspace: Workspace) -> dict[str, Any]:
    for path in (
        workspace.root / "packaging/llama-runtime.json",
        workspace.root / "llama-runtime.json",
    ):
        if path.is_file():
            value = json.loads(path.read_text())
            if value.get("version") != LLAMA_VERSION or value.get("revision") != LLAMA_REVISION:
                raise AssetError("unexpected llama.cpp runtime manifest")
            return value
    raise AssetError("the pinned llama.cpp runtime manifest is missing")


def runtime_directory(workspace: Workspace) -> Path:
    return workspace.runtime / "llama-runtime"


def archive_path(workspace: Workspace) -> Path:
    return workspace.cache / f"llama-{LLAMA_VERSION}-bin-macos-arm64.tar.gz"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(path: Path, manifest: dict[str, Any]) -> None:
    expected = manifest["archive"]
    if path.stat().st_size != expected["bytes"] or _sha256(path) != expected["sha256"]:
        raise AssetError("llama.cpp archive does not match the pinned release")


def _verify_files(directory: Path, manifest: dict[str, Any]) -> None:
    expected = set(manifest["files_sha256"]) | set(manifest.get("symlinks", {}))
    if {path.name for path in directory.iterdir()} - {LLAMA_MARKER} != expected:
        raise AssetError("llama.cpp runtime contains unexpected files")
    for name, digest in manifest["files_sha256"].items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or _sha256(path) != digest:
            raise AssetError(f"llama.cpp runtime file failed verification: {name}")
    for name, target in manifest.get("symlinks", {}).items():
        path = directory / name
        if not path.is_symlink() or os.readlink(path) != target:
            raise AssetError(f"llama.cpp runtime link failed verification: {name}")
    if not os.access(directory / "llama-server", os.X_OK):
        raise AssetError("llama.cpp server is not executable")


def installed(workspace: Workspace) -> bool:
    directory = runtime_directory(workspace)
    try:
        if directory.is_symlink():
            return False
        manifest = llama_distribution_manifest(workspace)
        marker = directory / LLAMA_MARKER
        if marker.is_symlink() or json.loads(marker.read_text()) != manifest:
            return False
        _verify_files(directory, manifest)
    except (OSError, ValueError, KeyError, AssetError):
        return False
    return True


def _extract(archive: Path, destination: Path, manifest: dict[str, Any]) -> None:
    """Allow only the release's pinned flat files and relative library links."""
    expected_files = set(manifest["files_sha256"])
    expected_links = manifest.get("symlinks", {})
    seen: set[str] = set()
    total = 0
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            path = PurePosixPath(member.name)
            if path.parts == (LLAMA_ARCHIVE_ROOT,) and member.isdir():
                continue
            if (
                len(path.parts) != 2
                or path.parts[0] != LLAMA_ARCHIVE_ROOT
                or path.name in seen
                or "\\" in member.name
            ):
                raise AssetError(f"unsafe llama.cpp archive entry: {member.name}")
            name = path.name
            seen.add(name)
            target = destination / name
            if member.isfile() and name in expected_files:
                source = bundle.extractfile(member)
                if source is None:
                    raise AssetError(f"cannot extract llama.cpp file: {name}")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o700 if member.mode & 0o111 else 0o600)
                total += member.size
            elif (
                member.issym()
                and name in expected_links
                and member.linkname == expected_links[name]
                and PurePosixPath(member.linkname).name == member.linkname
                and member.linkname not in {".", ".."}
            ):
                target.symlink_to(member.linkname)
            else:
                raise AssetError(f"unexpected llama.cpp archive entry: {member.name}")
    if seen != expected_files | set(expected_links) or total != manifest["extracted_bytes"]:
        raise AssetError("llama.cpp archive file inventory does not match its release")


def install_llama(workspace: Workspace, *, archive: Path | None = None) -> Path:
    try:
        require_qwen_macos()
    except UnsupportedHardwareError as error:
        raise AssetError(str(error)) from error
    directory = runtime_directory(workspace)
    if directory.is_symlink():
        raise AssetError("refusing to replace a symlinked llama.cpp runtime")
    manifest = llama_distribution_manifest(workspace)
    if installed(workspace):
        return directory / "llama-server"
    if directory.exists():
        try:
            marker = directory / LLAMA_MARKER
            if (
                marker.is_symlink()
                or json.loads(marker.read_text()).get("name") != manifest["name"]
            ):
                raise ValueError("unmanaged runtime")
        except (OSError, ValueError, AttributeError) as error:
            raise AssetError("refusing to replace an unmanaged llama.cpp runtime") from error
    workspace.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    if archive is None:
        archive = archive_path(workspace)
        try:
            verify_archive(archive, manifest)
        except (OSError, AssetError):
            descriptor, filename = tempfile.mkstemp(prefix=".llama-download-", dir=workspace.cache)
            temporary = Path(filename)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    with urllib.request.urlopen(manifest["archive"]["url"], timeout=60) as response:
                        count = 0
                        while chunk := response.read(1024 * 1024):
                            count += len(chunk)
                            if count > manifest["archive"]["bytes"]:
                                raise AssetError("llama.cpp download exceeds its pinned size")
                            output.write(chunk)
                verify_archive(temporary, manifest)
                os.replace(temporary, archive)
            finally:
                temporary.unlink(missing_ok=True)
    verify_archive(archive, manifest)
    with tempfile.TemporaryDirectory(prefix=".llama-install-", dir=workspace.runtime) as temporary:
        staged = Path(temporary) / "runtime"
        staged.mkdir(mode=0o700)
        _extract(archive, staged, manifest)
        _verify_files(staged, manifest)
        marker = staged / LLAMA_MARKER
        marker.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        marker.chmod(0o600)
        previous = Path(temporary).with_name(Path(temporary).name + ".previous")
        if directory.exists():
            os.replace(directory, previous)
        try:
            os.replace(staged, directory)
        except OSError:
            if previous.exists():
                try:
                    os.replace(previous, directory)
                except OSError as error:
                    raise AssetError(
                        f"llama.cpp installation failed; prior runtime retained at {previous}"
                    ) from error
            raise
        if previous.exists():
            shutil.rmtree(previous)
    return directory / "llama-server"
