"""Compatibility wrapper for source-checkout setup."""

from __future__ import annotations

import subprocess
import sys

if __name__ == "__main__":
    raise SystemExit(
        subprocess.run(["uv", "run", "geer", "setup", *sys.argv[1:]], check=False).returncode
    )
