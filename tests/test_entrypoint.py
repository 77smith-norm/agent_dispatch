from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_python_module_entrypoint_help_smoke() -> None:
    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "agent_dispatch", "--help"],
        capture_output=True,
        check=False,
        cwd=project_root,
        text=True,
    )

    assert result.returncode == 0
    assert "schema" in result.stdout
    assert "send" in result.stdout
