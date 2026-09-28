"""部署包装契约：统一 migrate 顺序、Compose 进程模型。"""

from __future__ import annotations

from pathlib import Path

import tomllib
from packaging.requirements import Requirement

from app.db.schema import (
    SCHEMA_APPLY_ORDER,
    SEED_SCHEMA_FILES,
    _load_sql,
    apply_postgres_schema,
)

ROOT = Path(__file__).resolve().parents[2]


def test_requirements_txt_covers_all_declared_direct_dependencies():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = {Requirement(item).name.lower() for item in project["dependencies"]}
    deployed = {
        Requirement(line).name.lower()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert declared <= deployed


class _FakeCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.statements.append(sql)

    def close(self) -> None:
        pass


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def test_apply_postgres_schema_executes_in_canonical_order():
    """migrate / 懒建表共用同一函数：按唯一清单顺序执行结构脚本。"""
    connection = _FakeConnection()
    apply_postgres_schema(connection, include_seed=False)

    seen = connection.cursor_instance.statements
    core = [_load_sql(name) for name in SCHEMA_APPLY_ORDER]
    assert seen == core


def test_apply_postgres_schema_seed_is_opt_in():
    connection = _FakeConnection()
    apply_postgres_schema(connection, include_seed=True)
    assert connection.cursor_instance.statements[-1] == _load_sql(SEED_SCHEMA_FILES[0])


def test_apply_postgres_schema_without_seed_skips_seed():
    connection = _FakeConnection()
    apply_postgres_schema(connection, include_seed=False)
    assert _load_sql(SEED_SCHEMA_FILES[0]) not in connection.cursor_instance.statements


def test_migrate_main_commits_on_success(monkeypatch):
    connection = _FakeConnection()
    monkeypatch.setattr(
        "app.core.config.get_postgres_connection_factory",
        lambda: (lambda: connection),
    )
    from scripts.migrate import main

    assert main([]) == 0
    assert connection.committed is True
    assert connection.closed is True
    assert connection.cursor_instance.statements[0] == _load_sql(SCHEMA_APPLY_ORDER[0])


def test_migrate_main_exits_nonzero_on_failure(monkeypatch, capsys):
    def _boom():
        raise RuntimeError("POSTGRES_PASSWORD 或 POSTGRES_DSN 必须配置")

    monkeypatch.setattr("app.core.config.get_postgres_connection_factory", _boom)
    from scripts.migrate import main

    assert main(["--seed"]) == 1
    captured = capsys.readouterr()
    assert "schema apply 失败" in captured.err


def _load_compose(text: str) -> dict:
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        data = yaml.safe_load(text)
        assert isinstance(data, dict)
        return data
    services: dict[str, dict] = {}
    in_services = False
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not in_services:
            if line.startswith("services:"):
                in_services = True
            continue
        if line and not line.startswith(" ") and line.endswith(":"):
            break
        if line.startswith("  ") and not line.startswith("    ") and line.endswith(":"):
            current = line.strip().rstrip(":")
            services[current] = {}
            continue
        if current is not None and "command:" in line:
            services[current]["command"] = line.split("command:", 1)[1].strip()
    return {"services": services}


def test_compose_declares_process_model_and_api_binds_all_interfaces():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    services = _load_compose(text)["services"]
    for name in ("postgres", "migrate", "api", "web"):
        assert name in services, f"docker-compose.yml 缺少服务 {name}"
    # 投顾/量化层已移除：不再有 celery / redis 服务。
    assert "celery" not in services
    assert "redis" not in services
    api = services["api"]
    command = api.get("command")
    command_text = " ".join(str(part) for part in command) if isinstance(command, list) else str(command or "")
    assert "0.0.0.0" in command_text
    assert "uvicorn" in command_text
    assert "app.main:app" in command_text


def test_dockerfile_installs_from_lock_and_listens_publicly():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "requirements.lock" in dockerfile
    assert "--host 0.0.0.0" in dockerfile or '"0.0.0.0"' in dockerfile
    assert "DEEPSEEK_API_KEY" not in dockerfile
    assert "app.main:app" in dockerfile
    # 业务代码收进 app/ 包；旧的顶层领域目录不再单独 COPY。
    assert "COPY tools" not in dockerfile
    assert "COPY cli" not in dockerfile
    assert "COPY auth" not in dockerfile
    assert "COPY product" not in dockerfile
    assert "COPY portfolio" not in dockerfile
    assert "COPY admin" not in dockerfile
    assert "COPY app" in dockerfile
    assert "COPY scripts" in dockerfile


def test_diagnose_schema_files_follow_migrate_order():
    from scripts.diagnose_schema_drift import SCHEMA_FILES

    assert SCHEMA_FILES == list(SCHEMA_APPLY_ORDER)
