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

PI_VERSION = "1.4.0"
PI_REVISION = "4a080252f06caf63a525d7873c5d94d3c23887e0"
PI_ARCHIVE_URL = (
    f"https://github.com/earendil-works/pi/releases/download/v{PI_VERSION}/"
    "pi-darwin-arm64.tar.gz"
)
PI_ARCHIVE_BYTES = 31_098_931
PI_ARCHIVE_SHA256 = "79d9979d76a2a5adb7c28e27e9b44629928ccd0dc9fbb6ae7217860bd5e8be5d"
PI_EXTRACTED_BYTES = 84_513_829
PI_SOURCE_URL = (
    f"https://github.com/earendil-works/pi/releases/download/v{PI_VERSION}/"
    f"pi-{PI_VERSION}-source.tar.gz"
)
PI_LICENSE_URL = f"https://github.com/earendil-works/pi/blob/{PI_REVISION}/LICENSE"
PI_BUN_VERSION = "1.3.14"
PI_BUN_LICENSE_URL = f"https://github.com/oven-sh/bun/blob/bun-v{PI_BUN_VERSION}/LICENSE.md"
PI_FILE_HASHES = {
    "pi": "b4d93b78e542ca8d1792242cf6c1a875ef4e6fb644141fb4cf67ec7c81ef9b14",
    "package.json": "df6883685382d6a914e5797232744c26579fe7db5576ea0e9c859d018ca2d54e",
    "photon_rs_bg.wasm": "10468181565c56004c867f3a4af96f89a0ef5a63a72f2b5fb12c1f1992a3615c",
    "native/darwin/prebuilds/darwin-arm64/darwin-platform.node": (
        "36e909adf35c2734a2a52b3d65b7dbfb83f2278f306d51b7e32c5045516dfb7f"
    ),
    "theme/dark.json": "e1f96982f8cb7d2a48eca7db03c9cb282cb1e14a243680cd855638ecd6a251b6",
    "theme/light.json": "2dee6a4be7f765e7fd7728166b7ebf8c29e4ce313f8ec6b38ae50699e3abc2f3",
    "assets/clankolas.png": (
        "169acd0dfe6fbb8d8742ed24a3fc654fd0b2e2d4223c733249c5493723f1b72d"
    ),
    "export-html/template.html": (
        "47d208235b066c856dae69c60281a3b99d164b99be9b81153d880375c9327105"
    ),
    "export-html/template.css": (
        "8ee19851f8e583277ed396cbb76496687aa556707fdfc1f78d1118bff87c740a"
    ),
    "export-html/template.js": (
        "946bd9e3e8255c1ebb40f806def18007aa9397816291a7bf2ac0e4e99cec07a4"
    ),
    "export-html/vendor/marked.min.js": (
        "438eedfcf932a414d0d0bfeea32dc365c063563b8ca713b4687fc8f8b501e5e4"
    ),
    "export-html/vendor/highlight.min.js": (
        "837a6fa5b0c736b52bbde2b2b6190f305da3fc9ed41681db5321507057b5c846"
    ),
}
PI_MARKER = "geer-pi-runtime.json"


class PiDistributionError(RuntimeError):
    """Raised when the pinned upstream Pi distribution cannot be installed safely."""


def pi_distribution_manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "Pi",
        "version": PI_VERSION,
        "revision": PI_REVISION,
        "platform": "darwin-arm64",
        "archive": {
            "url": PI_ARCHIVE_URL,
            "bytes": PI_ARCHIVE_BYTES,
            "sha256": PI_ARCHIVE_SHA256,
        },
        "extracted_bytes": PI_EXTRACTED_BYTES,
        "files_sha256": PI_FILE_HASHES,
        "source_url": PI_SOURCE_URL,
        "license_url": PI_LICENSE_URL,
        "embedded_runtime": {
            "name": "Bun",
            "version": PI_BUN_VERSION,
            "license_url": PI_BUN_LICENSE_URL,
        },
    }


