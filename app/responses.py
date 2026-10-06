"""预检响应组装：把校验计划与引擎结果装配为对外响应模型。

单容器 / 双容器预检端点与配液核对端点共用，保证核对返回的 precheck
字段与原预检接口响应逐字段一致。

引擎 finding 中携带的内部字段（``_source_snapshot`` 物料来源快照）不属于
原预检响应契约，由本模块统一剥离；来源溯源接口另行读取该快照。
"""
from __future__ import annotations

from .models import PrecheckResponse, TransferPrecheckResponse
from .rules import RULESET_VERSION
from .validator import Plan
from .transfer_validator import TransferPlan

# 引擎内部字段前缀：不进入原预检 / 核对响应
_INTERNAL_PREFIX = "_"


def public_findings(result: dict) -> list[dict]:
    """剥离 finding 中的内部字段（来源快照等），保持原预检响应契约。"""
    return [
        {k: v for k, v in finding.items() if not k.startswith(_INTERNAL_PREFIX)}
        for finding in result["findings"]
    ]


def build_precheck_response(plan: Plan, result: dict) -> PrecheckResponse:
    return PrecheckResponse(
        safe=not result["findings"],
        blocked=bool(result["findings"]),
        first_blocking_seq=result["first_blocking_seq"],
        ruleset_version=RULESET_VERSION,
        vessel_material=plan.vessel_material,
        temperature_c=plan.temperature_c,
        findings=public_findings(result),
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
        findings=public_findings(result),
        steps=result["traces"][1:],  # 第 0 条是 initial 快照
        final_states=result["final_states"],
    )
