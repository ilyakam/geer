#!/usr/bin/env python3
"""Build Geer's relocatable, pre-resolved oMLX runtime archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

from geer.runtime import OMLX_OVERRIDES, OMLX_SPEC, OMLX_VERSION

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_NAME = f"Geer-runtime-omlx-{OMLX_VERSION}-macOS-arm64.tar.gz"
PYTHON_VERSION = "3.13.7"
RUNTIME_PREFIX = "__GEER_RUNTIME_PYTHON_PREFIX__"
BUILD_ROOT = "__GEER_BUILD_ROOT__"
BUILD_HOME = "__GEER_BUILD_HOME__"
BUILD_TEMP = "__GEER_BUILD_TEMP__"
DETERMINISTIC_BUILD_ROOT = Path("/private/tmp/geer-runtime-build")


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def _normalized(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


def create_archive(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for path in sorted(source.rglob("*")):
                    archive.add(
                        path,
                        arcname=path.relative_to(source),
                        recursive=False,
                        filter=_normalized,
                    )


def _remove_runtime_build_artifacts(stage: Path) -> None:
    for cache in sorted(stage.rglob("__pycache__"), reverse=True):
        shutil.rmtree(cache)
    for bytecode in stage.rglob("*.pyc"):
        bytecode.unlink()
    for path in (
        stage / "python/include",
        stage / "python/share",
        stage / "python/lib/python3.13/config-3.13-darwin",
    ):
        if path.exists():
            shutil.rmtree(path)
    for activation in (stage / "omlx/bin").glob("activate*"):
        activation.unlink()


def _normalize_text_paths(stage: Path, *, python_home: Path, temporary: Path) -> None:
    user_home = re.compile(r"/Users/[^/\s'\"]+")
    build_temp = re.compile(
        r"/(?:private/)?var/folders/[^/\s'\"]+/[^/\s'\"]+/T/[^/\s'\"]+"
    )
    replacements = (
        (str(python_home), RUNTIME_PREFIX),
        (str(temporary), BUILD_ROOT),
    )
    for path in stage.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        original = text
        for source, replacement in replacements:
            text = text.replace(source, replacement)
        text = user_home.sub(BUILD_HOME, text)
        text = build_temp.sub(BUILD_TEMP, text)
        if text != original:
            path.write_text(text)


def _privacy_scan(stage: Path) -> None:
    forbidden = {
        str(ROOT).encode(): "checkout path",
        str(Path.home()).encode(): "builder home",
    }
    failures: list[str] = []
    for path in stage.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        for token, label in forbidden.items():
            if token in data:
                failures.append(f"{path.relative_to(stage)}: {label}")
    if failures:
        raise RuntimeError("runtime privacy scan failed:\n" + "\n".join(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / ARCHIVE_NAME,
    )
    options = parser.parse_args()
    uv = shutil.which("uv")
    if uv is None:
        parser.error("uv is required")

    if DETERMINISTIC_BUILD_ROOT.exists():
        shutil.rmtree(DETERMINISTIC_BUILD_ROOT)
    DETERMINISTIC_BUILD_ROOT.mkdir(parents=True)
    temporary = DETERMINISTIC_BUILD_ROOT
    try:
        stage = temporary / "runtime"
        python_install = temporary / "python-install"
        run(
            [
                uv,
                "python",
                "install",
                PYTHON_VERSION,
                "--install-dir",
                str(python_install),
                "--no-bin",
            ]
        )
        python_sources = tuple(python_install.glob("*/bin/python3.13"))
        if len(python_sources) != 1:
            raise RuntimeError(f"expected one managed Python, found {len(python_sources)}")
        python_home = python_sources[0].parent.parent.resolve()
        print(f"+ copy {python_home}", flush=True)
        shutil.copytree(python_home, stage / "python", symlinks=True)

        environment = stage / "omlx"
        run(
            [
                uv,
                "venv",
                "--relocatable",
                "--python",
                str(stage / "python/bin/python3.13"),
                str(environment),
            ]
        )
        overrides = Path(temporary) / "overrides.txt"
        overrides.write_text("\n".join(OMLX_OVERRIDES) + "\n")
        run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(environment / "bin/python"),
                "--overrides",
                str(overrides),
                OMLX_SPEC,
            ]
        )

        python = environment / "bin/python"
        python.unlink()
        python.symlink_to("../../python/bin/python3.13")
        for name in ("python3", "python3.13"):
            link = environment / "bin" / name
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to("python")

        config = environment / "pyvenv.cfg"
        lines = [
            line
            for line in config.read_text().splitlines()
            if not line.startswith("home = ")
        ]
        lines.insert(0, f"home = {RUNTIME_PREFIX}/bin")
        config.write_text("\n".join(lines) + "\n")
        config.write_text(config.read_text().replace(RUNTIME_PREFIX, str(stage / "python")))
        run([str(python), "-c", "import mlx.core, omlx; print(omlx.__file__)"])
        config.write_text(config.read_text().replace(str(stage / "python"), RUNTIME_PREFIX))
        _remove_runtime_build_artifacts(stage)
        _normalize_text_paths(stage, python_home=python_home, temporary=temporary)
        _privacy_scan(stage)
        create_archive(stage, options.output)
    finally:
        shutil.rmtree(DETERMINISTIC_BUILD_ROOT, ignore_errors=True)

    digest = hashlib.sha256(options.output.read_bytes()).hexdigest()
    print(f"\nArchive: {options.output}")
    print(f"Bytes:   {options.output.stat().st_size}")
    print(f"SHA256:  {digest}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("COPYFILE_DISABLE", "1")
    raise SystemExit(main())
