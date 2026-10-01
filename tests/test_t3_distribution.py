from __future__ import annotations

import hashlib
import io
import json
import os
import plistlib
import stat
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from urllib.error import URLError

import pytest

import geer.t3_distribution as distribution

ROOT = Path(__file__).resolve().parents[1]


def _pin_archive(monkeypatch: pytest.MonkeyPatch, archive: Path) -> None:
    monkeypatch.setattr(distribution, "T3_ARCHIVE_BYTES", archive.stat().st_size)
    monkeypatch.setattr(
        distribution, "T3_ARCHIVE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest()
    )


@pytest.fixture
def app_commands(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    commands: list[list[str]] = []
    native_run = subprocess.run

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert kwargs["check"] is True
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        if command[0] == "/usr/bin/ditto":
            assert command[1:3] == ["-x", "-k"]
            if sys.platform == "darwin":
                return native_run(command, **kwargs)
            with zipfile.ZipFile(command[3]) as archive:
                for entry in archive.infolist():
                    target = Path(command[4]).joinpath(*PurePosixPath(entry.filename).parts)
                    mode = entry.external_attr >> 16
                    if entry.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if stat.S_ISLNK(mode):
                            target.symlink_to(archive.read(entry).decode())
                        else:
                            target.write_bytes(archive.read(entry))
                            target.chmod(stat.S_IMODE(mode))
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[0] == "/usr/bin/lipo":
            assert command[1] == "-archs"
            return subprocess.CompletedProcess(command, 0, "arm64\n", "")
        assert command[0] == "/usr/bin/codesign"
        assert command[1:4] == ["--verify", "--deep", "--strict"]
        assert command[4] == f"-R={distribution.T3_SIGNATURE_REQUIREMENT}"
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(distribution.subprocess, "run", run)
    if sys.platform != "darwin":

        def promote(staged: Path, destination: Path) -> None:
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(f"destination already exists: {destination}")
            os.rename(staged, destination)

        monkeypatch.setattr(distribution, "_promote", promote)
    return commands


@pytest.fixture
def t3_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_commands: list[list[str]]
) -> Path:
    metadata = {
        "CFBundleIdentifier": distribution.T3_BUNDLE_ID,
        "CFBundleShortVersionString": distribution.T3_VERSION,
        "CFBundleVersion": distribution.T3_VERSION,
        "CFBundleExecutable": distribution.T3_EXECUTABLE,
    }
    files = {
        "Contents/Info.plist": plistlib.dumps(metadata),
        f"Contents/MacOS/{distribution.T3_EXECUTABLE}": b"synthetic arm64 executable",
        "Contents/Resources/LICENSE": b"Synthetic upstream notice. Preserve this file.\n",
        "Contents/Frameworks/Example.framework/Versions/A/Resources/notice": b"notice",
    }
    symlinks = {
        "Contents/Frameworks/Example.framework/Versions/Current": b"A",
        "Contents/Frameworks/Example.framework/Resources": b"Versions/Current/Resources",
    }
    archive = tmp_path / "t3.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, data in files.items():
            entry = zipfile.ZipInfo(f"{distribution.T3_BUNDLE_NAME}/{name}")
            mode = 0o755 if name.startswith("Contents/MacOS/") else 0o644
            entry.external_attr = (stat.S_IFREG | mode) << 16
            bundle.writestr(entry, data)
        for name, target in symlinks.items():
            entry = zipfile.ZipInfo(f"{distribution.T3_BUNDLE_NAME}/{name}")
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            bundle.writestr(entry, target)
    _pin_archive(monkeypatch, archive)
    monkeypatch.setattr(distribution, "T3_INSTALLED_BYTES", sum(map(len, files.values())))
    return archive


def test_distribution_pin_matches_packaged_provenance() -> None:
    assert json.loads((ROOT / "packaging/t3-desktop.json").read_text()) == (
        distribution.t3_distribution_manifest()
    )


def test_plan_does_not_inspect_download_or_modify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("the installation plan must not inspect or download an app")

    monkeypatch.setattr(distribution, "_verify_bundle", unexpected)
    monkeypatch.setattr(distribution.urllib.request, "urlopen", unexpected)
    destination = tmp_path / "Applications/T3 Code.app"

    plan = distribution.t3_distribution_plan(destination)

    assert plan["archive"]["bytes"] == 137_613_348
    assert plan["installed_bytes"] == 334_315_639
    assert plan["destination"] == str(destination)
    assert plan["executable"] == str(destination / "Contents/MacOS/T3 Code (Alpha)")
    assert not destination.parent.exists()


