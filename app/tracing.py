"""风险触发时的物料来源溯源。

在原预检引擎（app.engine / app.transfer_engine）之上做两件事：

1. 开启 VesselRuntime 的“来源批次”记账（runtime 内随纯物量一起按充分混合
   比例分摊；稀释不动批次；清洗清空）；
2. 在每条风险 finding **触发的那一刻** 对所在容器的批次台账拍快照——
   即时类风险在操作完成后的对账阶段拍，超时类风险在时间推进后、本步操作
   执行前拍，因此后续倒出 / 转空都不会改写已触发风险的来源明细。

溯源引擎复用原引擎的全部校验、重放与发现逻辑，只额外产生 traces；
原预检接口与配液核对接口不启用批次记账，行为保持不变。
"""
from __future__ import annotations

from .engine import Engine
from .runtime import VesselRuntime
from .transfer_engine import TwoVesselEngine
from .validator import PlannedStep

_EPS = 1e-9

_CATEGORY_ORDER = {"forbidden_mix": 0, "material_incompatible": 1, "overtime_contact": 2}


def _is_material_token(tok: str) -> bool:
    return tok.startswith("material:")


def _source_sort_key(source: dict, vessel_rank: dict[str, int] | None = None) -> tuple:
    # 初始来源在前，加液来源按步骤序号；双容器再按来源容器的提交顺序稳定排序
    rank = vessel_rank.get(source.get("vessel_id", ""), 0) if vessel_rank else 0
    if source["type"] == "initial":
        return (0, rank, source.get("reagent_id", ""))
    return (1, rank, source["seq"])


def _reagent_sources(
    rt: VesselRuntime, reagent_id: str, vessel_rank: dict[str, int] | None = None
) -> tuple[list[dict], float]:
    """汇总该试剂在触发时刻仍存的各来源批次，返回（明细, 该试剂纯物量合计）。"""
    lots = [lot for lot in rt.state.lots.values() if lot.reagent_id == reagent_id and lot.amount > _EPS]
    total = sum(lot.amount for lot in lots)
    if total <= _EPS:
        return [], 0.0
    lots.sort(key=lambda lot: _source_sort_key(lot.source, vessel_rank))
    items = [
        {
            "source": dict(lot.source),
            "pure_volume_ml": round(lot.amount, 4),
            "fraction": round(lot.amount / total, 6),
        }
        for lot in lots
    ]
    return items, total


class TracingSingleEngine(Engine):
    """单容器溯源：为 add 步骤登记以步骤序号标识的来源批次，并在触发时拍快照。"""

    lot_mode = "single"

    def __init__(self, plan):
        super().__init__(plan)
        self.trace_records: list[dict] = []

    def _apply(self, st: PlannedStep) -> None:
        if st.op == "add":
            source = {"key": self.runtime._add_lot_key(st.seq), "descriptor": {"type": "add", "seq": st.seq}}
            self.runtime.apply_add(st.reagent_id, st.concentration, st.volume_ml, source)  # type: ignore[arg-type]
        else:
            super()._apply(st)

    def _snapshot(self, finding: dict) -> dict:
        material = {"material_id": self.plan.vessel_material} if any(_is_material_token(t) for t in finding["entities"]) else None
        reagent_sources = []
        for tok in finding["entities"]:
            if _is_material_token(tok):
                continue
            items, total = _reagent_sources(self.runtime, tok)
            reagent_sources.append(
                {
                    "reagent_id": tok,
                    "total_pure_volume_ml": round(total, 4),
                    "sources": items,
                }
            )
        return {
            "seq": finding["seq"],
            "category": finding["category"],
            "rule_id": finding["rule_id"],
            "material": material,
            "reagent_sources": reagent_sources,
        }

    def _emit(self, seq, ev) -> None:
        super()._emit(seq, ev)
        self.trace_records.append(self._snapshot(self.findings[-1]))

    def run(self) -> dict:
        result = super().run()
        # 与 findings 完全相同的稳定排序，保证第 i 条来源明细对应第 i 条 finding
        self.trace_records.sort(key=lambda t: (t["seq"] if t["seq"] is not None else 0, _CATEGORY_ORDER[t["category"]]))
        traces = []
        for index, rec in enumerate(self.trace_records):
            traces.append({"finding_index": index, **rec})
        return {**result, "source_traces": traces}


class TracingTransferEngine(TwoVesselEngine):
    """双容器溯源：批次键带容器前缀、跨容器转移保持来源不变，并在触发容器拍快照。"""

    lot_mode = "dual"

    def __init__(self, plan):
        super().__init__(plan)
        self.trace_records: list[dict] = []
        self.vessel_rank = {spec.vessel_id: i for i, spec in enumerate(plan.vessels)}

    def _snapshot(self, rt: VesselRuntime, finding: dict) -> dict:
        material = (
            {"vessel_id": rt.vessel_id, "material_id": rt.vessel_material}
            if any(_is_material_token(t) for t in finding["entities"])
            else None
        )
        reagent_sources = []
        for tok in finding["entities"]:
            if _is_material_token(tok):
                continue
            items, total = _reagent_sources(rt, tok, self.vessel_rank)
            reagent_sources.append(
                {
                    "reagent_id": tok,
                    "total_pure_volume_ml": round(total, 4),
                    "sources": items,
                }
            )
        return {
            "seq": finding["seq"],
            "vessel_id": rt.vessel_id,
            "category": finding["category"],
            "rule_id": finding["rule_id"],
            "material": material,
            "reagent_sources": reagent_sources,
        }

    def _emit(self, rt: VesselRuntime, seq, ev) -> None:
        super()._emit(rt, seq, ev)
        self.trace_records.append(self._snapshot(rt, self.findings[-1]))

    def run(self) -> dict:
        result = super().run()
        self.trace_records.sort(
            key=lambda t: (
                t["seq"] if t["seq"] is not None else 0,
                _CATEGORY_ORDER[t["category"]],
                self.vessel_rank[t["vessel_id"]],
            )
        )
        traces = []
        for index, rec in enumerate(self.trace_records):
            traces.append({"finding_index": index, **rec})
        return {**result, "source_traces": traces}
