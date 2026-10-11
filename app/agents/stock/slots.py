"""股票任务条件；原文标的在服务层通过元数据解析。"""

from pydantic import BaseModel, ConfigDict, Field

from app.agents.slots import TimeRange
from app.agents.stock.contracts import Horizon, Scenario, Strategy


class StockSlots(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: Scenario | None = None
    symbols: list[str] = Field(default_factory=list, max_length=3)
    timeframe: TimeRange | None = None
    horizon: Horizon | None = None
    strategy: Strategy | None = None
    industries: list[str] = Field(default_factory=list)
    index_scope: list[str] = Field(default_factory=list)
    candidate_count: int = Field(default=5, ge=1, le=10)
    filter_by_profile: bool = False
    metrics: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


__all__ = ["StockSlots"]
