#!/usr/bin/env python3
"""Build the self-contained Apple Silicon Geer installer package."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tomllib
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "release"
IDENTIFIER = "com.ilyakam.geer"
PYINSTALLER_VERSION = "6.16.0"
APP_ROOT = Path("Library/Application Support/Geer/app")
RUNTIME_ROOT = APP_ROOT / "runtime/geer"
SETUP_APP = APP_ROOT / "Geer Setup.app"


class BuildError(RuntimeError):
    """Raised when a release artifact cannot be built safely."""


def run(command: list[str], *, cwd: Path = ROOT) -> None:
    print("+", " ".join(command))
    try:
        subprocess.run(
            command,
            cwd=cwd,
            check=True,
            env={**os.environ, "COPYFILE_DISABLE": "1"},
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise BuildError(f"command failed: {' '.join(command)}") from error


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def download(url: str, destination: Path, expected_sha256: str) -> None:
    print("+ download", url)
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url) as response, destination.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
    except OSError as error:
        raise BuildError(f"could not download {url}: {error}") from error
    if digest.hexdigest() != expected_sha256:
        raise BuildError(f"download hash did not match for {url}")


def copy_resources(payload: Path) -> None:
    destination = payload / APP_ROOT
    destination.mkdir(parents=True)
    for name in (
        "AGENTS.md",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "pyproject.toml",
        "uv.lock",
    ):
        shutil.copy(ROOT / name, destination / name)
    for name in (
        "model-cards",
        "model-distributions",
        "model-recipes",
        "tests/fixtures/retrieval-canary",
    ):
        source = ROOT / name
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, copy_function=shutil.copy)

    tools = destination / "tools"
    tools.mkdir()
    shutil.copy(ROOT / "tools/model_build.py", tools / "model_build.py")
    setup_command = destination / "geer-setup.command"
    shutil.copy(ROOT / "packaging/geer-setup.command", setup_command)
    setup_command.chmod(0o755)
    run(
        [
            "uv",
            "run",
            "--no-dev",
            "python",
            str(ROOT / "tools/collect_licenses.py"),
            str(destination / "third-party-licenses"),
        ]
    )


def build_setup_app(payload: Path, version: str, identity: str | None) -> None:
    source_root = ROOT / "packaging/macos/GeerSetup"
    app = payload / SETUP_APP
    contents = app / "Contents"
    executable = contents / "MacOS/GeerSetup"
    executable.parent.mkdir(parents=True)
    resources = contents / "Resources"
    resources.mkdir()
    run(
        [
            "xcrun",
            "swiftc",
            "-parse-as-library",
            "-swift-version",
            "5",
            "-target",
            "arm64-apple-macos13.0",
            "-o",
            str(executable),
            str(source_root / "Sources/GeerSetupApp/GeerSetupApp.swift"),
        ]
    )
    info = (source_root / "Info.plist").read_text().replace("__GEER_VERSION__", version)
    (contents / "Info.plist").write_text(info)
    shutil.copy(ROOT / "assets/logo.svg", resources / "Geer.svg")
    iconset = BUILD / "Geer.iconset"
    iconset.mkdir()
    icon_sizes = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }
    for name, size in icon_sizes.items():
        run(
            [
                "sips",
                "-z",
                str(size),
                str(size),
                "-s",
                "format",
                "png",
                str(ROOT / "assets/logo.svg"),
                "--out",
                str(iconset / name),
            ]
        )
    run(
        [
            "iconutil",
            "-c",
            "icns",
            str(iconset),
            "-o",
            str(resources / "Geer.icns"),
        ]
    )
    shutil.rmtree(iconset)
    if identity:
        run(
            [
                "codesign",
                "--force",
                "--options",
                "runtime",
                "--timestamp",
                "--sign",
                identity,
                str(app),
            ]
        )


def build_runtime(payload: Path, identity: str | None) -> None:
    pyinstaller = [
        "uv",
        "run",
        "--isolated",
        "--no-dev",
        "--with",
        f"pyinstaller=={PYINSTALLER_VERSION}",
        "--with-editable",
        str(ROOT),
        "pyinstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        "geer",
        "--collect-all",
        "semble",
        "--collect-data",
        "model2vec",
        "--collect-all",
        "tree_sitter_language_pack",
        "--hidden-import",
        "huggingface_hub",
        "--hidden-import",
        "hf_xet",
        "--distpath",
        str(BUILD / "pyinstaller-dist"),
        "--workpath",
        str(BUILD / "pyinstaller-work"),
        "--specpath",
        str(BUILD),
        str(ROOT / "packaging/entrypoint.py"),
    ]
    if identity:
        pyinstaller[-1:-1] = [
            "--codesign-identity",
            identity,
            "--osx-entitlements-file",
            str(ROOT / "packaging/entitlements.plist"),
        ]
    run(pyinstaller)
    source = BUILD / "pyinstaller-dist/geer"
    destination = payload / RUNTIME_ROOT
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    for direct_url in destination.rglob("direct_url.json"):
        direct_url.unlink()
    (destination / "semble").symlink_to("geer")


def build_payload(payload: Path, version: str, identity: str | None) -> None:
    copy_resources(payload)
    build_setup_app(payload, version, identity)
    build_runtime(payload, identity)
    cli_root = payload / "usr/local/bin"
    cli_root.mkdir(parents=True)
    for name in ("geer", "geer-claude"):
        cli = cli_root / name
        shutil.copy(ROOT / "packaging" / name, cli)
        cli.chmod(0o755)


def build_scripts(scripts: Path) -> None:
    scripts.mkdir(parents=True)
    postinstall = scripts / "postinstall"
    shutil.copy(ROOT / "packaging/postinstall", postinstall)
    postinstall.chmod(0o755)


def verify_payload(payload: Path) -> None:
    cli = payload / "usr/local/bin/geer"
    launcher = payload / "usr/local/bin/geer-claude"
    runtime = payload / RUNTIME_ROOT / "geer"
    semble = payload / RUNTIME_ROOT / "semble"
    setup_command = payload / APP_ROOT / "geer-setup.command"
    setup_app = payload / SETUP_APP
    setup_executable = setup_app / "Contents/MacOS/GeerSetup"
    setup_icon = setup_app / "Contents/Resources/Geer.icns"
    license_inventory = payload / APP_ROOT / "third-party-licenses/INVENTORY.md"
    for path in (cli, launcher, runtime, semble, setup_command, setup_executable):
        if not path.is_file() or not os.access(path, os.X_OK):
            raise BuildError(f"release payload is missing an executable: {path}")
    if not license_inventory.is_file() or "Python " not in license_inventory.read_text():
        raise BuildError("release payload is missing third-party license evidence")
    forbidden = {
        str(ROOT).encode(): "checkout path",
        str(Path.home()).encode(): "builder home",
    }
    leaks: list[str] = []
    for path in payload.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        for token, label in forbidden.items():
            if token in data:
                leaks.append(f"{path.relative_to(payload)}: {label}")
    if leaks:
        raise BuildError("release payload privacy scan failed:\n" + "\n".join(leaks))
    if setup_command.read_text() != ("#!/bin/sh\nset -eu\n\nexec /usr/local/bin/geer setup\n"):
        raise BuildError("packaged setup launcher does not run explicit setup")
    with (setup_app / "Contents/Info.plist").open("rb") as stream:
        app_info = plistlib.load(stream)
    if app_info.get("CFBundleIdentifier") != "com.ilyakam.geer.setup":
        raise BuildError("setup application has an unexpected bundle identifier")
    if app_info.get("CFBundleIconFile") != "Geer.icns" or not setup_icon.is_file():
        raise BuildError("setup application is missing its Geer icon")
    architecture = subprocess.run(
        ["lipo", "-archs", str(setup_executable)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if architecture != ["arm64"]:
        raise BuildError(f"setup application has unexpected architecture: {architecture}")
    result = subprocess.run(
        [str(runtime), "--help"],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GEER_WORKSPACE": str(payload / APP_ROOT),
            "GEER_HOME": str(BUILD / "smoke-home"),
            "GEER_CLI": str(cli),
        },
    )
    if "set up Geer on this Mac" not in result.stdout:
        raise BuildError("packaged CLI smoke test returned unexpected help")
    source_help = subprocess.run(
        ["uv", "run", "--no-sync", "geer", "--help"],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if result.stdout != source_help.stdout:
        raise BuildError("packaged CLI does not match the current worktree")
    result = subprocess.run(
        [str(semble), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    if "semble" not in result.stdout.lower():
        raise BuildError("packaged Semble smoke test returned unexpected help")


def package(
    payload: Path,
    scripts: Path,
    output: Path,
    version: str,
    identity: str | None,
) -> None:
    if shutil.which("xattr"):
        run(["xattr", "-cr", str(payload)])
    command = [
        "pkgbuild",
        "--root",
        str(payload),
        "--component-plist",
        str(ROOT / "packaging/components.plist"),
        "--scripts",
        str(scripts),
        "--identifier",
        IDENTIFIER,
        "--version",
        version,
        "--install-location",
        "/",
    ]
    if identity:
        command.extend(("--sign", identity))
    command.append(str(output))
    run(command)


def verify_package_metadata(output: Path) -> None:
    expanded = BUILD / "package-inspection"
    if expanded.exists():
        shutil.rmtree(expanded)
    run(["pkgutil", "--expand", str(output), str(expanded)])
    try:
        metadata = ET.parse(expanded / "PackageInfo").getroot()
        relocate = metadata.find("relocate")
        if relocate is None or list(relocate):
            raise BuildError("setup application bundle relocation is enabled")
        expected = "./Library/Application Support/Geer/app/Geer Setup.app"
        bundle = metadata.find(f"bundle[@path='{expected}']")
        if bundle is None or bundle.get("id") != "com.ilyakam.geer.setup":
            raise BuildError("setup application package metadata is missing")
    finally:
        shutil.rmtree(expanded)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--version", default=project_version())
    command.add_argument(
        "--application-sign",
        metavar="IDENTITY",
        help='Developer ID Application identity, such as "Developer ID Application: Name (TEAMID)"',
    )
    command.add_argument(
        "--installer-sign",
        "--sign",
        dest="installer_sign",
        metavar="IDENTITY",
        help='Developer ID Installer identity, such as "Developer ID Installer: Name (TEAMID)"',
    )
    command.add_argument(
        "--notary-profile",
        help="notarytool keychain profile; submits and staples the signed package",
    )
    return command


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise BuildError("release packages must be built on Apple Silicon macOS")
    if shutil.which("pkgbuild") is None:
        raise BuildError("pkgbuild is missing; install the Xcode command-line tools")

    if BUILD.exists():
        shutil.rmtree(BUILD)
    payload = BUILD / "payload"
    scripts = BUILD / "scripts"
    payload.mkdir(parents=True)
    if options.notary_profile and (not options.application_sign or not options.installer_sign):
        raise BuildError(
            "notarization requires both Developer ID Application and Installer identities"
        )
    build_payload(payload, options.version, options.application_sign)
    build_scripts(scripts)
    verify_payload(payload)

    output = ROOT / "dist" / f"Geer-{options.version}-macOS-arm64.pkg"
    output.parent.mkdir(exist_ok=True)
    output.unlink(missing_ok=True)
    package(payload, scripts, output, options.version, options.installer_sign)
    verify_package_metadata(output)
    if options.notary_profile:
        run(
            [
                "xcrun",
                "notarytool",
                "submit",
                str(output),
                "--keychain-profile",
                options.notary_profile,
                "--wait",
            ]
        )
        run(["xcrun", "stapler", "staple", str(output)])
        run(["spctl", "--assess", "--type", "install", "--verbose=2", str(output)])
    print(f"\nBuilt {output}")
    if options.installer_sign is None:
        print("The package is unsigned. Sign and notarize it before publishing.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
