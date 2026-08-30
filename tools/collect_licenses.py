#!/usr/bin/env python3
"""Collect license evidence for the frozen production Python environment."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LICENSE_NAMES = re.compile(r"(?:license|licence|copying|notice|authors)", re.IGNORECASE)
TOKENIZERS_LICENSE = ROOT / "model-cards/ornith-1.5-35b-6bit/LICENSE-QWEN"


def production_packages() -> list[str]:
    result = subprocess.run(
        [
            "uv",
            "export",
            "--no-dev",
            "--no-hashes",
            "--no-emit-project",
            "--format",
            "requirements-txt",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    names: set[str] = set()
    for line in result.stdout.splitlines():
        if not line or line[0].isspace() or line.startswith("#"):
            continue
        name = re.split(r"[=; <>=!~\[]", line, maxsplit=1)[0]
        names.add(name)
    return sorted(names, key=str.casefold)


def collect(destination: Path) -> None:
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    inventory = ["# Frozen Python dependency inventory", ""]
    for name in production_packages():
        try:
            package = distribution(name)
        except PackageNotFoundError:
            continue  # A locked dependency for another platform.
        package_dir = destination / f"{package.metadata['Name']}-{package.version}"
        evidence: list[Path] = []
        for entry in package.files or ():
            if LICENSE_NAMES.search(str(entry)):
                source = Path(package.locate_file(entry))
                if source.is_file() and source.stat().st_size <= 2 * 1024 * 1024:
                    evidence.append(source)
        if evidence:
            package_dir.mkdir()
            used: set[str] = set()
            for index, source in enumerate(evidence, start=1):
                filename = source.name
                if filename in used:
                    filename = f"{index}-{filename}"
                used.add(filename)
                shutil.copy(source, package_dir / filename)
        else:
            license_text = package.metadata.get("License")
            expression = package.metadata.get("License-Expression")
            package_dir.mkdir()
            if license_text and "\n" in license_text:
                (package_dir / "LICENSE-from-package-metadata.txt").write_text(
                    license_text.rstrip() + "\n"
                )
            elif name.casefold() == "tokenizers":
                shutil.copy(TOKENIZERS_LICENSE, package_dir / "LICENSE-APACHE-2.0")
            elif expression or license_text:
                (package_dir / "LICENSE-EXPRESSION.txt").write_text(
                    f"Declared license: {expression or license_text}\n"
                )
            else:
                raise RuntimeError(f"no license evidence found for {name} {package.version}")
        inventory.append(f"- {package.metadata['Name']} {package.version}")

    python_licenses = list(Path(sys.base_prefix).glob("lib/python*/LICENSE.txt"))
    if len(python_licenses) != 1:
        raise RuntimeError("expected one bundled Python license")
    python_dir = destination / f"Python-{sys.version_info.major}.{sys.version_info.minor}"
    python_dir.mkdir()
    shutil.copy(python_licenses[0], python_dir / "LICENSE.txt")
    inventory.append(f"- Python {sys.version.split()[0]}")
    (destination / "INVENTORY.md").write_text("\n".join(inventory) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    options = parser.parse_args()
    collect(options.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
