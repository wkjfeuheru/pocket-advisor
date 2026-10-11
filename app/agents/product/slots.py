"""产品查询、比较和推荐的任务条件。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.slots import TimeRange


class ProductSlots(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["query", "compare", "recommend"] | None = None
    product_type: Literal["fund", "etf", "wealth"] | None = None
    symbols: list[str] = Field(default_factory=list)
    codes: list[str] = Field(default_factory=list)
    timeframe: TimeRange | None = None
    metrics: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


__all__ = ["ProductSlots"]