def test_install_preserves_upstream_notices_and_framework_links(
    tmp_path: Path, t3_archive: Path, app_commands: list[list[str]]
) -> None:
    destination = tmp_path / "Applications/T3 Code.app"

    result = distribution.install_t3(destination, cache=tmp_path / "cache", archive=t3_archive)

    assert result == destination
    assert (result / "Contents/Resources/LICENSE").read_bytes() == (
        b"Synthetic upstream notice. Preserve this file.\n"
    )
    framework = result / "Contents/Frameworks/Example.framework"
    assert (framework / "Versions/Current").is_symlink()
    assert (framework / "Resources").is_symlink()
    assert (framework / "Resources/notice").read_bytes() == b"notice"
    assert [command[0] for command in app_commands] == [
        "/usr/bin/ditto",
        "/usr/bin/lipo",
        "/usr/bin/codesign",
    ]
    assert not (tmp_path / "cache").exists()
    assert not list(destination.parent.glob(".geer-t3-install-*"))


def test_reuses_verified_app_without_download_or_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, t3_archive: Path
) -> None:
    destination = tmp_path / "T3 Code.app"
    distribution.install_t3(destination, cache=tmp_path / "cache", archive=t3_archive)
    notice = (destination / "Contents/Resources/LICENSE").read_bytes()
    t3_archive.unlink()
    monkeypatch.setattr(
        distribution.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("a verified app must not be downloaded again"),
    )

    assert distribution.install_t3(destination, cache=tmp_path / "cache") == destination
    assert (destination / "Contents/Resources/LICENSE").read_bytes() == notice
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("corruption", ["size", "hash"])
def test_rejects_corrupt_input_archive_before_creating_an_app(
    tmp_path: Path, t3_archive: Path, app_commands: list[list[str]], corruption: str
) -> None:
    data = t3_archive.read_bytes()
    t3_archive.write_bytes(data + b"?" if corruption == "size" else b"?" + data[1:])
    destination = tmp_path / "Applications/T3 Code.app"

    with pytest.raises(distribution.T3DistributionError, match="pinned release"):
        distribution.install_t3(destination, cache=tmp_path / "cache", archive=t3_archive)

    assert not destination.parent.exists()
    assert not app_commands


@pytest.mark.parametrize("existing", ["directory", "file", "symlink"])
def test_retains_unmanaged_destinations_without_downloading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, t3_archive: Path, existing: str
) -> None:
    destination = tmp_path / "T3 Code.app"
    user_file = tmp_path / "user-file"
    user_file.write_bytes(b"retain user state")
    if existing == "directory":
        destination.mkdir()
        user_file = destination / "user-file"
        user_file.write_bytes(b"retain user state")
    elif existing == "file":
        destination.write_bytes(b"retain user state")
        user_file = destination
    else:
        destination.symlink_to(user_file)
    monkeypatch.setattr(
        distribution.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("an existing destination must not cause a download"),
    )

    with pytest.raises(distribution.T3DistributionError, match="refusing"):
        distribution.install_t3(destination, cache=tmp_path / "cache")

    assert user_file.read_bytes() == b"retain user state"
    assert destination.is_symlink() == (existing == "symlink")
    assert not (tmp_path / "cache").exists()


def test_rejects_symlinked_installation_parent(tmp_path: Path, t3_archive: Path) -> None:
    target = tmp_path / "user-owned"
    target.mkdir()
    parent = tmp_path / "Applications"
    parent.symlink_to(target, target_is_directory=True)

    with pytest.raises(distribution.T3DistributionError, match="symlinked destination"):
        distribution.install_t3(
            parent / "T3 Code.app", cache=tmp_path / "cache", archive=t3_archive
        )

    assert not list(target.iterdir())


@pytest.mark.parametrize(
    "metadata_key",
    ["CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion", "CFBundleExecutable"],
)
def test_refuses_to_replace_an_existing_app_with_wrong_metadata(
    tmp_path: Path, t3_archive: Path, metadata_key: str
) -> None:
    destination = tmp_path / "T3 Code.app"
    distribution.install_t3(destination, cache=tmp_path / "cache", archive=t3_archive)
    plist = destination / "Contents/Info.plist"
    metadata = plistlib.loads(plist.read_bytes())
    metadata[metadata_key] = "user's existing value"
    original = plistlib.dumps(metadata)
    plist.write_bytes(original)

    with pytest.raises(distribution.T3DistributionError, match="refusing to replace"):
        distribution.install_t3(destination, cache=tmp_path / "cache", archive=t3_archive)

    assert plist.read_bytes() == original
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("cache_state", ["absent", "valid", "corrupt"])
def test_downloads_only_the_pin_and_reuses_or_repairs_the_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, t3_archive: Path, cache_state: str
) -> None:
    cache = tmp_path / "cache"
    cached = cache / f"T3-Code-{distribution.T3_VERSION}-arm64.zip"
    if cache_state != "absent":
        cache.mkdir()
        cached.write_bytes(t3_archive.read_bytes() if cache_state == "valid" else b"old cache")
    requests: list[str] = []

    def download(url: str, *, timeout: int) -> io.BytesIO:
        requests.append(url)
        assert timeout == 60
        return io.BytesIO(t3_archive.read_bytes())

    monkeypatch.setattr(distribution.urllib.request, "urlopen", download)

    result = distribution.install_t3(tmp_path / "T3 Code.app", cache=cache)

    assert result.is_dir()
    assert requests == ([] if cache_state == "valid" else [distribution.T3_ARCHIVE_URL])
    assert cached.read_bytes() == t3_archive.read_bytes()
    assert not list(cache.glob(".t3-download-*"))


