from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parent
    cmd = [sys.executable, "-m", "bot.main", *sys.argv[1:]]
    return subprocess.call(cmd, cwd=str(project_root))


if __name__ == "__main__":
    raise SystemExit(main())
