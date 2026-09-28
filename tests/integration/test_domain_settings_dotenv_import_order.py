"""业务策略设置应无论 ASGI 导入顺序都遵循 .env。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAIN_ENV_NAMES = {
    "PORTFOLIO_MAX_DEPOSIT_AMOUNT",
    "PORTFOLIO_MIN_ORDER_AMOUNT",
}


def test_asgi_import_loads_domain_policy_values_from_dotenv(work_dir: Path) -> None:
    """先导入 ASGI 应用也不能在 .env 加载前冻结领域默认值。"""
    dotenv_values = {
        "PORTFOLIO_MAX_DEPOSIT_AMOUNT": "7654321.5",
        "PORTFOLIO_MIN_ORDER_AMOUNT": "17.25",
    }
    (work_dir / ".env").write_text(
        "\n".join(f"{name}={value}" for name, value in dotenv_values.items()),
        encoding="utf-8",
    )

    child_env = {
        name: value
        for name, value in os.environ.items()
        if name not in DOMAIN_ENV_NAMES
    }
    child_env.update({"PYTHONPATH": str(REPO_ROOT)})
    script = """
import json
import app.main
from app.core import config as settings
print(json.dumps({
    "PORTFOLIO_MAX_DEPOSIT_AMOUNT": settings.PORTFOLIO_MAX_DEPOSIT_AMOUNT,
    "PORTFOLIO_MIN_ORDER_AMOUNT": settings.PORTFOLIO_MIN_ORDER_AMOUNT,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=work_dir,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {
        name: float(value) for name, value in dotenv_values.items()
    }
