from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path


def main() -> int:
    multiprocessing.freeze_support()
    if Path(sys.argv[0]).name == "semble":
        from semble.cli import main as semble_main

        result = semble_main()
        return int(result or 0)

    from geer.cli import main as geer_main

    return geer_main()


if __name__ == "__main__":
    raise SystemExit(main())
