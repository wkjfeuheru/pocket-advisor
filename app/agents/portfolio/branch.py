"""持仓配置分支：生成方案 → 挂起等用户选 → 采纳。

接在 ``portfolio_agent`` 节点上。用可注入的 ``interrupt`` 函数实现，好处是：

- 能在**不搭真图 + 真 checkpointer** 的情况下测完整分支逻辑（真图那层另有集成测试）；
- 与 LangGraph 的耦合只剩一个函数签名；
- 挂起前先落库，因此 resume 时方案一定存在。

返回值是结构化的 ``PortfolioResult``：``offer`` 承载挂起前的三档方案集，
``adopted`` 承载采纳后的成交摘要。节点与 compliance_node / suitability_guard
只认这个类型，不需要知道分支内部细节。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping
from inspect import signature
from datetime import date

from app.agents.graphs.state import PortfolioResult
from app.agents.portfolio.contracts import (
    AllocationInterruptError,
    AllocationOffer,
    AllocationOutcome,
    build_allocation_payload,
    validate_allocation_resume,
)

#: 生成方案时的会话阶段标签（前端会直接展示）。
ALLOCATION_STAGE = "生成配置方案"

#: 中断时展示的阶段标签。
ALLOCATION_WAITING_STAGE = "等待选择方案"


class AllocationBranchError(ValueError):
    """分支前置条件不满足。``code`` 便于上层区分处理。"""

    def __init__(self, message: str, *, code: str = "allocation_branch_error") -> None:
        super().__init__(message)
        self.code = code


def _result(
    *,
    summary: str,
    status: str = "ok",
    warnings: list[str] | None = None,
    offer: AllocationOffer | None = None,
    adopted: AllocationOutcome | None = None,
) -> PortfolioResult:
    """构造持仓 worker 结果。``offer`` / ``adopted`` 分别对应两种产物。"""
    return PortfolioResult(
        status=status,  # type: ignore[arg-type] - 由调用点保证是三态之一
        summary=summary,
        warnings=list(warnings or []),
        source="postgresql" if offer is not None or adopted is not None else "",
        as_of=offer.nav_as_of if offer is not None else "",
        offer=offer,
        adopted=adopted,
    )


def _offer(generated: Mapping[str, Any]) -> AllocationOffer:
    """把服务返回整理成方案集：挂起前留痕，供结果侧核对。"""
    return AllocationOffer(
        plans=list(generated.get("plans") or []),
        unavailable=list(generated.get("unavailable") or []),
        nav_as_of=str(generated.get("nav_as_of") or ""),
        missing_nav=list(generated.get("missing_nav") or []),
    )


def _outcome(raw: Mapping[str, Any]) -> AllocationOutcome:
    """按白名单字段把采纳结果收成结构化摘要（丢弃 weights 等内部字段）。"""
    payload = {key: value for key, value in raw.items() if key in AllocationOutcome.model_fields}
    as_of = payload.get("nav_as_of")
    if isinstance(as_of, date):
        payload["nav_as_of"] = as_of.isoformat()
    elif as_of is None:
        payload["nav_as_of"] = ""
    return AllocationOutcome(**payload)


def _summarize(service_payload: Mapping[str, Any]) -> str:
    """给用户看的一句话，附带数据截止日与不可用档位。"""
    plans = list(service_payload.get("plans") or [])
    parts = [f"已生成 {len(plans)} 个配置方案，请选择其中一个。"]
    unavailable = list(service_payload.get("unavailable") or [])
    if unavailable:
        labels = "、".join(str(item.get("tier_label") or item.get("tier")) for item in unavailable)
        parts.append(f"以下档位在当前持仓下不可行：{labels}。")
    as_of = service_payload.get("nav_as_of")
    if as_of:
        parts.append(f"净值截至 {as_of}。")
    missing = list(service_payload.get("missing_nav") or [])
    if missing:
        parts.append(f"{len(missing)} 个持仓标的缺少净值，未纳入候选。")
    return "".join(parts)


def run_allocation_task(
    *,
    customer_id: str,
    service: Any,
    interrupt: Callable[[Any], Any],
    request_id: str | None = None,
    session_id: str | None = None,
    task_id: str = "",
    max_risk: str | None = None,
    domain_slots: dict[str, Any] | None = None,
) -> PortfolioResult:
    """跑一次配置建议：生成 → 挂起 → 校验选择 → 采纳。

    ``interrupt`` 是 LangGraph 的 ``interrupt``（或测试替身）。它在**方案已落库之后**
    才被调用，因此用户 resume 时引用的 ``plan_id`` 必然存在。
    """
    slots = domain_slots or {}
    unsupported = [key for key in ("limitations", "amount", "timeframe") if slots.get(key)]
    if unsupported:
        return _result(summary="当前配置引擎无法验证本次附加条件，请调整条件后再生成方案。",
                       status="degraded", warnings=[f"unsupported_{key}" for key in unsupported])
    try:
        kwargs = {"request_id": request_id, "session_id": session_id}
        if task_id:
            kwargs["task_id"] = task_id
        if max_risk:
            kwargs["max_risk"] = max_risk
        generated = service.generate(customer_id, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 生成失败要降级成可展示的结果
        return _result(
            summary="暂时无法生成配置建议，请稍后重试。",
            status="failed",
            warnings=[f"生成配置建议失败：{type(exc).__name__}"],
        )

    plans = list(generated.get("plans") or [])
    offer = _offer(generated)
    if not plans:
        # 一个可用档位都没有：不挂起（挂起也没有可选项），直接如实说明。
        unavailable = list(generated.get("unavailable") or [])
        detail = "、".join(
            f"{item.get('tier_label') or item.get('tier')}（{item.get('unavailable_reason')}）"
            for item in unavailable
        )
        return _result(
            summary=(
                "当前持仓下没有可行的调仓方案。"
                + (f"已排除的档位：{detail}。" if detail else "")
            ),
            status="degraded",
            warnings=["没有可行的调仓方案"],
            offer=offer,
        )

    payload = build_allocation_payload(plans)

    # 挂起：把选择权交给用户。interrupt 的返回值就是 resume 时提交的内容。
    resume_value = interrupt(payload)

    try:
        chosen = validate_allocation_resume(payload, resume_value)
    except AllocationInterruptError as exc:
        return _result(
            summary=f"方案选择无效，请重新发起配置建议（{exc}）。",
            status="failed",
            warnings=[f"方案选择无效：{exc.code}"],
            offer=offer,
        )

    try:
        adopt_kwargs = {"request_id": request_id}
        if max_risk and "max_risk" in signature(service.adopt).parameters:
            adopt_kwargs["max_risk"] = max_risk
        adopted = service.adopt(customer_id, chosen["plan_id"], **adopt_kwargs)
    except Exception as exc:  # noqa: BLE001 - 采纳失败要能解释，不能把整轮打崩
        return _result(
            summary="采纳方案失败，请核对当前画像与方案条件后重试。",
            status="failed",
            warnings=[f"采纳方案失败：{type(exc).__name__}"],
            offer=offer,
        )

    outcome = _outcome(adopted)
    return _result(
        summary=(
            f"已采纳「{outcome.tier_label or outcome.tier}」方案："
            f"成交 {outcome.trade_count} 笔，"
            f"AI 账户单位净值 {outcome.unit_nav}。"
            "您的真实持仓未做任何改动。"
        ),
        status="ok",
        offer=offer,
        adopted=outcome,
    )


def build_allocation_payload_from(generated: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """把服务的返回整理成 ``(公开摘要, 中断载荷)``。

    单独抽出来是为了让"不可用档位"也能进载荷——用户需要看到"为什么没有这一档"，
    而 ``build_allocation_payload`` 只接受可用方案（不可用的进去会被 resume 校验拒掉）。
    """
    plans = list(generated.get("plans") or [])
    payload = build_allocation_payload(plans)
    summary = {
        "content": _summarize(generated),
        "unavailable": list(generated.get("unavailable") or []),
    }
    return summary, payload


__all__ = [
    "ALLOCATION_STAGE",
    "ALLOCATION_WAITING_STAGE",
    "AllocationBranchError",
    "build_allocation_payload_from",
    "run_allocation_task",
]
