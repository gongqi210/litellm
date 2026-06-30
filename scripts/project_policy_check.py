from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aimanager.scripts.project_policy_check import CheckResult, exit_code_for, main, run_policy_checks  # noqa: E402

__all__ = ["CheckResult", "exit_code_for", "main", "run_policy_checks"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
