"""配液核对（实测浓度对账）。

流程：
1. 沿用原预检请求的逐项校验与模拟（build_plan / build_transfer_plan + 引擎），
   原流程无效则整单拒绝，不返回任何对账结果；
2. 把每条观测定位到“初始态”或“某个步骤完成后”的位置，取该位置的**未四舍五入**
   预测浓度——该位置未出现的已知试剂按 0 计；
3. 逐条比较 |实测 - 预测| ≤ tolerance_pp（统一有限非负容差，百分点）；
4. 返回原预检风险结果、逐条预测值与偏差，以及按步骤执行顺序的最早超差位置
   （初始态位置最前；同一位置按观测提交顺序）。

观测本身的非法情形（未知步骤/容器/试剂、同位置同试剂重复、浓度越界、
非法容差）同样整单拒绝。
"""
from __future__ import annotations

import math

from . import errors
from .catalog import REAGENTS
from .engine import Engine
from .models import (
    ReconcileRequest,
    ReconcileResponse,
    TransferReconcileRequest,
    TransferReconcileResponse,
)
from .responses import build_precheck_response, build_transfer_precheck_response
from .transfer_engine import TwoVesselEngine
from .transfer_validator import build_transfer_plan
from .validator import _detail, build_plan

_EPS = 1e-9

REJECT_MESSAGE = "核对请求被整单拒绝：原流程无效或容差/观测非法，未进行模拟与对账"


def _is_real_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _validate_tolerance(tolerance, errs: list[dict]) -> bool:
    if not _is_real_number(tolerance) or tolerance < 0:
        errs.append(
            _detail(
                errors.INVALID_TOLERANCE,
                f"tolerance_pp 须为有限非负数（百分点），收到 {tolerance}",
                fld="tolerance_pp",
            )
        )
        return False
    return True


def _normalize_observations(
    observations, errs: list[dict], *, transfer: bool, check_vessel: bool, vessel_ids: set[str] | None = None
) -> list[dict]:
    """与原流程无关的逐项观测校验：位置表达、容器、试剂、浓度、同位置重复。

    transfer=False 为单容器（观测带 vessel_id 在 schema 层即被拒）。
    双容器模式（transfer=True）始终按观测自带 vessel_id 维度去重；仅当容器定义
    本身可信（check_vessel=True）时才报 UNKNOWN_VESSEL，避免容器非法时产生级联噪声。
    返回归一化观测列表（initial=True 时 seq 归为 None），非法记录仍占位。
    """
    normalized: list[dict] = []
    seen: set[tuple] = set()
    for i, ob in enumerate(observations):
        pf = f"observations[{i}]"
        initial = bool(ob.at_initial)
        seq = ob.seq

        # 位置：at_initial=true 不得再带 seq；二者皆缺 / seq 非正整数 则位置非法
        if initial:
            if seq is not None:
                errs.append(
                    _detail(errors.INVALID_POSITION, "初始态观测（at_initial=true）不得同时指定 seq", fld=f"{pf}.seq")
                )
            pos_key = None
        else:
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
                errs.append(
                    _detail(
                        errors.INVALID_POSITION,
                        "观测须指定 at_initial=true，或给出正整数步骤序号 seq",
                        fld=f"{pf}.seq",
                    )
                )
                pos_key = None
            else:
                pos_key = seq

        # 双容器：vessel_id 始终参与去重维度；只有容器定义可信时才校验其是否已知
        vessel_id = getattr(ob, "vessel_id", None) if transfer else None
        if transfer and check_vessel and vessel_id not in vessel_ids:
            errs.append(
                _detail(errors.UNKNOWN_VESSEL, f"未知容器编号：{vessel_id}", fld=f"{pf}.vessel_id")
            )

        rid = ob.reagent_id
        if rid not in REAGENTS:
            errs.append(
                _detail(errors.UNKNOWN_REAGENT, f"未知试剂编号：{rid}", fld=f"{pf}.reagent_id")
            )

        conc = ob.concentration
        if not _is_real_number(conc) or conc < 0.0 or conc > 100.0:
            errs.append(
                _detail(
                    errors.CONCENTRATION_OUT_OF_RANGE,
                    f"实测浓度须在 [0, 100] 百分点内，收到 {conc}",
                    fld=f"{pf}.concentration",
                )
            )

        # 同位置同试剂重复观测（双容器再加容器维度）；位置畸形的记录不参与去重以免级联噪声
        if initial or pos_key is not None:
            key = (initial, pos_key, vessel_id, rid)
            if key in seen:
                where = "初始态" if initial else f"步骤 {pos_key}"
                if vessel_id is not None:
                    where = f"容器 {vessel_id} {where}"
                errs.append(
                    _detail(
                        errors.DUPLICATE_OBSERVATION,
                        f"{where}对试剂 {rid} 的观测重复提交（同位置同试剂只允许一条）",
                        fld=pf,
                    )
                )
            else:
                seen.add(key)

        normalized.append(
            {
                "index": i,
                "initial": initial,
                "seq": None if initial else (pos_key if isinstance(pos_key, int) else None),
                "vessel_id": vessel_id,
                "reagent_id": rid,
                "measured": conc if _is_real_number(conc) else None,
                "position_ok": initial or pos_key is not None,
            }
        )
    return normalized


