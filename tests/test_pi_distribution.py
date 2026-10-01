from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from urllib.error import URLError

import pytest

import geer.pi_distribution as distribution

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def pi_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    files = {
        "pi": b"#!/bin/sh\necho 0.99.1\n",
        "package.json": b'{"version":"0.99.1"}',
        "theme/dark.json": b'{"colors":{}}',
        "photon_rs_bg.wasm": b"synthetic wasm",
    }
    archive = tmp_path / "pi.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for name, data in files.items():
            member = tarfile.TarInfo("pi/" + name)
            member.size = len(data)
            member.mode = 0o755 if name == "pi" else 0o644
            bundle.addfile(member, io.BytesIO(data))
    monkeypatch.setattr(distribution, "PI_ARCHIVE_BYTES", archive.stat().st_size)
    monkeypatch.setattr(
        distribution, "PI_ARCHIVE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(distribution, "PI_EXTRACTED_BYTES", sum(map(len, files.values())))
    monkeypatch.setattr(
        distribution,
        "PI_FILE_HASHES",
        {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    )
    return archive


def test_distribution_pin_matches_packaged_provenance() -> None:
    assert json.loads((ROOT / "packaging/pi-runtime.json").read_text()) == (
        distribution.pi_distribution_manifest()
    )


def test_distribution_plan_never_reads_or_downloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        distribution.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("plan must not make a network request"),
    )
    destination = tmp_path / "missing"

    result = distribution.pi_distribution_plan(destination)

    assert result["archive"]["bytes"] == 31_069_587
    assert result["executable"] == str(destination / "pi/pi")
    assert not destination.exists()


def test_install_pi_verifies_archive_and_reuses_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pi_archive: Path
) -> None:
    destination = tmp_path / "private-pi"

    executable = distribution.install_pi(destination, archive=pi_archive)

    assert executable == destination / "pi/pi"
    assert os.access(executable, os.X_OK)
    assert json.loads((executable.parent / distribution.PI_MARKER).read_text()) == (
        distribution.pi_distribution_manifest()
    )
    pi_archive.unlink()
    monkeypatch.setattr(
        distribution.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("verified Pi runtime should be reused"),
    )
    assert distribution.install_pi(destination) == executable


@pytest.mark.parametrize("corruption", ["size", "hash"])
def test_install_pi_rejects_wrong_archive_without_replacing_prior_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pi_archive: Path, corruption: str
) -> None:
    destination = tmp_path / "private-pi"
    executable = distribution.install_pi(destination, archive=pi_archive)
    executable.write_bytes(b"previous working runtime")
    original = executable.read_bytes()
    if corruption == "size":
        monkeypatch.setattr(distribution, "PI_ARCHIVE_BYTES", pi_archive.stat().st_size + 1)
    else:
        monkeypatch.setattr(distribution, "PI_ARCHIVE_SHA256", "0" * 64)

    with pytest.raises(distribution.PiDistributionError, match="pinned release"):
        distribution.install_pi(destination, archive=pi_archive)

    assert executable.read_bytes() == original


@pytest.mark.parametrize("name", ["theme/dark.json", "photon_rs_bg.wasm"])
@pytest.mark.parametrize("corruption", ["missing", "hash"])
def test_bootstrap_repairs_managed_assets_even_when_pi_version_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pi_archive: Path,
    name: str,
    corruption: str,
) -> None:
    from geer.assets import Workspace
    from geer.runtime import bootstrap_pi, compatible_pi

    monkeypatch.delenv("GEER_PI_BIN", raising=False)
    workspace = Workspace(tmp_path / "source", tmp_path / "home")
    destination = workspace.runtime / "pi-runtime"
    executable = distribution.install_pi(destination, archive=pi_archive)
    asset = executable.parent / name
    original = asset.read_bytes()
    if corruption == "missing":
        asset.unlink()
    else:
        asset.write_bytes(b"?" * len(original))
    assert compatible_pi(workspace) == str(executable)
    prepared: list[Path] = []

    def prepare(directory: Path) -> Path:
        prepared.append(directory)
        return distribution.install_pi(directory, archive=pi_archive)

    monkeypatch.setattr("geer.runtime.install_pi", prepare)

    result = bootstrap_pi(workspace)

    assert result["pi"] == str(executable)
    assert prepared == [destination]
    assert asset.read_bytes() == original


