"""持仓配置 REST：生成三档方案、查看、采纳，以及 AI 账户对照。

与 ``/api/portfolio/*`` 的模拟交易接口同域但不同职责：这里不改用户持仓，
只产出方案并把**采纳结果写进 AI 账本**。所有端点仅普通用户可用。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core.dependencies import require_user
from app.services.portfolio_advisor.advisor_service import (
    AdvisorService,
    AdvisorServiceError,
    get_advisor_service,
)
from app.utils.errors import http_500

router = APIRouter(prefix="/api/portfolio", tags=["portfolio-advisor"])

#: 业务错误码 → HTTP 状态。
_STATUS_BY_CODE = {
    "no_positions": 409,
    "no_products": 409,
    "no_eligible_product": 409,
    "no_current": 409,
    "plan_not_found": 404,
    "already_adopted": 409,
    "plan_unavailable": 423,
    "turnover_out_of_band": 423,
    "constraint_repaired": 423,
    "missing_nav": 409,
    "risk_exceeded": 422,
    "unpriceable": 422,
    "unpriceable_increase": 422,
    "unknown_plan": 422,
    "unknown_field": 422,
    "missing_plan": 422,
}


class GenerateAdviceRequest(BaseModel):
    """生成建议的请求体。两个 id 都是可选的，用于把方案挂到会话/轮次上。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str | None = None
    session_id: str | None = None


class AdoptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str | None = Field(default=None, max_length=128)


class SnapshotRequest(BaseModel):
    """写入快照的请求体；``as_of`` 留空表示今天。"""

    model_config = ConfigDict(extra="forbid")

    as_of: str | None = Field(default=None, max_length=32)


def _fail(exc: AdvisorServiceError) -> HTTPException:
    status = _STATUS_BY_CODE.get(exc.code, 400)
    return HTTPException(status_code=status, detail=f"{exc}（{exc.code}）")


def _guard(action: str, call: Any) -> Any:
    try:
        return call()
    except AdvisorServiceError as exc:
        raise _fail(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise http_500(action, exc) from exc


@router.post("/advice")
def generate_advice(http_request: Request, body: GenerateAdviceRequest | None = None) -> dict[str, Any]:
    """取当前持仓生成三档配置方案（不落用户持仓）。"""
    customer_id = require_user(http_request)
    payload = body or GenerateAdviceRequest()
    service: AdvisorService = get_advisor_service()
    return _guard(
        "生成配置建议",
        lambda: service.generate(
            customer_id, request_id=payload.request_id, session_id=payload.session_id
        ),
    )


@router.get("/allocations")
def list_allocations(http_request: Request, limit: int = 50) -> dict[str, Any]:
    customer_id = require_user(http_request)
    service = get_advisor_service()
    return _guard("读取方案列表", lambda: service.list_plans(customer_id, limit=limit))


@router.get("/allocations/{plan_id}")
def get_allocation(http_request: Request, plan_id: str) -> dict[str, Any]:
    customer_id = require_user(http_request)
    service = get_advisor_service()
    return _guard("读取方案详情", lambda: service.get_plan(customer_id, plan_id))


@router.post("/allocations/{plan_id}/adopt")
def adopt_allocation(
    http_request: Request, plan_id: str, body: AdoptRequest | None = None
) -> dict[str, Any]:
    """采纳方案：按方案权重在 AI 账本内差额调仓，用户持仓不变。"""
    customer_id = require_user(http_request)
    payload = body or AdoptRequest()
    service = get_advisor_service()
    return _guard(
        "采纳配置方案",
        lambda: service.adopt(customer_id, plan_id, request_id=payload.request_id),
    )


@router.get("/ai-account")
def get_ai_account(http_request: Request) -> dict[str, Any]:
    """AI 账户快照、用户账户口径对照，以及两条归一净值对比曲线。"""
    customer_id = require_user(http_request)
    service = get_advisor_service()
    return _guard("读取 AI 账户", lambda: service.ai_account(customer_id))


@router.post("/snapshots")
def write_snapshots(http_request: Request, body: SnapshotRequest | None = None) -> dict[str, Any]:
    """把当天两条线固化成快照（幂等，同日重跑覆盖）。

    正常由每日作业调用；单独暴露是为了让运维补完数据后能立刻生成一个点，
    也让验收能手工触发。
    """
    customer_id = require_user(http_request)
    payload = body or SnapshotRequest()
    service = get_advisor_service()
    as_of = None
    if payload.as_of:
        try:
            as_of = date.fromisoformat(payload.as_of)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"as_of 日期非法: {payload.as_of}"
            ) from exc
    outcomes = _guard(
        "写入每日快照", lambda: service.write_daily_snapshots(customer_id, as_of=as_of)
    )
    return {
        "customer_id": customer_id,
        "outcomes": [
            {
                "owner": item.owner,
                "snapshot_date": item.snapshot_date.isoformat(),
                "unit_nav": item.unit_nav,
                "daily_return": item.daily_return,
                "written": item.written,
                "skipped_reason": item.skipped_reason,
            }
            for item in outcomes
        ],
    }


__all__ = ["router"]
