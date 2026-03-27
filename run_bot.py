from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    workspace_root = Path(__file__).resolve().parent
    project_root = workspace_root / "trading-bot"

    if not project_root.exists():
        print(f"Error: project directory not found: {project_root}")
        return 1

    cmd = [sys.executable, "-m", "bot.main", *sys.argv[1:]]
    return subprocess.call(cmd, cwd=str(project_root))


if __name__ == "__main__":
    raise SystemExit(main())