def _check_unknown_positions(normalized: list[dict], known_seqs: set[int], errs: list[dict]) -> None:
    """原流程校验通过后：步骤序号必须存在于流程中（乱序提交也按归一化计划识别）。"""
    for ob in normalized:
        if not ob["initial"] and ob["position_ok"] and ob["seq"] not in known_seqs:
            errs.append(
                _detail(
                    errors.UNKNOWN_POSITION,
                    f"步骤序号 {ob['seq']} 不在原流程中（只能核对初始态或已有步骤完成后的状态）",
                    seq=ob["seq"],
                    fld=f"observations[{ob['index']}].seq",
                )
            )


def _compare(normalized, tolerance, raw_concs, *, seq_to_pos, by_vessel: bool) -> tuple[list[dict], list[dict]]:
    """用未四舍五入的预测浓度逐条对账，返回（逐条结果, 超差结果）。"""
    items: list[dict] = []
    failures: list[dict] = []
    for ob in normalized:
        pos = 0 if ob["initial"] else seq_to_pos[ob["seq"]]
        table = raw_concs[pos]
        if by_vessel:
            predicted = table.get(ob["vessel_id"], {}).get(ob["reagent_id"], 0.0)
        else:
            predicted = table.get(ob["reagent_id"], 0.0)
        measured = float(ob["measured"])
        deviation = abs(measured - predicted)
        passed = deviation <= tolerance + _EPS
        item = {
            "observation_index": ob["index"],
            "at_initial": ob["initial"],
            "seq": ob["seq"],
            "reagent_id": ob["reagent_id"],
            "measured_concentration": measured,
            "predicted_concentration": predicted,
            "deviation": deviation,
            "passed": passed,
        }
        if by_vessel:
            item["vessel_id"] = ob["vessel_id"]
        items.append(item)
        if not passed:
            failures.append({"position_index": pos, **item})
    failures.sort(key=lambda f: (f["position_index"], f["observation_index"]))
    return items, failures


def _first_out(failure: dict | None, tolerance: float, *, by_vessel: bool) -> dict | None:
    if failure is None:
        return None
    out = {
        "observation_index": failure["observation_index"],
        "at_initial": failure["at_initial"],
        "seq": failure["seq"],
        "reagent_id": failure["reagent_id"],
        "measured_concentration": failure["measured_concentration"],
        "predicted_concentration": failure["predicted_concentration"],
        "deviation": failure["deviation"],
        "tolerance_pp": tolerance,
    }
    if by_vessel:
        out["vessel_id"] = failure["vessel_id"]
    return out


