#!/usr/bin/env python3
"""Install Geer's pinned official Pi release for source development."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from geer.pi_distribution import (  # noqa: E402
    PiDistributionError,
    install_pi,
    pi_distribution_plan,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="print the pin without downloading")
    parser.add_argument("--destination", type=Path, default=ROOT / "build/pi-runtime")
    parser.add_argument("--archive", type=Path, help="verify and use an existing upstream archive")
    options = parser.parse_args(argv)
    if options.plan:
        print(json.dumps(pi_distribution_plan(options.destination), indent=2, sort_keys=True))
        return 0
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise PiDistributionError("Geer's pinned Pi runtime requires Apple Silicon macOS")
    executable = install_pi(options.destination, archive=options.archive)
    print(f"Pi is ready at {executable}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PiDistributionError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
