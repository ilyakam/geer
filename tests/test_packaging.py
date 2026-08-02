from __future__ import annotations

import plistlib
from pathlib import Path

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


def test_setup_app_uses_aligned_feature_rows_and_copy_logs_label() -> None:
    source = (
        ROOT
        / "packaging/macos/GeerSetup/Sources/GeerSetupApp/GeerSetupApp.swift"
    ).read_text()

    assert source.count("SetupFeatureRow(") == 5
    assert ".frame(width: 22, height: 22, alignment: .center)" in source
    assert 'Button("Copy Logs") { model.copyLogs() }' in source
    assert "Copy Diagnostics" not in source