# ---------------------------------------------------------------------------
# 单容器
# ---------------------------------------------------------------------------
def reconcile_single(req: ReconcileRequest) -> ReconcileResponse:
    errs: list[dict] = []
    _validate_tolerance(req.tolerance_pp, errs)
    normalized = _normalize_observations(req.observations, errs, transfer=False, check_vessel=False)

    plan = None
    try:
        plan = build_plan(req.request)
    except errors.ValidationFailure as exc:
        errs.extend(exc.details)

    if plan is not None:
        known_seqs = {st.seq for st in plan.steps}
    else:
        # 原流程无效时仍须给出 UNKNOWN_POSITION：直接用原请求提交的 seq 兜底
        # （seq 重复等原流程错误已在 details 中，不影响观测位置的逐项拒绝）
        known_seqs = {st.seq for st in req.request.steps if isinstance(st.seq, int) and not isinstance(st.seq, bool) and st.seq > 0}
    _check_unknown_positions(normalized, known_seqs, errs)

    if errs:
        raise errors.ValidationFailure(errs, message=REJECT_MESSAGE)

    engine = Engine(plan)
    result = engine.run()
    seq_to_pos = {st.seq: i + 1 for i, st in enumerate(plan.steps)}
    items, failures = _compare(
        normalized, float(req.tolerance_pp), engine.raw_concentrations,
        seq_to_pos=seq_to_pos, by_vessel=False,
    )

    return ReconcileResponse(
        tolerance_pp=float(req.tolerance_pp),
        all_passed=all(it["passed"] for it in items),
        first_out_of_tolerance=_first_out(failures[0] if failures else None, float(req.tolerance_pp), by_vessel=False),
        precheck=build_precheck_response(plan, result),
        observations=items,
    )


# ---------------------------------------------------------------------------
# 双容器
# ---------------------------------------------------------------------------
def reconcile_transfer(req: TransferReconcileRequest) -> TransferReconcileResponse:
    errs: list[dict] = []
    _validate_tolerance(req.tolerance_pp, errs)

    plan = None
    # 容器编号集合在原流程校验前即可静态取得；数量/重复问题由原流程校验负责，
    # 容器定义不可信时跳过观测容器校验，避免级联噪声。
    candidate_ids = {v.vessel_id for v in req.request.vessels if isinstance(v.vessel_id, str) and v.vessel_id}
    usable_vessels = len(candidate_ids) == 2 and len(req.request.vessels) == 2
    normalized = _normalize_observations(
        req.observations,
        errs,
        transfer=True,
        check_vessel=usable_vessels,
        vessel_ids=candidate_ids if usable_vessels else None,
    )

    try:
        plan = build_transfer_plan(req.request)
    except errors.ValidationFailure as exc:
        errs.extend(exc.details)

    if plan is not None:
        known_seqs = {st.seq for st in plan.steps}
    else:
        # 原流程无效时仍须给出 UNKNOWN_POSITION：直接用原请求提交的 seq 兜底
        known_seqs = {
            st.seq for st in req.request.steps
            if isinstance(st.seq, int) and not isinstance(st.seq, bool) and st.seq > 0
        }
    _check_unknown_positions(normalized, known_seqs, errs)

    if errs:
        raise errors.ValidationFailure(errs, message=REJECT_MESSAGE)

    engine = TwoVesselEngine(plan)
    result = engine.run()
    seq_to_pos = {st.seq: i + 1 for i, st in enumerate(plan.steps)}
    items, failures = _compare(
        normalized, float(req.tolerance_pp), engine.raw_concentrations,
        seq_to_pos=seq_to_pos, by_vessel=True,
    )

    return TransferReconcileResponse(
        tolerance_pp=float(req.tolerance_pp),
        all_passed=all(it["passed"] for it in items),
        first_out_of_tolerance=_first_out(failures[0] if failures else None, float(req.tolerance_pp), by_vessel=True),
        precheck=build_transfer_precheck_response(plan, result),
        observations=items,
    )