@pytest.mark.parametrize("unsafe", ["parent", "symlink", "absolute"])
def test_install_pi_rejects_unsafe_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pi_archive: Path, unsafe: str
) -> None:
    malicious = tmp_path / "unsafe.tar.gz"
    with tarfile.open(malicious, "w:gz") as bundle:
        member = tarfile.TarInfo(
            {"parent": "pi/../../outside", "absolute": "/outside"}.get(unsafe, "pi/link")
        )
        if unsafe == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "../../outside"
        bundle.addfile(member)
    monkeypatch.setattr(distribution, "PI_ARCHIVE_BYTES", malicious.stat().st_size)
    monkeypatch.setattr(
        distribution, "PI_ARCHIVE_SHA256", hashlib.sha256(malicious.read_bytes()).hexdigest()
    )

    with pytest.raises(distribution.PiDistributionError, match="unsafe entry"):
        distribution.install_pi(tmp_path / "private-pi", archive=malicious)

    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "private-pi/pi").exists()


def test_install_pi_keeps_unmanaged_state(tmp_path: Path, pi_archive: Path) -> None:
    directory = tmp_path / "private-pi/pi"
    directory.mkdir(parents=True)
    user_file = directory / "user-owned"
    user_file.write_text("keep")

    with pytest.raises(distribution.PiDistributionError, match="unmanaged"):
        distribution.install_pi(directory.parent, archive=pi_archive)

    assert user_file.read_text() == "keep"


def test_install_pi_downloads_only_the_pinned_upstream_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pi_archive: Path
) -> None:
    requests: list[str] = []

    def fetch(url: str, **kwargs: object) -> io.BytesIO:
        requests.append(url)
        return io.BytesIO(pi_archive.read_bytes())

    monkeypatch.setattr(distribution.urllib.request, "urlopen", fetch)

    executable = distribution.install_pi(tmp_path / "private-pi")

    assert executable.is_file()
    assert requests == [distribution.PI_ARCHIVE_URL]
    assert not list((tmp_path / "private-pi/archives").glob(".pi-download-*"))


@pytest.mark.parametrize("failure", ["network", "hash"])
def test_failed_download_keeps_the_previous_runtime_and_cleans_partial_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pi_archive: Path, failure: str
) -> None:
    destination = tmp_path / "private-pi"
    executable = distribution.install_pi(destination, archive=pi_archive)
    original = executable.read_bytes()
    marker_path = executable.parent / distribution.PI_MARKER
    prior_marker = json.loads(marker_path.read_text())
    prior_marker["version"] = "0.98.0"
    marker_path.write_text(json.dumps(prior_marker))

    def fetch(*args: object, **kwargs: object) -> io.BytesIO:
        if failure == "network":
            raise URLError("simulated connection failure")
        return io.BytesIO(b"?" * pi_archive.stat().st_size)

    monkeypatch.setattr(distribution.urllib.request, "urlopen", fetch)

    with pytest.raises(distribution.PiDistributionError):
        distribution.install_pi(destination)

    assert executable.read_bytes() == original
    assert json.loads(marker_path.read_text()) == prior_marker
    assert not list((destination / "archives").iterdir())


def test_pi_download_keeps_setup_protocol_stdout_valid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pi_archive: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from geer.setup_protocol import run_json_setup

    monkeypatch.setattr(
        distribution.urllib.request,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(pi_archive.read_bytes()),
    )
    output = io.StringIO()

    run_json_setup(
        lambda frontend: {"pi": str(distribution.install_pi(tmp_path / "private-pi"))},
        output_stream=output,
    )

    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [record["event"] for record in records] == ["hello", "completed"]
    captured = capsys.readouterr()
    assert "Downloading Pi" in captured.err
    assert captured.out == ""


def test_install_pi_rolls_back_failed_promotion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pi_archive: Path
) -> None:
    destination = tmp_path / "private-pi"
    executable = distribution.install_pi(destination, archive=pi_archive)
    executable.write_bytes(b"prior runtime")
    replace = os.replace

    def fail_promotion(source: Path, target: Path) -> None:
        if source.name == "pi" and source.parent.name.startswith(".pi-install-"):
            raise OSError("simulated promotion failure")
        replace(source, target)

    monkeypatch.setattr(distribution.os, "replace", fail_promotion)

    with pytest.raises(distribution.PiDistributionError, match="promotion failure"):
        distribution.install_pi(destination, archive=pi_archive)

    assert executable.read_bytes() == b"prior runtime"


def test_install_pi_retains_backup_if_restoration_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pi_archive: Path
) -> None:
    destination = tmp_path / "private-pi"
    executable = distribution.install_pi(destination, archive=pi_archive)
    executable.write_bytes(b"prior runtime")
    replace = os.replace

    def fail_promotion_and_restore(source: Path, target: Path) -> None:
        if (source.parent.name.startswith(".pi-install-") and source.name == "pi") or (
            source.name.endswith(".previous")
        ):
            raise OSError("simulated promotion and restoration failure")
        replace(source, target)

    monkeypatch.setattr(distribution.os, "replace", fail_promotion_and_restore)

    with pytest.raises(distribution.PiDistributionError, match="retained at"):
        distribution.install_pi(destination, archive=pi_archive)

    saved = list(destination.glob(".pi-install-*.previous/pi"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"prior runtime"
