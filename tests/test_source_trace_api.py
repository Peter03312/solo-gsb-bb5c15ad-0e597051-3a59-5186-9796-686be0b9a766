"""单容器风险来源溯源 /api/v1/precheck-source-trace 端到端测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
PATH = "/api/v1/precheck-source-trace"
LEGACY = "/api/v1/precheck"


def post(payload):
    return client.post(PATH, json=payload)


def by_lot(rs, reagent_id):
    rg = next(r for r in rs["reagents"] if r["reagent_id"] == reagent_id)
    return rg, {(lot["origin"], lot["seq"]): (lot["pure_volume_ml"], lot["fraction"]) for lot in rg["lots"]}


class TestTraceShape:
    def test_response_wraps_original_precheck(self):
        payload = {
            "vessel_material": "glass", "temperature_c": 22,
            "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
            "steps": [{"seq": 1, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 50}],
        }
        d = post(payload).json()
        legacy = client.post(LEGACY, json=payload).json()
        assert d["precheck"] == legacy
        assert len(d["risk_sources"]) == len(legacy["findings"]) == 1

    def test_safe_request_has_empty_sources(self):
        d = post({
            "vessel_material": "hdpe", "temperature_c": 20,
            "steps": [{"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 10, "volume_ml": 100}],
        }).json()
        assert d["precheck"]["safe"] is True
        assert d["risk_sources"] == []

    def test_sources_aligned_with_findings(self):
        payload = {
            "vessel_material": "glass", "temperature_c": 22,
            "initial_liquid": [
                {"reagent_id": "naocl", "concentration": 12, "volume_ml": 50},
                {"reagent_id": "hcl", "concentration": 30, "volume_ml": 50},
            ],
            "steps": [],
        }
        d = post(payload).json()
        rs = d["risk_sources"][0]
        assert rs["finding_index"] == 0
        assert rs["seq"] == 0 and rs["rule_id"] == "F01"
        assert {r["reagent_id"] for r in rs["reagents"]} == {"hcl", "naocl"}

    def test_illegal_input_whole_order_rejected(self):
        r = post({
            "vessel_material": "glass", "temperature_c": 20,
            "steps": [{"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 0, "volume_ml": 0}],
        })
        assert r.status_code == 400
        body = r.json()
        assert "risk_sources" not in body and "precheck" not in body
        codes = {e["code"] for e in body["details"]}
        assert {"CONCENTRATION_OUT_OF_RANGE", "INVALID_VOLUME"} <= codes


class TestInitialAndSteps:
    def test_initial_liquid_identified_by_reagent(self):
        d = post({
            "vessel_material": "hdpe", "temperature_c": 20,
            "initial_liquid": [
                {"reagent_id": "naocl", "concentration": 12, "volume_ml": 50},
                {"reagent_id": "hcl", "concentration": 30, "volume_ml": 50},
            ],
            "steps": [],
        }).json()
        rs = d["risk_sources"][0]
        hcl, lots = by_lot(rs, "hcl")
        assert hcl["pure_volume_ml"] == pytest.approx(15.0)
        assert lots[("initial", None)] == (pytest.approx(15.0), 1.0)

    def test_add_identified_by_step_seq(self):
        d = post({
            "vessel_material": "glass", "temperature_c": 22,
            "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
            "steps": [{"seq": 1, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 50}],
        }).json()
        rs = d["risk_sources"][0]
        _, hcl_lots = by_lot(rs, "hcl")
        _, ocl_lots = by_lot(rs, "naocl")
        assert hcl_lots[("step", 1)] == (pytest.approx(15.0), 1.0)
        assert ocl_lots[("initial", None)] == (pytest.approx(10.0), 1.0)

    def test_same_reagent_readded_is_separate_lot(self):
        # 3% 盐酸 100mL（酸 1.5% 不触发），再加 30% 盐酸 50mL 触发；两个批次分开
        d = post({
            "vessel_material": "glass", "temperature_c": 22,
            "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "hcl", "concentration": 3, "volume_ml": 100},
                {"seq": 2, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 50, "at_seconds": 10},
            ],
        }).json()
        rs = d["risk_sources"][0]
        hcl, lots = by_lot(rs, "hcl")
        assert hcl["pure_volume_ml"] == pytest.approx(18.0)
        assert lots[("step", 1)][0] == pytest.approx(3.0)
        assert lots[("step", 2)][0] == pytest.approx(15.0)
        assert lots[("step", 1)][1] + lots[("step", 2)][1] == pytest.approx(1.0)
        assert lots[("step", 2)][1] == pytest.approx(15 / 18, abs=1e-6)

    def test_fractions_sum_to_one_after_pour(self):
        d = post({
            "vessel_material": "glass", "temperature_c": 22,
            "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "hcl", "concentration": 3, "volume_ml": 100},
                {"seq": 2, "op": "pour", "volume_ml": 40, "at_seconds": 5},
                {"seq": 3, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 50, "at_seconds": 10},
            ],
        }).json()
        rs = d["risk_sources"][0]
        hcl, lots = by_lot(rs, "hcl")
        # seq1 批次 3mL pure 随 40/200 倒出后剩 3×0.8=2.4；seq3 新批次 15
        assert lots[("step", 1)][0] == pytest.approx(2.4)
        assert lots[("step", 3)][0] == pytest.approx(15.0)
        assert hcl["pure_volume_ml"] == pytest.approx(17.4)
        assert sum(v[1] for v in lots.values()) == pytest.approx(1.0)


class TestOperations:
    def test_dilution_creates_no_source(self):
        d = post({
            "vessel_material": "hdpe", "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 10, "volume_ml": 100},
                {"seq": 2, "op": "dilute", "volume_ml": 100, "at_seconds": 5},
            ],
        }).json()
        assert d["risk_sources"] == []

    def test_pour_apportions_proportionally(self):
        # 同试剂两批加入后倒出一半：各批次等比缩减
        d = post({
            "vessel_material": "hdpe", "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 10, "volume_ml": 100},
                {"seq": 2, "op": "add", "reagent_id": "nacl", "concentration": 20, "volume_ml": 100, "at_seconds": 5},
                {"seq": 3, "op": "pour", "volume_ml": 100, "at_seconds": 10},
            ],
        }).json()
        # nacl 不触发任何规则，用 final_state 间接核对无来源泄漏；此处直接查台账行为
        assert d["risk_sources"] == []
        comps = {c["reagent_id"]: c for c in d["precheck"]["final_state"]["components"]}
        assert comps["nacl"]["pure_volume_ml"] == pytest.approx(15.0)

    def test_clean_clears_sources(self):
        d = post({
            "vessel_material": "hdpe", "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "naocl", "concentration": 10, "volume_ml": 100},
                {"seq": 2, "op": "clean", "at_seconds": 10},
                {"seq": 3, "op": "add", "reagent_id": "hcl", "concentration": 10, "volume_ml": 50, "at_seconds": 20},
            ],
        }).json()
        # 清洗后只有 hcl，二者不构成禁配
        assert d["precheck"]["safe"] is True
        assert d["risk_sources"] == []


class TestOvertimeSnapshot:
    def test_overtime_snapshot_at_trigger_moment(self):
        d = post({
            "vessel_material": "glass", "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "naocl", "concentration": 12, "volume_ml": 100},
                {"seq": 2, "op": "add", "reagent_id": "nh4cl", "concentration": 20, "volume_ml": 100, "at_seconds": 60},
                {"seq": 3, "op": "hold", "at_seconds": 700},
            ],
        }).json()
        rs = d["risk_sources"][0]
        assert rs["category"] == "overtime_contact" and rs["seq"] == 3
        totals = {r["reagent_id"]: r["pure_volume_ml"] for r in rs["reagents"]}
        assert totals == {"naocl": pytest.approx(12.0), "nh4cl": pytest.approx(20.0)}

    def test_overtime_uses_pre_operation_ownership(self):
        # 超时在 seq3（pour）操作前结算：快照保留满量，随后倒空不改写已触发风险
        d = post({
            "vessel_material": "glass", "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "naocl", "concentration": 12, "volume_ml": 100},
                {"seq": 2, "op": "add", "reagent_id": "nh4cl", "concentration": 20, "volume_ml": 100, "at_seconds": 60},
                {"seq": 3, "op": "pour", "volume_ml": 200, "at_seconds": 700},
            ],
        }).json()
        rs = d["risk_sources"][0]
        assert rs["category"] == "overtime_contact"
        totals = {r["reagent_id"]: r["pure_volume_ml"] for r in rs["reagents"]}
        assert totals["naocl"] == pytest.approx(12.0)
        assert totals["nh4cl"] == pytest.approx(20.0)
        assert d["precheck"]["final_state"]["liquid_volume_ml"] == 0.0

    def test_material_overtime_also_traced(self):
        d = post({
            "vessel_material": "glass", "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "naoh", "concentration": 30, "volume_ml": 100},
                {"seq": 2, "op": "hold", "at_seconds": 2000},
            ],
        }).json()
        rs = d["risk_sources"][0]
        assert rs["category"] == "overtime_contact" and rs["rule_id"] == "M04"
        assert rs["material_id"] == "glass"
        assert [r["reagent_id"] for r in rs["reagents"]] == ["naoh"]


class TestMaterialRisk:
    def test_material_marks_vessel_material_no_material_share(self):
        d = post({
            "vessel_material": "glass", "temperature_c": 20,
            "steps": [{"seq": 1, "op": "add", "reagent_id": "hf", "concentration": 10, "volume_ml": 50}],
        }).json()
        rs = d["risk_sources"][0]
        assert rs["category"] == "material_incompatible"
        assert rs["material_id"] == "glass" and rs["vessel_material"] == "glass"
        assert [r["reagent_id"] for r in rs["reagents"]] == ["hf"]
        hf, lots = by_lot(rs, "hf")
        assert hf["pure_volume_ml"] == pytest.approx(5.0)
        assert lots[("step", 1)] == (pytest.approx(5.0), 1.0)

    def test_reagent_reagent_risk_has_null_material(self):
        d = post({
            "vessel_material": "hdpe", "temperature_c": 22,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "naocl", "concentration": 10, "volume_ml": 100},
                {"seq": 2, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 50, "at_seconds": 5},
            ],
        }).json()
        assert d["risk_sources"][0]["material_id"] is None


class TestLegacyCompatibility:
    def test_original_precheck_response_unchanged(self):
        payload = {
            "vessel_material": "glass", "temperature_c": 22,
            "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
            "steps": [{"seq": 1, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 50}],
        }
        d = client.post(LEGACY, json=payload).json()
        assert set(d) == {
            "safe", "blocked", "first_blocking_seq", "ruleset_version", "vessel_material",
            "temperature_c", "findings", "final_state", "steps",
        }
        assert all(not k.startswith("_") for f in d["findings"] for k in f)
        assert "risk_sources" not in d
