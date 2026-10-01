from __future__ import annotations

import ctypes
import hashlib
import os
import plistlib
import posixpath
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

T3_VERSION = "0.0.44"
T3_REVISION = "451afcb22d93f06cb24f9bc16703404564952553"
T3_ARCHIVE_URL = (
    f"https://github.com/pingdotgg/t3code/releases/download/v{T3_VERSION}/"
    f"T3-Code-{T3_VERSION}-arm64.zip"
)
T3_ARCHIVE_BYTES = 137_613_348
T3_ARCHIVE_SHA256 = "480b5cd8ebcee4f4f43e108d309d91fe7c879931d8aae8a67b71cfd8a07ce0df"
# Regular file bytes, excluding the signed framework symlinks.
T3_INSTALLED_BYTES = 334_315_639
T3_BUNDLE_ID = "com.t3tools.t3code"
T3_BUNDLE_NAME = "T3 Code (Alpha).app"
T3_EXECUTABLE = "T3 Code (Alpha)"
T3_TEAM_ID = "ARK85ZXQ4Z"
T3_SIGNATURE_REQUIREMENT = (
    f'identifier "{T3_BUNDLE_ID}" and anchor apple generic '
    "and certificate 1[field.1.2.840.113635.100.6.2.6] exists "
    "and certificate leaf[field.1.2.840.113635.100.6.1.13] exists "
    f'and certificate leaf[subject.OU] = "{T3_TEAM_ID}"'
)
T3_SOURCE_URL = f"https://github.com/pingdotgg/t3code/tree/{T3_REVISION}"
T3_LICENSE_URL = f"https://github.com/pingdotgg/t3code/blob/{T3_REVISION}/LICENSE"


class T3DistributionError(RuntimeError):
    """Raised when the pinned T3 Code desktop app cannot be installed safely."""


def t3_distribution_manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "T3 Code",
        "version": T3_VERSION,
        "revision": T3_REVISION,
        "platform": "darwin-arm64",
        "archive": {
            "url": T3_ARCHIVE_URL,
            "bytes": T3_ARCHIVE_BYTES,
            "sha256": T3_ARCHIVE_SHA256,
        },
        "installed_bytes": T3_INSTALLED_BYTES,
        "bundle_name": T3_BUNDLE_NAME,
        "bundle_id": T3_BUNDLE_ID,
        "executable": T3_EXECUTABLE,
        "signature": {
            "team_id": T3_TEAM_ID,
            "requirement": T3_SIGNATURE_REQUIREMENT,
        },
        "source_url": T3_SOURCE_URL,
        "license": "MIT",
        "license_url": T3_LICENSE_URL,
        "copyright": "Copyright (c) 2026 T3 Tools Inc.",
    }