@pytest.mark.parametrize("failure", ["network", "short", "hash", "long"])
def test_failed_download_retains_the_prior_cache_and_cleans_partial_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, t3_archive: Path, failure: str
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    cached = cache / f"T3-Code-{distribution.T3_VERSION}-arm64.zip"
    cached.write_bytes(b"retain the existing cache until replacement is verified")
    original = cached.read_bytes()

    def download(*args: object, **kwargs: object) -> io.BytesIO:
        if failure == "network":
            raise URLError("simulated connection failure")
        data = t3_archive.read_bytes()
        return io.BytesIO(
            {"short": data[:-1], "hash": b"?" * len(data), "long": data + b"?"}[failure]
        )

    monkeypatch.setattr(distribution.urllib.request, "urlopen", download)
    destination = tmp_path / "Applications/T3 Code.app"

    with pytest.raises(distribution.T3DistributionError):
        distribution.install_t3(destination, cache=cache)

    assert cached.read_bytes() == original
    assert list(cache.iterdir()) == [cached]
    assert not destination.parent.exists()


def test_download_diagnostics_do_not_write_protocol_stdout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    t3_archive: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        distribution.urllib.request,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(t3_archive.read_bytes()),
    )

    distribution.install_t3(tmp_path / "T3 Code.app", cache=tmp_path / "cache")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Downloading T3 Code" in captured.err


@pytest.mark.parametrize("unsafe", ["parent", "absolute", "root", "special", "duplicate"])
def test_rejects_unsafe_zip_entries_before_extraction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    t3_archive: Path,
    app_commands: list[list[str]],
    unsafe: str,
) -> None:
    name = {
        "parent": f"{distribution.T3_BUNDLE_NAME}/../../outside",
        "absolute": "/outside",
        "root": "Other.app/Contents/user-file",
    }.get(unsafe, f"{distribution.T3_BUNDLE_NAME}/Contents/user-file")
    malformed = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(malformed, "w") as bundle:
        entry = zipfile.ZipInfo(name)
        entry.external_attr = (stat.S_IFIFO if unsafe == "special" else stat.S_IFREG) << 16
        bundle.writestr(entry, b"unsafe")
        if unsafe == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                bundle.writestr(entry, b"duplicate")
    _pin_archive(monkeypatch, malformed)

    with pytest.raises(distribution.T3DistributionError, match="unsafe entry"):
        distribution.install_t3(
            tmp_path / "T3 Code.app", cache=tmp_path / "cache", archive=malformed
        )

    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "T3 Code.app").exists()
    assert not list(tmp_path.glob(".geer-t3-install-*"))
    assert not app_commands


@pytest.mark.parametrize("target", ["/outside", "../outside", "..", "", "A\\outside"])
def test_rejects_unsafe_framework_symlinks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, t3_archive: Path, target: str
) -> None:
    malformed = tmp_path / "unsafe-link.zip"
    with zipfile.ZipFile(malformed, "w") as bundle:
        entry = zipfile.ZipInfo(f"{distribution.T3_BUNDLE_NAME}/link")
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(entry, target)
    _pin_archive(monkeypatch, malformed)

    with pytest.raises(distribution.T3DistributionError, match="unsafe symlink"):
        distribution.install_t3(
            tmp_path / "T3 Code.app", cache=tmp_path / "cache", archive=malformed
        )

    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "T3 Code.app").exists()


def test_rejects_zip_entries_under_a_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, t3_archive: Path
) -> None:
    malformed = tmp_path / "link-child.zip"
    with zipfile.ZipFile(malformed, "w") as bundle:
        entry = zipfile.ZipInfo(f"{distribution.T3_BUNDLE_NAME}/link")
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(entry, "directory")
        bundle.writestr(f"{distribution.T3_BUNDLE_NAME}/link/file", b"unsafe")
    _pin_archive(monkeypatch, malformed)

    with pytest.raises(distribution.T3DistributionError, match="below a symlink"):
        distribution.install_t3(
            tmp_path / "T3 Code.app", cache=tmp_path / "cache", archive=malformed
        )

    assert not (tmp_path / "T3 Code.app").exists()


