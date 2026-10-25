import os
import subprocess
import sys
from pathlib import Path


def test_deepseek_smoke_script_imports_and_stops_without_api_key():
    repo_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env.pop("DEEPSEEK_API_KEY", None)

    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "deepseek_integration_smoke.py")],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout.strip() == "FAIL: DEEPSEEK_API_KEY repository secret is missing."
    assert result.stderr == ""