def pi_distribution_plan(destination: Path) -> dict[str, Any]:
    """Describe installation without inspecting, downloading, or changing files."""
    return {
        **pi_distribution_manifest(),
        "destination": str(destination / "pi"),
        "executable": str(destination / "pi/pi"),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_archive(archive: Path) -> None:
    if archive.stat().st_size != PI_ARCHIVE_BYTES:
        raise PiDistributionError("Pi archive size does not match the pinned release")
    if _sha256(archive) != PI_ARCHIVE_SHA256:
        raise PiDistributionError("Pi archive SHA-256 does not match the pinned release")


def _download_archive(destination: Path) -> None:
    print(f"  Downloading Pi {PI_VERSION} from its official release")
    descriptor, filename = tempfile.mkstemp(prefix=".pi-download-", dir=destination.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as output:
            with urllib.request.urlopen(PI_ARCHIVE_URL, timeout=60) as response:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > PI_ARCHIVE_BYTES:
                        raise PiDistributionError("Pi download exceeds the pinned archive size")
                    output.write(chunk)
        _verify_archive(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _extract_archive(archive: Path, destination: Path) -> None:
    """Extract only ordinary files and directories under the upstream pi/ root."""
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        names: set[str] = set()
        total = 0
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or not path.parts
                or path.parts[0] != "pi"
                or ".." in path.parts
                or "\\" in member.name
                or not (member.isdir() or member.isfile())
                or member.name in names
            ):
                raise PiDistributionError(f"Pi archive contains an unsafe entry: {member.name}")
            names.add(member.name)
            total += member.size
        if total != PI_EXTRACTED_BYTES:
            raise PiDistributionError("Pi extracted size does not match the pinned release")

        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = bundle.extractfile(member)
            if source is None:
                raise PiDistributionError(f"could not extract Pi archive entry: {member.name}")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o700 if member.mode & 0o111 else 0o600)


def _verify_files(directory: Path) -> None:
    for name, expected in PI_FILE_HASHES.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or _sha256(path) != expected:
            raise PiDistributionError(f"Pi runtime file failed verification: {name}")
    package = json.loads((directory / "package.json").read_text())
    if package.get("version") != PI_VERSION:
        raise PiDistributionError("Pi package metadata has an unexpected version")
    if not os.access(directory / "pi", os.X_OK):
        raise PiDistributionError("Pi executable is not executable")


def _installed(directory: Path) -> bool:
    try:
        marker = json.loads((directory / PI_MARKER).read_text())
        if marker != pi_distribution_manifest():
            return False
        _verify_files(directory)
    except (OSError, ValueError, PiDistributionError):
        return False
    return True


def install_pi(destination: Path, *, archive: Path | None = None) -> Path:
    """Fetch a verified upstream Pi release, preserving the prior runtime on failure."""
    directory = destination / "pi"
    if destination.is_symlink() or directory.is_symlink():
        raise PiDistributionError("refusing to replace a symlinked Pi runtime directory")
    if _installed(directory):
        return directory / "pi"
    if directory.exists():
        try:
            previous = json.loads((directory / PI_MARKER).read_text())
        except (OSError, ValueError) as error:
            raise PiDistributionError(
                "refusing to replace an unmanaged Pi runtime directory"
            ) from error
        if not isinstance(previous, dict) or previous.get("name") != "Pi":
            raise PiDistributionError("refusing to replace an unmanaged Pi runtime directory")

    try:
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        if archive is None:
            cache = destination / "archives"
            cache.mkdir(exist_ok=True, mode=0o700)
            archive = cache / f"pi-{PI_VERSION}-darwin-arm64.tar.gz"
            if archive.is_file():
                try:
                    _verify_archive(archive)
                except PiDistributionError:
                    _download_archive(archive)
            else:
                _download_archive(archive)
        _verify_archive(archive)

        with tempfile.TemporaryDirectory(prefix=".pi-install-", dir=destination) as temporary:
            staged = Path(temporary)
            _extract_archive(archive, staged)
            _verify_files(staged / "pi")
            (staged / "pi" / PI_MARKER).write_text(
                json.dumps(pi_distribution_manifest(), indent=2, sort_keys=True) + "\n"
            )
            (staged / "pi" / PI_MARKER).chmod(0o600)
            previous_directory = staged.with_name(staged.name + ".previous")
            if directory.exists():
                os.replace(directory, previous_directory)
            try:
                os.replace(staged / "pi", directory)
            except OSError:
                if previous_directory.exists():
                    try:
                        os.replace(previous_directory, directory)
                    except OSError as error:
                        raise PiDistributionError(
                            "Pi installation failed and the prior runtime could not be restored; "
                            f"it is retained at {previous_directory}"
                        ) from error
                raise
            if previous_directory.exists():
                shutil.rmtree(previous_directory)
    except (OSError, ValueError, tarfile.TarError) as error:
        raise PiDistributionError(f"could not install Pi {PI_VERSION}: {error}") from error
    return directory / "pi"
