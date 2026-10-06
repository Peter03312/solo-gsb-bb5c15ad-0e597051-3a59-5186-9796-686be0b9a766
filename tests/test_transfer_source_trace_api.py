"""双容器风险来源溯源 /api/v1/transfer-source-trace 端到端测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
PATH = "/api/v1/transfer-source-trace"
LEGACY = "/api/v1/transfer-precheck"


def hdpe(vid, initial=None):
    return {"vessel_id": vid, "vessel_material": "hdpe", "initial_liquid": initial or []}


def post(payload):
    return client.post(PATH, json=payload)


def lots_of(rs, reagent_id):
    rg = next(r for r in rs["reagents"] if r["reagent_id"] == reagent_id)
    return rg, {lot["vessel_id"]: (lot["pure_volume_ml"], lot["fraction"]) for lot in rg["lots"]}


class TestTraceShape:
    def test_response_wraps_original_transfer_precheck(self):
        payload = {
            "vessels": [
                {"vessel_id": "src", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "hcl", "concentration": 30, "volume_ml": 100}]},
                {"vessel_id": "dst", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}]},
            ],
            "temperature_c": 22,
            "steps": [{"seq": 1, "op": "transfer", "from_vessel": "src", "to_vessel": "dst",
                       "volume_ml": 50, "at_seconds": 5}],
        }
        d = post(payload).json()
        legacy = client.post(LEGACY, json=payload).json()
        assert d["precheck"] == legacy
        assert len(d["risk_sources"]) == len(legacy["findings"]) == 1
        assert all(not k.startswith("_") for f in legacy["findings"] for k in f)

    def test_safe_transfer_empty_sources(self):
        d = post({
            "vessels": [
                hdpe("A", [{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}]),
                hdpe("B"),
            ],
            "temperature_c": 20,
            "steps": [{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                       "volume_ml": 40, "at_seconds": 10}],
        }).json()
        assert d["precheck"]["safe"] is True
        assert d["risk_sources"] == []

    def test_illegal_input_whole_order_rejected(self):
        r = post({
            "vessels": [hdpe("A"), hdpe("A")],
            "temperature_c": 20,
            "steps": [],
        })
        assert r.status_code == 400
        body = r.json()
        assert "risk_sources" not in body
        assert any(e["code"] == "DUPLICATE_VESSEL_ID" for e in body["details"])


class TestInitialAndTransfer:
    def test_initial_identified_by_vessel_and_reagent(self):
        d = post({
            "vessels": [
                hdpe("A", [
                    {"reagent_id": "naocl", "concentration": 12, "volume_ml": 50},
                    {"reagent_id": "hcl", "concentration": 30, "volume_ml": 50},
                ]),
                hdpe("B"),
            ],
            "temperature_c": 22,
            "steps": [],
        }).json()
        rs = d["risk_sources"][0]
        assert rs["seq"] == 0 and rs["vessel_id"] == "A"
        hcl, lots = lots_of(rs, "hcl")
        assert hcl["pure_volume_ml"] == pytest.approx(15.0)
        assert lots["A"] == (pytest.approx(15.0), 1.0)
        assert rs["reagents"][0]["lots"][0]["origin"] == "vessel"

    def test_transfer_carries_origin_vessel_identity(self):
        d = post({
            "vessels": [
                {"vessel_id": "acid-bottle", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "hcl", "concentration": 30, "volume_ml": 100}]},
                {"vessel_id": "bleach-tank", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}]},
            ],
            "temperature_c": 22,
            "steps": [{"seq": 1, "op": "transfer", "from_vessel": "acid-bottle",
                       "to_vessel": "bleach-tank", "volume_ml": 50, "at_seconds": 30}],
        }).json()
        rs = d["risk_sources"][0]
        assert rs["vessel_id"] == "bleach-tank" and rs["seq"] == 1
        _, hcl_lots = lots_of(rs, "hcl")
        _, ocl_lots = lots_of(rs, "naocl")
        assert hcl_lots["acid-bottle"] == (pytest.approx(15.0), 1.0)
        assert ocl_lots["bleach-tank"] == (pytest.approx(10.0), 1.0)

    def test_same_reagent_from_two_vessels_kept_separate(self):
        # B 初始 naocl 12% 100 + hcl 3% 100（酸 1.5% < 5% 阈值，不触发）；
        # A 30% hcl 100 全部转入 B：hcl 两来源（B 3mL / A 30mL）首次共同触发 F01
        d = post({
            "vessels": [
                hdpe("A", [{"reagent_id": "hcl", "concentration": 30, "volume_ml": 100}]),
                hdpe("B", [
                    {"reagent_id": "naocl", "concentration": 12, "volume_ml": 100},
                    {"reagent_id": "hcl", "concentration": 3, "volume_ml": 100},
                ]),
            ],
            "temperature_c": 22,
            "steps": [{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                       "volume_ml": 100, "at_seconds": 5}],
        }).json()
        assert [(f["seq"], f["vessel_id"]) for f in d["precheck"]["findings"]] == [(1, "B")]
        rs = d["risk_sources"][0]
        hcl, lots = lots_of(rs, "hcl")
        assert hcl["pure_volume_ml"] == pytest.approx(33.0)
        assert lots["A"][0] == pytest.approx(30.0)
        assert lots["B"][0] == pytest.approx(3.0)
        assert lots["A"][1] + lots["B"][1] == pytest.approx(1.0)
        assert lots["A"][1] == pytest.approx(30 / 33, abs=1e-6)

    def test_partial_transfer_apportions_source_proportionally(self):
        # A 30% hcl 100mL；B 10% naocl 100mL：
        # seq1 转 10mL A->B（B 内酸 3/110≈2.7% < 5%，不触发）；
        # seq2 再转 50mL A->B（累计转入 60mL，酸 18/160=11.25% 首次触发），
        # 快照中 A 来源 hcl pure=18mL；A 剩余 40mL * 30% = 12mL。
        d = post({
            "vessels": [
                hdpe("A", [{"reagent_id": "hcl", "concentration": 30, "volume_ml": 100}]),
                hdpe("B", [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}]),
            ],
            "temperature_c": 22,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                 "volume_ml": 10, "at_seconds": 5},
                {"seq": 2, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                 "volume_ml": 50, "at_seconds": 10},
            ],
        }).json()
        assert len(d["risk_sources"]) == 1
        rs2 = d["risk_sources"][0]
        assert rs2["finding_index"] == 0 and rs2["seq"] == 2
        _, hcl_lots = lots_of(rs2, "hcl")
        assert hcl_lots["A"][0] == pytest.approx(18.0)
        # A 源容器内尚留 40mL 液体 * 30% = 12mL pure
        a_state = {v["vessel_id"]: v for v in d["precheck"]["final_states"]}["A"]
        assert a_state["components"][0]["pure_volume_ml"] == pytest.approx(12.0)

    def test_back_and_forth_merges_same_origin_lot(self):
        # A 初始 hcl 30% 100；转 60 到空 B，再从 B 转回 20 到 A：
        # A 内 hcl 两个持有批次（A 原留 / A 经 B 回流）同源于 ("vessel","A","hcl")，合并
        d = post({
            "vessels": [
                hdpe("A", [{"reagent_id": "hcl", "concentration": 30, "volume_ml": 100}]),
                hdpe("B"),
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                 "volume_ml": 60, "at_seconds": 1},
                {"seq": 2, "op": "transfer", "from_vessel": "B", "to_vessel": "A",
                 "volume_ml": 20, "at_seconds": 2},
            ],
        }).json()
        assert d["precheck"]["safe"] is True
        states = {v["vessel_id"]: v for v in d["precheck"]["final_states"]}
        # A: 40mL 原液(pure12) + 回流20mL(pure6) = 60mL，pure 18
        assert states["A"]["components"][0]["pure_volume_ml"] == pytest.approx(18.0)
        assert states["B"]["components"][0]["pure_volume_ml"] == pytest.approx(12.0)


class TestOvertime:
    def test_overtime_snapshot_pre_transfer_ownership(self):
        # A 初始 naocl+nh4cl；seq1 hold 400；seq2 转 100/200 mL 到空 B at500（A 部分转出）；
        # seq3 hold 1200：A 旧片段自 0 起 1200s 超时、B 新片段自 500 起 700s 超时
        d = post({
            "vessels": [
                hdpe("A", [
                    {"reagent_id": "naocl", "concentration": 12, "volume_ml": 100},
                    {"reagent_id": "nh4cl", "concentration": 20, "volume_ml": 100},
                ]),
                hdpe("B"),
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "hold", "at_seconds": 400},
                {"seq": 2, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                 "volume_ml": 100, "at_seconds": 500},
                {"seq": 3, "op": "hold", "at_seconds": 1200},
            ],
        }).json()
        by_vessel = {rs["vessel_id"]: rs for rs in d["risk_sources"]}
        assert set(by_vessel) == {"A", "B"}
        for rs in by_vessel.values():
            assert rs["seq"] == 3 and rs["category"] == "overtime_contact"
        # A 侧（留 100mL）：naocl 6 / nh4cl 10；B 侧（100mL）：同为 6 / 10
        a_totals = {r["reagent_id"]: r["pure_volume_ml"] for r in by_vessel["A"]["reagents"]}
        b_totals = {r["reagent_id"]: r["pure_volume_ml"] for r in by_vessel["B"]["reagents"]}
        assert a_totals == {"naocl": pytest.approx(6.0), "nh4cl": pytest.approx(10.0)}
        assert b_totals == a_totals
        for rs in by_vessel.values():
            for rg in rs["reagents"]:
                assert {lot["vessel_id"] for lot in rg["lots"]} == {"A"}

    def test_emptied_source_does_not_rewrite_triggered_risk(self):
        # seq1 hold 601 先超时（A 满量），seq2 同刻把 A 全部转空：已触发风险快照不变
        d = post({
            "vessels": [
                hdpe("A", [
                    {"reagent_id": "naocl", "concentration": 12, "volume_ml": 100},
                    {"reagent_id": "nh4cl", "concentration": 20, "volume_ml": 100},
                ]),
                hdpe("B"),
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "hold", "at_seconds": 601},
                {"seq": 2, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                 "volume_ml": 200, "at_seconds": 601},
            ],
        }).json()
        overtime_a = [rs for rs in d["risk_sources"] if rs["vessel_id"] == "A"]
        assert len(overtime_a) == 1 and overtime_a[0]["seq"] == 1
        totals = {r["reagent_id"]: r["pure_volume_ml"] for r in overtime_a[0]["reagents"]}
        assert totals == {"naocl": pytest.approx(12.0), "nh4cl": pytest.approx(20.0)}
        states = {v["vessel_id"]: v for v in d["precheck"]["final_states"]}
        assert states["A"]["liquid_volume_ml"] == 0.0


class TestMaterial:
    def test_material_risk_marks_vessel_and_material(self):
        d = post({
            "vessels": [
                hdpe("A", [{"reagent_id": "hf", "concentration": 10, "volume_ml": 50}]),
                {"vessel_id": "B", "vessel_material": "glass", "initial_liquid": []},
            ],
            "temperature_c": 20,
            "steps": [{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                       "volume_ml": 50, "at_seconds": 5}],
        }).json()
        rs = d["risk_sources"][0]
        assert rs["vessel_id"] == "B" and rs["vessel_material"] == "glass"
        assert rs["material_id"] == "glass"
        assert [r["reagent_id"] for r in rs["reagents"]] == ["hf"]
        _, lots = lots_of(rs, "hf")
        assert lots["A"] == (pytest.approx(5.0), 1.0)

    def test_initial_material_risk_in_vessel(self):
        d = post({
            "vessels": [
                hdpe("A"),
                {"vessel_id": "B", "vessel_material": "glass",
                 "initial_liquid": [{"reagent_id": "hf", "concentration": 10, "volume_ml": 50}]},
            ],
            "temperature_c": 20,
            "steps": [],
        }).json()
        rs = d["risk_sources"][0]
        assert rs["seq"] == 0 and rs["vessel_id"] == "B" and rs["material_id"] == "glass"
        _, lots = lots_of(rs, "hf")
        assert lots["B"][0] == pytest.approx(5.0)


class TestLegacyCompatibility:
    def test_original_transfer_response_unchanged(self):
        d = client.post(LEGACY, json={
            "vessels": [hdpe("A"), hdpe("B")],
            "temperature_c": 20,
            "steps": [],
        }).json()
        assert set(d) == {
            "safe", "blocked", "first_blocking_seq", "first_blocking_vessel_id",
            "ruleset_version", "temperature_c", "findings", "steps", "final_states",
        }
        assert "risk_sources" not in d
