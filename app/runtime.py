"""单只容器的逐步混配运行时（单容器预检与双容器转移预检共用）。

状态模型（简化、透明的体积-浓度簿记）：
- 每个试剂记录“纯物体积” pure_ml；当前浓度 = pure_ml / 液体总体积。
- add:      pure_ml += conc * v / 100，总体积 += v
- dilute:   仅总体积 += v（加纯水）
- pour:     按倒出比例 f 同步缩减总体积与每个 pure_ml（均匀混合假设）
- transfer_out/in: pour 的“跨容器”版本，转出侧按比例缩减，转入侧计入纯物量与体积
- clean:    清空全部状态，接触计时归零

溯源模式（lot_mode 非 None）另维护“来源批次”台账 lots：批次随纯物量一起按比例
分摊（pour/transfer），dilute 不动、clean 清空；原预检/核对不启用该台账。

每个实体对（试剂-试剂 / 试剂-材质）的每条匹配规则维护一个“接触片段”：
条件满足期间计时；超时在时间推进到下一个操作点时检查。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import REAGENTS, material_token, reagent_tags
from .rules import RULES, Rule

_EPS = 1e-9


# ---------------------------------------------------------------------------
# 实体与规则匹配
# ---------------------------------------------------------------------------
def entity_kind(tok: str) -> str:
    return "material" if tok.startswith("material:") else "reagent"


def _entity_tags(tok: str) -> frozenset[str]:
    if tok.startswith("material:"):
        return frozenset((tok,))
    return reagent_tags(tok)


def _side_matches(side, tok: str, concentration: float) -> bool:
    kind = entity_kind(tok)
    selectors = side.selectors
    if any(s.startswith("material:") for s in selectors):
        if kind != "material":
            return False
    else:
        if kind != "reagent":
            return False
    if kind == "reagent" and concentration < side.min_concentration - _EPS:
        return False
    return len(selectors & _entity_tags(tok)) > 0


@dataclass
class RuleEpisode:
    rule_id: str
    start: float           # 本接触片段开始时刻
    reported: bool = False  # 该片段内是否已出过超时 finding


# ---------------------------------------------------------------------------
# 物料来源批次（风险溯源用）
#
# 同一试剂的纯物量按“加入批次”分开记账：
# - 单容器：初始液体批次键为试剂编号，加液批次键为 step:<seq>；
# - 双容器：批次键再带容器编号前缀（<vessel>:<reagent> / <vessel>:step:<seq>），
#   跨容器转移时批次键与来源描述符保持不变（同一来源），并入目标时按键合并。
# 倒出 / 转移按充分混合比例同步缩放各批次；稀释不动批次；清洗清空批次。
# ---------------------------------------------------------------------------
@dataclass
class Lot:
    key: str
    reagent_id: str
    amount: float          # 该批次当前仍存的纯物体积（mL）
    source: dict           # 来源描述符（{"type": "initial", ...} / {"type": "add", ...}）


# ---------------------------------------------------------------------------
# 规则触发事件（由上层引擎翻译为响应 finding）
# ---------------------------------------------------------------------------
@dataclass
class RuleEvent:
    rule: Rule
    orient: tuple[str, str]   # (落在 side_a 上的实体, 落在 side_b 上的实体)
    concs: tuple[float, float]
    contact_seconds: float | None = None
    allowed_seconds: float | None = None


@dataclass
class VesselState:
    vessel_id: str
    vessel_material: str
    total_ml: float = 0.0
    pure_ml: dict[str, float] = field(default_factory=dict)
    cleaned: bool = False
    now: float = 0.0
    # pair_key -> {rule_id: episode}
    contacts: dict[tuple[str, str], dict[str, RuleEpisode]] = field(default_factory=dict)
    # 物料来源批次：lot_key -> Lot（溯源模式下启用）
    lots: dict[str, Lot] = field(default_factory=dict)


class VesselRuntime:
    """一只容器的状态机：浓度簿记、接触片段对账与超时判定。"""

    def __init__(self, vessel_id: str, vessel_material: str, lot_mode: str | None = None):
        self.vessel_id = vessel_id
        self.vessel_material = vessel_material
        self.mat_tok = material_token(vessel_material)
        # None=不做批次记账（原预检行为）；"single"/"dual" 控制批次键与来源描述符形态
        self.lot_mode = lot_mode
        self.state = VesselState(vessel_id=vessel_id, vessel_material=vessel_material)

    # ---------------- 浓度与组分 ----------------
    def concentrations(self) -> dict[str, float]:
        s = self.state
        if s.total_ml <= _EPS:
            return {}
        return {r: p / s.total_ml * 100.0 for r, p in s.pure_ml.items() if p > _EPS}

    def snapshot_components(self) -> list[dict]:
        conc = self.concentrations()
        out = []
        for rid in sorted(conc):
            out.append(
                {
                    "reagent_id": rid,
                    "name": REAGENTS[rid].name,
                    "concentration": round(conc[rid], 4),
                    "pure_volume_ml": round(self.state.pure_ml[rid], 4),
                }
            )
        return out

    def snapshot(self) -> dict:
        s = self.state
        return {
            "liquid_volume_ml": round(s.total_ml, 4),
            "cleaned": s.cleaned,
            "components": self.snapshot_components(),
        }

    # ---------------- 初始 / 操作 ----------------
    def _lot_prefix(self) -> str:
        return f"{self.vessel_id}:" if self.lot_mode == "dual" else ""

    def _initial_lot_key(self, rid: str) -> str:
        return f"{self._lot_prefix()}{rid}"

    def _add_lot_key(self, seq: int) -> str:
        return f"{self._lot_prefix()}step:{seq}"

    def _add_lot(self, key: str, rid: str, amount: float, source: dict) -> None:
        """登记/合并一个来源批次（同一试剂再次加入须与旧来源分开：键不同即独立）。"""
        lot = self.state.lots.get(key)
        if lot is not None:
            # 同一来源批次并入（如跨容器多跳转移回到其原始容器）
            lot.amount += amount
        else:
            self.state.lots[key] = Lot(key=key, reagent_id=rid, amount=amount, source=dict(source))

    def load_initial(self, initial: list[tuple[str, float, float]]) -> None:
        s = self.state
        for rid, conc, vol in initial:
            amount = conc * vol / 100.0
            s.pure_ml[rid] = s.pure_ml.get(rid, 0.0) + amount
            s.total_ml += vol
            if self.lot_mode is not None:
                source = {"type": "initial", "reagent_id": rid}
                if self.lot_mode == "dual":
                    source = {"type": "initial", "vessel_id": self.vessel_id, "reagent_id": rid}
                self._add_lot(self._initial_lot_key(rid), rid, amount, source)
        if initial:
            s.cleaned = False

    def advance(self, now: float) -> None:
        self.state.now = now

    def apply_add(self, rid: str, concentration: float, volume_ml: float, source: dict | None = None) -> None:
        s = self.state
        amount = concentration * volume_ml / 100.0
        s.cleaned = False
        s.pure_ml[rid] = s.pure_ml.get(rid, 0.0) + amount
        s.total_ml += volume_ml
        if self.lot_mode is not None and source is not None:
            self._add_lot(source["key"], rid, amount, source["descriptor"])

    def apply_dilute(self, volume_ml: float) -> None:
        # 稀释只加纯水：纯物量与来源批次均不变（稀释不产生来源）
        self.state.total_ml += volume_ml

    def apply_pour(self, volume_ml: float) -> None:
        s = self.state
        f = volume_ml / s.total_ml
        self._scale(1.0 - f)
        s.total_ml -= volume_ml
        if s.total_ml <= _EPS:
            s.total_ml = 0.0
            s.pure_ml.clear()
            s.lots.clear()

    def apply_clean(self) -> None:
        s = self.state
        s.total_ml = 0.0
        s.pure_ml.clear()
        s.cleaned = True
        s.contacts.clear()
        s.lots.clear()  # 清洗清空来源

    def transfer_out(self, volume_ml: float) -> tuple[dict[str, float], dict[str, Lot]]:
        """按充分混合比例转出 volume_ml。

        返回 (各组分转出纯物体积映射, 转出的来源批次映射)；转出侧各批次按比例同步缩减，
        批次的来源描述符随纯物量一起跨容器延续。完全转空则终止本容器全部接触与来源。
        """
        s = self.state
        f = volume_ml / s.total_ml
        moved: dict[str, float] = {}
        for rid in list(s.pure_ml.keys()):
            moved[rid] = s.pure_ml[rid] * f
        moved_lots: dict[str, Lot] = {}
        for key, lot in list(self.state.lots.items()):
            out_amount = lot.amount * f
            moved_lots[key] = Lot(key=lot.key, reagent_id=lot.reagent_id, amount=out_amount, source=dict(lot.source))
        self._scale(1.0 - f)
        s.total_ml -= volume_ml
        if s.total_ml <= _EPS:
            s.total_ml = 0.0
            s.pure_ml.clear()
            s.lots.clear()
        return moved, moved_lots

    def transfer_in(self, moved_pure: dict[str, float], moved_lots: dict[str, Lot], volume_ml: float) -> None:
        """接收一次定量转移：体积、各组分纯物量与来源批次计入本容器。"""
        s = self.state
        s.cleaned = False
        for rid, amount in moved_pure.items():
            if amount > _EPS:
                s.pure_ml[rid] = s.pure_ml.get(rid, 0.0) + amount
        for key, lot in moved_lots.items():
            if lot.amount > _EPS:
                self._add_lot(key, lot.reagent_id, lot.amount, lot.source)
        s.total_ml += volume_ml

    def _scale(self, factor: float) -> None:
        for rid in list(self.pure_ml_keys()):
            self.state.pure_ml[rid] *= factor
            if self.state.pure_ml[rid] < _EPS:
                del self.state.pure_ml[rid]
        # 倒出 / 转出：各来源批次按同一充分混合比例同步分摊
        for key, lot in list(self.state.lots.items()):
            remaining = lot.amount * factor
            if remaining < _EPS:
                del self.state.lots[key]
            else:
                lot.amount = remaining

    def pure_ml_keys(self):
        return list(self.state.pure_ml.keys())

    # ---------------- 实体对与规则 ----------------
    def _active_pairs(self) -> dict[tuple[str, str], dict[str, float]]:
        """返回当前存活的实体对及各实体当前浓度（材质侧记 inf 以通过阈值检查）。"""
        s = self.state
        conc = self.concentrations()
        present = sorted(conc.keys())
        pairs: dict[tuple[str, str], dict[str, float]] = {}

        # 试剂-试剂
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                a, b = present[i], present[j]
                pairs[(a, b)] = {a: conc[a], b: conc[b]}
        # 试剂-材质（容器内有液体时）
        if s.total_ml > _EPS:
            for r in present:
                key = tuple(sorted((r, self.mat_tok)))
                pairs[key] = {r: conc[r], self.mat_tok: float("inf")}
        return pairs

    def _match_rule(self, rule: Rule, a: str, b: str, ca: float, cb: float):
        """返回 ((side_a 上的实体, side_b 上的实体), (side_a 浓度, side_b 浓度))，不匹配返回 None。"""
        if _side_matches(rule.side_a, a, ca) and _side_matches(rule.side_b, b, cb):
            return (a, b), (ca, cb)
        if _side_matches(rule.side_a, b, cb) and _side_matches(rule.side_b, a, ca):
            return (b, a), (cb, ca)
        return None

    def _rules_for_pair(self, key: tuple[str, str], pair_concs: dict[str, float]):
        a, b = key
        matched = []
        for rule in RULES:
            orient = self._match_rule(rule, a, b, pair_concs[a], pair_concs[b])
            if orient is not None:
                (ta, tb), (cc_a, cc_b) = orient
                matched.append((rule, (ta, tb), (cc_a, cc_b)))
        return matched

    # ---------------- 接触对账 ----------------
    def reconcile(self) -> list[RuleEvent]:
        """在操作完成后：建立/终止接触片段，立即类规则在片段首次建立时触发。

        两侧既有且仍达阈值的接触计时继续（不产生事件）；仅新达阈值的接触
        从当前时刻起计时；跌破阈值的片段结束。
        """
        s = self.state
        active = self._active_pairs()
        events: list[RuleEvent] = []

        # 容器变空 / 组分消失：清理不再存活的对
        for key in list(s.contacts.keys()):
            if key not in active:
                del s.contacts[key]

        for key, pair_concs in active.items():
            episodes = s.contacts.setdefault(key, {})
            matched = self._rules_for_pair(key, pair_concs)
            matched_ids = {r.id for r, _, _ in matched}

            # 阈值跌破 -> 结束该规则的片段
            for rid in list(episodes.keys()):
                if rid not in matched_ids:
                    del episodes[rid]

            for rule, orient, concs in matched:
                is_new_episode = rule.id not in episodes
                if is_new_episode:
                    episodes[rule.id] = RuleEpisode(rule_id=rule.id, start=s.now)

                # 立即禁配 / 立即材质不相容：片段首次建立即触发
                if is_new_episode and rule.max_contact_seconds is None:
                    events.append(RuleEvent(rule=rule, orient=orient, concs=concs))
        return events

    # ---------------- 超时检查 ----------------
    def check_overtime(self, effective_limit) -> list[RuleEvent]:
        """检查已建立接触是否在时间推进后超过允许接触秒数（严格大于）。"""
        s = self.state
        active = self._active_pairs()
        events: list[RuleEvent] = []
        for key, episodes in s.contacts.items():
            pair_concs = active.get(key)
            if pair_concs is None:
                continue
            matched = {r.id: (r, orient, concs) for r, orient, concs in self._rules_for_pair(key, pair_concs)}
            for rid, ep in episodes.items():
                if ep.reported:
                    continue
                rule, orient, concs = matched.get(rid, (None, None, None))
                if rule is None or rule.max_contact_seconds is None:
                    continue
                allowed = effective_limit(rule, key)
                elapsed = s.now - ep.start
                if elapsed > allowed + _EPS:
                    ep.reported = True
                    events.append(
                        RuleEvent(
                            rule=rule,
                            orient=orient,
                            concs=concs,
                            contact_seconds=elapsed,
                            allowed_seconds=allowed,
                        )
                    )
        return events
