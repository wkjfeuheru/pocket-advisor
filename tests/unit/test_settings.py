"""Config/PostgreSQL 设置校验测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_postgres_settings_reject_invalid_values():
    from app.core.config import PostgresSettings

    with pytest.raises(ValidationError):
        PostgresSettings(port="not-a-port")
    with pytest.raises(ValidationError):
        PostgresSettings(pool_min_size=8, pool_max_size=4)
    with pytest.raises(ValidationError):
        PostgresSettings(connect_timeout=-1)


def test_config_still_exports_the_validated_values():
    """兼容导出：config 的常量名保持存在，且与 Settings 的校验口径一致。"""
    from app.core import config
    from app.core.config import load_postgres_settings

    postgres = load_postgres_settings()

    assert config.POSTGRES_HOST == postgres.host
    assert config.POSTGRES_PORT == postgres.port
    assert config.POSTGRES_POOL_MIN_SIZE == postgres.pool_min_size
    assert config.POSTGRES_POOL_MAX_SIZE == postgres.pool_max_size


def test_business_policy_thresholds_are_numeric():
    from app.core import config

    assert isinstance(config.PORTFOLIO_MAX_DEPOSIT_AMOUNT, float)
    assert isinstance(config.PORTFOLIO_MIN_ORDER_AMOUNT, float)
    assert config.PORTFOLIO_MAX_DEPOSIT_AMOUNT > 0
    assert config.PORTFOLIO_MIN_ORDER_AMOUNT > 0
