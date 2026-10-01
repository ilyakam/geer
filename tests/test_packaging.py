from __future__ import annotations

import importlib.util
import json
import plistlib
import shlex
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from geer.pi_distribution import pi_distribution_manifest
from geer.t3_distribution import t3_distribution_manifest

ROOT = Path(__file__).resolve().parents[1]


def test_setup_app_cannot_be_relocated_from_install_path() -> None:
    with (ROOT / "packaging/components.plist").open("rb") as stream:
        components = plistlib.load(stream)

    assert components == [
        {
            "BundleHasStrictIdentifier": True,
            "BundleIsRelocatable": False,
            "BundleIsVersionChecked": True,
            "BundleOverwriteAction": "upgrade",
            "RootRelativeBundlePath": (
                "Library/Application Support/Geer/app/Geer Setup.app"
            ),
        }
    ]


def test_postinstall_keeps_terminal_fallback_after_graphical_app() -> None:
    script = (ROOT / "packaging/postinstall").read_text()

    assert script.index('setup_app="') < script.index('setup_command="')
    assert script.count('/usr/bin/open "$setup_app"') == 1
    assert script.count('/usr/bin/open "$setup_command"') == 1


def test_postinstall_retains_legacy_launcher_and_user_state_on_reinstall(tmp_path: Path) -> None:
    volume = tmp_path / "Target Volume"
    legacy = volume / "usr/local/bin/geer-claude"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("old Geer launcher\n")
    retained = {
        "usr/local/bin/geer": b"Geer CLI\n",
        "usr/local/bin/geer-pi": b"Pi launcher\n",
        "usr/local/bin/other-tool": b"independent tool\n",
        "Users/test/.geer/state/setup.json": b'{"status":"complete"}\n',
        "Users/test/.geer/models/active/model.safetensors": b"retained model\n",
        "Users/test/.local/bin/claude": b"independent Claude\n",
    }
    for relative, content in retained.items():
        path = volume / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    stat = tmp_path / "stat"
    stat.write_text("#!/bin/sh\nprintf '%s\\n' root\n")
    stat.chmod(0o755)
    gui_marker = tmp_path / "unexpected-gui"
    launchctl = tmp_path / "launchctl"
    launchctl.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(gui_marker))}\nexit 99\n")
    launchctl.chmod(0o755)
    script = (ROOT / "packaging/postinstall").read_text()
    script = script.replace("/usr/bin/stat", shlex.quote(str(stat)))
    script = script.replace("/bin/launchctl", shlex.quote(str(launchctl)))
    postinstall = tmp_path / "postinstall"
    postinstall.write_text(script)

    for _ in range(2):
        result = subprocess.run(
            ["/bin/sh", str(postinstall), "Geer.pkg", "/", f"{volume}/"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert legacy.read_text() == "old Geer launcher\n"
        assert not gui_marker.exists()
        assert not result.stdout and not result.stderr
        for relative, content in retained.items():
            assert (volume / relative).read_bytes() == content


def test_setup_app_uses_aligned_feature_rows_and_copy_logs_label() -> None:
    source = (
        ROOT
        / "packaging/macos/GeerSetup/Sources/GeerSetupApp/GeerSetupApp.swift"
    ).read_text()

    assert source.count("SetupFeatureRow(") == 5
    assert ".frame(width: 22, height: 22, alignment: .center)" in source
    assert 'Button("Copy Logs") { model.copyLogs() }' in source
    assert "Copy Diagnostics" not in source


def test_packaged_launcher_uses_geer_pi() -> None:
    cli = (ROOT / "packaging/geer").read_text()
    launcher = (ROOT / "packaging/geer-pi").read_text()

    assert 'export GEER_PI_LAUNCHER="/usr/local/bin/geer-pi"' in cli
    assert "GEER_CLAUDE_LAUNCHER" not in cli
    assert "exec /usr/local/bin/geer pi --version" in launcher
    assert 'exec /usr/local/bin/geer launch "$@"' in launcher


def test_setup_app_explains_automatic_pi_and_t3_installation() -> None:
    source = (
        ROOT / "packaging/macos/GeerSetup/Sources/GeerSetupApp/GeerSetupApp.swift"
    ).read_text()

    assert (
        "Pi and T3 Code are installed and verified automatically; existing installations are reused"
        in source
    )
    assert "Claude Code" not in source


@pytest.fixture
def release_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "geer_release_builder", ROOT / "tools/build_release.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pinned_payload(tmp_path: Path, release_builder: ModuleType) -> Path:
    payload = tmp_path / "payload"
    app = payload / release_builder.APP_ROOT
    for path in (
        payload / "usr/local/bin/geer",
        payload / "usr/local/bin/geer-pi",
        payload / release_builder.RUNTIME_ROOT / "geer",
        payload / release_builder.RUNTIME_ROOT / "semble",
        app / "geer-setup.command",
        payload / release_builder.SETUP_APP / "Contents/MacOS/GeerSetup",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic executable\n")
        path.chmod(0o755)
    inventory = app / "third-party-licenses/INVENTORY.md"
    inventory.parent.mkdir()
    inventory.write_text("Python 3.13.7\n")
    for name, manifest in (
        ("pi-runtime.json", pi_distribution_manifest()),
        ("t3-desktop.json", t3_distribution_manifest()),
    ):
        (app / name).write_text(json.dumps(manifest))
    return payload


def test_release_resources_copy_dependency_pins_without_t3_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, release_builder: ModuleType
) -> None:
    monkeypatch.setattr(release_builder, "run", lambda *args, **kwargs: None)
    payload = tmp_path / "payload"

    release_builder.copy_resources(payload)

    resources = payload / release_builder.APP_ROOT
    assert json.loads((resources / "t3-desktop.json").read_text()) == t3_distribution_manifest()
    assert json.loads((resources / "pi-runtime.json").read_text()) == pi_distribution_manifest()
    assert not list(payload.rglob("T3*.app"))
    assert not list(payload.rglob("T3-Code-*.zip"))


@pytest.mark.parametrize("name", ["pi-runtime.json", "t3-desktop.json"])
@pytest.mark.parametrize("failure", ["missing", "version", "extra-field"])
def test_release_verification_rejects_missing_or_stale_dependency_pin(
    release_builder: ModuleType, pinned_payload: Path, name: str, failure: str
) -> None:
    path = pinned_payload / release_builder.APP_ROOT / name
    if failure == "missing":
        path.unlink()
    else:
        data = json.loads(path.read_text())
        data["version" if failure == "version" else "unexpected"] = "stale"
        path.write_text(json.dumps(data))

    with pytest.raises(release_builder.BuildError, match="pinned upstream .* manifest"):
        release_builder.verify_payload(pinned_payload)


@pytest.mark.parametrize(
    "binary", ["T3 Code.app", "T3 Code (Alpha).app", "T3-Code-0.0.44-arm64.zip"]
)
def test_release_verification_rejects_bundled_t3(
    release_builder: ModuleType, pinned_payload: Path, binary: str
) -> None:
    path = pinned_payload / "Applications" / binary
    path.parent.mkdir()
    if binary.endswith(".app"):
        path.mkdir()
    else:
        path.write_bytes(b"synthetic archive")

    with pytest.raises(
        release_builder.BuildError, match="T3 Code must be downloaded from upstream"
    ):
        release_builder.verify_payload(pinned_payload)
