"""预检风险的物料来源溯源组装。

引擎在每条风险触发瞬间把涉及试剂的**来源批次快照**附在 finding 的内部字段
``_source_snapshot`` 中（reagent_id -> {lot_key: 纯物体积}）。本模块把快照
翻译为对外的 ``risk_sources``：

- 单容器：初始液体批次 ("initial", reagent_id)、后续加液批次 ("step", seq, reagent_id)；
- 双容器：初始液体批次 ("vessel", vessel_id, reagent_id)，跨容器转移只携带原键。

比例口径：每条风险、每个涉及试剂，分母为**触发时刻、触发容器内该试剂的纯物
总量**（各批次之和），故同一试剂各批次 fraction 合计为 1；材质实体不给液体份额。

超时风险的快照在该步操作执行前采集（引擎先结算超时再执行操作），因此后续倒出
或转空不会改写已触发风险的来源。
"""
from __future__ import annotations

from .models import (
    RiskSource,
    SourceLotOut,
    SourceTraceResponse,
    TracedReagent,
    TransferRiskSource,
    TransferSourceTraceResponse,
    VesselSourceLotOut,
    VesselTracedReagent,
)
from .responses import build_precheck_response, build_transfer_precheck_response

_EPS = 1e-9
_ROUND_VOL = 4
_ROUND_FRAC = 6


def _lot_order(key) -> tuple:
    if isinstance(key, tuple):
        if key[0] == "initial":
            return (0, 0, str(key[1] if len(key) > 1 else ""))
        if key[0] == "step":
            return (1, key[1] if len(key) > 1 else 0, str(key[2] if len(key) > 2 else ""))
        if key[0] == "vessel":
            return (0, str(key[1]), str(key[2] if len(key) > 2 else ""))
    return (2, 0, str(key))


def _material_id(entities: list[str]) -> str | None:
    for tok in entities:
        if tok.startswith("material:"):
            return tok[len("material:"):]
    return None


def _reagent_order(finding: dict) -> list[str]:
    """按 finding 实体顺序列出液相试剂（材质实体不参与液体份额）。"""
    return [tok for tok in finding["entities"] if not tok.startswith("material:")]


def _build_single_lots(rid: str, lots: dict, total: float) -> list[SourceLotOut]:
    out: list[SourceLotOut] = []
    for key in sorted(lots, key=_lot_order):
        amount = lots[key]
        if amount <= _EPS:
            continue
        kind = key[0] if isinstance(key, tuple) and key else None
        out.append(
            SourceLotOut(
                origin="step" if kind == "step" else "initial",
                reagent_id=rid,
                seq=key[1] if kind == "step" and len(key) > 1 else None,
                pure_volume_ml=round(amount, _ROUND_VOL),
                fraction=round(amount / total, _ROUND_FRAC) if total > _EPS else 0.0,
            )
        )
    return out


def _build_vessel_lots(rid: str, lots: dict, total: float) -> list[VesselSourceLotOut]:
    out: list[VesselSourceLotOut] = []
    for key in sorted(lots, key=_lot_order):
        amount = lots[key]
        if amount <= _EPS:
            continue
        kind = key[0] if isinstance(key, tuple) and key else None
        out.append(
            VesselSourceLotOut(
                origin="vessel",
                vessel_id=key[1] if kind == "vessel" and len(key) > 1 else "",
                reagent_id=rid,
                pure_volume_ml=round(amount, _ROUND_VOL),
                fraction=round(amount / total, _ROUND_FRAC) if total > _EPS else 0.0,
            )
        )
    return out


def build_source_trace(plan, result: dict) -> SourceTraceResponse:
    precheck = build_precheck_response(plan, result)
    risk_sources: list[RiskSource] = []
    for idx, finding in enumerate(result["findings"]):
        snapshot: dict = finding.get("_source_snapshot", {}) or {}
        reagents: list[TracedReagent] = []
        for rid in _reagent_order(finding):
            lots = snapshot.get(rid, {})
            total = sum(lots.values())
            reagents.append(
                TracedReagent(
                    reagent_id=rid,
                    pure_volume_ml=round(total, _ROUND_VOL),
                    lots=_build_single_lots(rid, lots, total),
                )
            )
        risk_sources.append(
            RiskSource(
                finding_index=idx,
                seq=finding["seq"],
                category=finding["category"],
                rule_id=finding["rule_id"],
                vessel_material=plan.vessel_material,
                material_id=_material_id(finding["entities"]),
                reagents=reagents,
            )
        )
    return SourceTraceResponse(precheck=precheck, risk_sources=risk_sources)


def build_transfer_source_trace(plan, result: dict) -> TransferSourceTraceResponse:
    precheck = build_transfer_precheck_response(plan, result)
    material_by_vessel = {spec.vessel_id: spec.vessel_material for spec in plan.vessels}
    risk_sources: list[TransferRiskSource] = []
    for idx, finding in enumerate(result["findings"]):
        snapshot: dict = finding.get("_source_snapshot", {}) or {}
        reagents: list[VesselTracedReagent] = []
        for rid in _reagent_order(finding):
            lots = snapshot.get(rid, {})
            total = sum(lots.values())
            reagents.append(
                VesselTracedReagent(
                    reagent_id=rid,
                    pure_volume_ml=round(total, _ROUND_VOL),
                    lots=_build_vessel_lots(rid, lots, total),
                )
            )
        vid = finding["vessel_id"]
        risk_sources.append(
            TransferRiskSource(
                finding_index=idx,
                seq=finding["seq"],
                vessel_id=vid,
                category=finding["category"],
                rule_id=finding["rule_id"],
                vessel_material=material_by_vessel.get(vid, ""),
                material_id=_material_id(finding["entities"]),
                reagents=reagents,
            )
        )
    return TransferSourceTraceResponse(precheck=precheck, risk_sources=risk_sources)
