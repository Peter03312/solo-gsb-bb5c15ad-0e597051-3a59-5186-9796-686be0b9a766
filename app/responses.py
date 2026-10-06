"""预检响应组装：把校验计划与引擎结果装配为对外响应模型。

单容器 / 双容器预检端点与配液核对端点共用，保证核对返回的 precheck
字段与原预检接口响应逐字段一致。
"""
from __future__ import annotations

from .models import PrecheckResponse, TransferPrecheckResponse
from .rules import RULESET_VERSION
from .validator import Plan
from .transfer_validator import TransferPlan


def build_precheck_response(plan: Plan, result: dict) -> PrecheckResponse:
    return PrecheckResponse(
        safe=not result["findings"],
        blocked=bool(result["findings"]),
        first_blocking_seq=result["first_blocking_seq"],
        ruleset_version=RULESET_VERSION,
        vessel_material=plan.vessel_material,
        temperature_c=plan.temperature_c,
        findings=result["findings"],
        final_state=result["final_state"],
        steps=result["traces"][1:],  # 第 0 条是 initial 快照
    )


def build_transfer_precheck_response(plan: TransferPlan, result: dict) -> TransferPrecheckResponse:
    return TransferPrecheckResponse(
        safe=not result["findings"],
        blocked=bool(result["findings"]),
        first_blocking_seq=result["first_blocking_seq"],
        first_blocking_vessel_id=result["first_blocking_vessel_id"],
        ruleset_version=RULESET_VERSION,
        temperature_c=plan.temperature_c,
        findings=result["findings"],
        steps=result["traces"][1:],  # 第 0 条是 initial 快照
        final_states=result["final_states"],
    )