def t3_distribution_plan(destination: Path) -> dict[str, Any]:
    """Describe installation without inspecting, downloading, or changing files."""
    return {
        **t3_distribution_manifest(),
        "destination": str(destination),
        "executable": str(destination / "Contents/MacOS" / T3_EXECUTABLE),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_archive(archive: Path) -> None:
    if not archive.is_file() or archive.stat().st_size != T3_ARCHIVE_BYTES:
        raise T3DistributionError("T3 Code archive size does not match the pinned release")
    if _sha256(archive) != T3_ARCHIVE_SHA256:
        raise T3DistributionError("T3 Code archive SHA-256 does not match the pinned release")


def _download_archive(destination: Path) -> None:
    print(f"  Downloading T3 Code {T3_VERSION} from its official release", file=sys.stderr)
    descriptor, filename = tempfile.mkstemp(prefix=".t3-download-", dir=destination.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as output:
            with urllib.request.urlopen(T3_ARCHIVE_URL, timeout=60) as response:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > T3_ARCHIVE_BYTES:
                        raise T3DistributionError(
                            "T3 Code download exceeds the pinned archive size"
                        )
                    output.write(chunk)
        _verify_archive(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _run(command: list[str], action: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=True, timeout=120)
    except subprocess.CalledProcessError as error:
        details = (error.stderr or error.stdout or str(error)).strip()
        raise T3DistributionError(f"could not {action}: {details}") from error
    except subprocess.TimeoutExpired as error:
        raise T3DistributionError(f"timed out while trying to {action}") from error
    except OSError as error:
        raise T3DistributionError(f"could not {action}: {error}") from error


def _extract_archive(archive: Path, destination: Path) -> None:
    """Validate zip paths, then retain upstream permissions and symlinks with ditto."""
    with zipfile.ZipFile(archive) as bundle:
        paths: set[PurePosixPath] = set()
        links: set[PurePosixPath] = set()
        total = 0
        for entry in bundle.infolist():
            path = PurePosixPath(entry.filename)
            kind = stat.S_IFMT(entry.external_attr >> 16)
            if (
                path.is_absolute()
                or not path.parts
                or path.parts[0] != T3_BUNDLE_NAME
                or ".." in path.parts
                or "\\" in entry.filename
                or path in paths
                or kind not in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK)
            ):
                raise T3DistributionError(
                    f"T3 Code archive contains an unsafe entry: {entry.filename}"
                )
            paths.add(path)
            if kind == stat.S_IFLNK:
                target = bundle.read(entry).decode("utf-8")
                resolved = PurePosixPath(posixpath.normpath(str(path.parent / target)))
                if (
                    not target
                    or PurePosixPath(target).is_absolute()
                    or "\\" in target
                    or "\x00" in target
                    or not resolved.parts
                    or resolved.parts[0] != T3_BUNDLE_NAME
                ):
                    raise T3DistributionError(
                        f"T3 Code archive contains an unsafe symlink: {entry.filename}"
                    )
                links.add(path)
            elif not entry.is_dir():
                total += entry.file_size
        if any(parent in links for path in paths for parent in path.parents):
            raise T3DistributionError("T3 Code archive contains entries below a symlink")
        if total != T3_INSTALLED_BYTES:
            raise T3DistributionError("T3 Code installed size does not match the pinned release")
    _run(
        ["/usr/bin/ditto", "-x", "-k", str(archive), str(destination)],
        "extract the T3 Code archive with macOS ditto",
    )


def _verify_bundle(directory: Path) -> None:
    metadata_path = directory / "Contents/Info.plist"
    if directory.is_symlink() or not directory.is_dir() or metadata_path.is_symlink():
        raise T3DistributionError("T3 Code app bundle is missing or symlinked")
    try:
        metadata = plistlib.loads(metadata_path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException) as error:
        raise T3DistributionError(f"cannot read T3 Code bundle metadata: {error}") from error
    expected = {
        "CFBundleIdentifier": T3_BUNDLE_ID,
        "CFBundleShortVersionString": T3_VERSION,
        "CFBundleVersion": T3_VERSION,
        "CFBundleExecutable": T3_EXECUTABLE,
    }
    if not isinstance(metadata, dict) or any(
        metadata.get(key) != value for key, value in expected.items()
    ):
        raise T3DistributionError("T3 Code bundle metadata does not match the pinned release")
    executable = directory / "Contents/MacOS" / T3_EXECUTABLE
    if executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise T3DistributionError("T3 Code app executable is missing or not executable")
    architectures = _run(["/usr/bin/lipo", "-archs", str(executable)], "check T3 Code architecture")
    if architectures.stdout.split() != ["arm64"]:
        raise T3DistributionError("T3 Code app executable is not the pinned arm64 build")
    _run(
        [
            "/usr/bin/codesign",
            "--verify",
            "--deep",
            "--strict",
            f"-R={T3_SIGNATURE_REQUIREMENT}",
            str(directory),
        ],
        "verify the official T3 Code signature",
    )


def _promote(staged: Path, destination: Path) -> None:
    """Atomically install on macOS without overwriting a concurrently created app."""
    if sys.platform != "darwin":
        raise T3DistributionError("T3 Code desktop installation requires macOS")
    rename = ctypes.CDLL(None, use_errno=True).renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # macOS RENAME_EXCL refuses every existing destination, including an empty directory.
    if rename(os.fsencode(staged), os.fsencode(destination), 0x00000004) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def install_t3(destination: Path, *, cache: Path, archive: Path | None = None) -> Path:
    """Install into absence, retaining existing apps, caches, and upstream notices."""
    if destination.is_symlink() or destination.parent.is_symlink():
        raise T3DistributionError("refusing to install T3 Code at a symlinked destination")
    if destination.exists():
        try:
            _verify_bundle(destination)
        except T3DistributionError as error:
            raise T3DistributionError(
                f"refusing to replace an existing T3 Code destination at {destination}: {error}. "
                "Use T3 Code's updater or choose an empty installation destination"
            ) from error
        return destination
    try:
        if archive is None:
            if cache.is_symlink():
                raise T3DistributionError("refusing to download T3 Code into a symlinked cache")
            cache.mkdir(parents=True, exist_ok=True, mode=0o700)
            archive = cache / f"T3-Code-{T3_VERSION}-arm64.zip"
            if archive.is_symlink() or (archive.exists() and not archive.is_file()):
                raise T3DistributionError(
                    f"refusing to replace an unmanaged T3 Code cache: {archive}"
                )
            try:
                _verify_archive(archive)
            except T3DistributionError:
                _download_archive(archive)
        _verify_archive(archive)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".geer-t3-install-", dir=destination.parent
        ) as path:
            staged = Path(path)
            _extract_archive(archive, staged)
            _verify_bundle(staged / T3_BUNDLE_NAME)
            _promote(staged / T3_BUNDLE_NAME, destination)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        raise T3DistributionError(f"could not install T3 Code {T3_VERSION}: {error}") from error
    return destination
