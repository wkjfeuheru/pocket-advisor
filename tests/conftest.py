"""测试环境隔离。"""

import shutil
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def work_dir():
    """仓库内的临时目录，替代在本环境不可用的 ``tmp_path``。

    写系统临时目录会 ``PermissionError``，因此统一锚定到仓库内
    ``.cache/test-work``（已在 .gitignore 中）。
    """
    base = Path(__file__).resolve().parent.parent / ".cache" / "test-work"
    path = base / f"case-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)
