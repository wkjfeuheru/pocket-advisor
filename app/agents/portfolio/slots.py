"""组合查询、诊断和生成方案的任务条件；画像不在槽位里。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.slots import TimeRange


class PortfolioSlots(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["query", "diagnose", "generate"] | None = None
    scope: Literal["current", "whole", "selected"] = "current"
    timeframe: TimeRange | None = None
    metrics: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    amount: float | None = Field(default=None, gt=0)


__all__ = ["PortfolioSlots"]
