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

PI_VERSION = "0.99.1"
PI_REVISION = "d86654abb8862e201933517d6f1fce9f88dd117f"
PI_ARCHIVE_URL = (
    f"https://github.com/earendil-works/pi/releases/download/v{PI_VERSION}/"
    "pi-darwin-arm64.tar.gz"
)
PI_ARCHIVE_BYTES = 31_069_587
PI_ARCHIVE_SHA256 = "4692aba1dcd48219b61edb4ecebc3c33f6199ebe65299228fce5ba69c31a23a6"
PI_EXTRACTED_BYTES = 84_421_159
PI_SOURCE_URL = (
    f"https://github.com/earendil-works/pi/releases/download/v{PI_VERSION}/"
    f"pi-{PI_VERSION}-source.tar.gz"
)
PI_LICENSE_URL = f"https://github.com/earendil-works/pi/blob/{PI_REVISION}/LICENSE"
PI_BUN_VERSION = "1.3.14"
PI_BUN_LICENSE_URL = f"https://github.com/oven-sh/bun/blob/bun-v{PI_BUN_VERSION}/LICENSE.md"
PI_FILE_HASHES = {
    "pi": "aceda3ee52b624a3c695de494b2bd7a24bf59b5a2e0ac5839e7eb80344636a23",
    "package.json": "c701ddbf28b5170deb478b8586ee713f9372027c04736946a4ffe82168ece5ba",
    "photon_rs_bg.wasm": "10468181565c56004c867f3a4af96f89a0ef5a63a72f2b5fb12c1f1992a3615c",
    "native/darwin/prebuilds/darwin-arm64/darwin-platform.node": (
        "36e909adf35c2734a2a52b3d65b7dbfb83f2278f306d51b7e32c5045516dfb7f"
    ),
    "theme/dark.json": "c11a588b714d35300293079b425fb09a3693d4b2d453585d211b08163c648b75",
    "theme/light.json": "f590c51c2bc8b238891efd0e983473ed2e2a6487b6692cc1d4b2845165e703f7",
    "theme/theme-schema.json": (
        "8787918132dbf7aaee1be2ab30ee6306c715dad609cd85e448df595bb02e2a56"
    ),
    "assets/clankolas.png": (
        "169acd0dfe6fbb8d8742ed24a3fc654fd0b2e2d4223c733249c5493723f1b72d"
    ),
    "export-html/template.html": (
        "916782b1184a9597527605ad751e2b3af30fcea23ba2194002969cd217a06881"
    ),
    "export-html/template.css": (
        "8ee19851f8e583277ed396cbb76496687aa556707fdfc1f78d1118bff87c740a"
    ),
    "export-html/template.js": (
        "b5bbffdf5d9ec8bb519df45c7ff953ac1969af80e8aba33331b9f87983f91fa5"
    ),
    "export-html/vendor/marked.min.js": (
        "d5487edc7258b404bfa74c393d74a6393155f02517bd5e7e77cd64f8187f39a0"
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