def test_rejects_a_malformed_zip_with_an_actionable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, t3_archive: Path
) -> None:
    malformed = tmp_path / "malformed.zip"
    malformed.write_bytes(b"not a zip")
    _pin_archive(monkeypatch, malformed)

    with pytest.raises(distribution.T3DistributionError, match="could not install T3 Code"):
        distribution.install_t3(
            tmp_path / "T3 Code.app", cache=tmp_path / "cache", archive=malformed
        )

    assert not (tmp_path / "T3 Code.app").exists()
    assert not list(tmp_path.glob(".geer-t3-install-*"))


@pytest.mark.parametrize("architecture", ["x86_64", "arm64 x86_64", ""])
def test_rejects_an_app_that_is_not_the_pinned_arm64_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, t3_archive: Path, architecture: str
) -> None:
    run = distribution.subprocess.run

    def wrong_architecture(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[0] == "/usr/bin/lipo":
            return subprocess.CompletedProcess(command, 0, architecture, "")
        return run(command, **kwargs)

    monkeypatch.setattr(distribution.subprocess, "run", wrong_architecture)

    with pytest.raises(distribution.T3DistributionError, match="pinned arm64 build"):
        distribution.install_t3(
            tmp_path / "T3 Code.app", cache=tmp_path / "cache", archive=t3_archive
        )

    assert not (tmp_path / "T3 Code.app").exists()
    assert not list(tmp_path.glob(".geer-t3-install-*"))


@pytest.mark.parametrize("failure", ["extract", "signature", "timeout", "missing-tool"])
def test_failed_macos_checks_leave_no_partially_installed_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, t3_archive: Path, failure: str
) -> None:
    run = distribution.subprocess.run

    def fail_command(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        selected = "/usr/bin/ditto" if failure == "extract" else "/usr/bin/codesign"
        if command[0] == selected:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, 120)
            if failure == "missing-tool":
                raise FileNotFoundError("macOS codesign is unavailable")
            raise subprocess.CalledProcessError(1, command, stderr="simulated verification failure")
        return run(command, **kwargs)

    monkeypatch.setattr(distribution.subprocess, "run", fail_command)

    with pytest.raises(distribution.T3DistributionError, match="T3 Code"):
        distribution.install_t3(
            tmp_path / "T3 Code.app", cache=tmp_path / "cache", archive=t3_archive
        )

    assert not (tmp_path / "T3 Code.app").exists()
    assert not list(tmp_path.glob(".geer-t3-install-*"))


def test_failed_promotion_rolls_back_to_an_absent_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, t3_archive: Path
) -> None:
    def fail_promotion(staged: Path, destination: Path) -> None:
        raise OSError("simulated promotion failure")

    monkeypatch.setattr(distribution, "_promote", fail_promotion)

    with pytest.raises(distribution.T3DistributionError, match="promotion failure"):
        distribution.install_t3(
            tmp_path / "T3 Code.app", cache=tmp_path / "cache", archive=t3_archive
        )

    assert not (tmp_path / "T3 Code.app").exists()
    assert not list(tmp_path.glob(".geer-t3-install-*"))


@pytest.mark.parametrize("concurrent", ["empty-directory", "user-app", "symlink"])
def test_promotion_never_overwrites_a_concurrently_created_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, t3_archive: Path, concurrent: str
) -> None:
    promote = distribution._promote
    destination = tmp_path / "T3 Code.app"
    user_owned = tmp_path / "user-owned"
    user_owned.mkdir()
    (user_owned / "state").write_text("retain")

    def concurrent_promotion(staged: Path, target: Path) -> None:
        if concurrent == "symlink":
            target.symlink_to(user_owned, target_is_directory=True)
        else:
            target.mkdir()
            if concurrent == "user-app":
                (target / "state").write_text("retain")
        promote(staged, target)

    monkeypatch.setattr(distribution, "_promote", concurrent_promotion)

    with pytest.raises(distribution.T3DistributionError, match="already exists|File exists"):
        distribution.install_t3(destination, cache=tmp_path / "cache", archive=t3_archive)

    assert (user_owned / "state").read_text() == "retain"
    if concurrent == "empty-directory":
        assert list(destination.iterdir()) == []
    else:
        assert (destination / "state").read_text() == "retain"
    assert destination.is_symlink() == (concurrent == "symlink")
    assert not list(tmp_path.glob(".geer-t3-install-*"))
