"""风险物料来源溯源接口测试（双容器 POST /api/v1/transfer-precheck-provenance）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def post(payload):
    return client.post("/api/v1/transfer-precheck-provenance", json=payload)


def _by_reagent(trace):
    return {rs["reagent_id"]: rs for rs in trace["reagent_sources"]}


class TestTransferProvenanceBasics:
    def test_embeds_original_transfer_precheck(self):
        payload = {
            "vessels": [
                {"vessel_id": "A", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "hcl", "concentration": 30, "volume_ml": 100}]},
                {"vessel_id": "B", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}]},
            ],
            "temperature_c": 22,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                 "volume_ml": 50, "at_seconds": 30},
            ],
        }
        d = post(payload).json()
        pre = client.post("/api/v1/transfer-precheck", json=payload).json()
        assert d["precheck"] == pre
        t = d["risk_sources"][0]
        assert t["finding_index"] == 0
        assert t["vessel_id"] == "B"
        assert t["seq"] == 1

    def test_safe_empty(self):
        d = post({
            "vessels": [
                {"vessel_id": "A", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "nacl", "concentration": 5, "volume_ml": 100}]},
                {"vessel_id": "B", "vessel_material": "hdpe", "initial_liquid": []},
            ],
            "temperature_c": 20,
            "steps": [{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                       "volume_ml": 30}],
        }).json()
        assert d["precheck"]["safe"] is True
        assert d["risk_sources"] == []

    def test_invalid_input_whole_order_rejected(self):
        r = post({
            "vessels": [
                {"vessel_id": "A", "vessel_material": "hdpe", "initial_liquid": []},
                {"vessel_id": "A", "vessel_material": "hdpe", "initial_liquid": []},
            ],
            "temperature_c": 20,
            "steps": [{"seq": 1, "op": "hold", "at_seconds": 10}],
        })
        assert r.status_code == 400
        assert "DUPLICATE_VESSEL_ID" in {x["code"] for x in r.json()["details"]}

    def test_initial_risk_seq_zero_uses_vessel_and_reagent_id(self):
        d = post({
            "vessels": [
                {"vessel_id": "X", "vessel_material": "hdpe", "initial_liquid": []},
                {"vessel_id": "Y", "vessel_material": "hdpe",
                 "initial_liquid": [
                     {"reagent_id": "naocl", "concentration": 12, "volume_ml": 50},
                     {"reagent_id": "hcl", "concentration": 30, "volume_ml": 50},
                 ]},
            ],
            "temperature_c": 20, "steps": [],
        }).json()
        t = d["risk_sources"][0]
        assert t["seq"] == 0 and t["vessel_id"] == "Y"
        rs = _by_reagent(t)
        assert rs["naocl"]["sources"][0]["source"] == {
            "type": "initial", "vessel_id": "Y", "reagent_id": "naocl", "seq": None
        }


class TestTransferSources:
    def _acid_bleach(self, volume=50.0):
        return {
            "vessels": [
                {"vessel_id": "A", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "hcl", "concentration": 30, "volume_ml": 100}]},
                {"vessel_id": "B", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}]},
            ],
            "temperature_c": 22,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                 "volume_ml": volume, "at_seconds": 30},
            ],
        }

    def test_transfer_carries_source_and_proportional_split(self):
        d = post(self._acid_bleach(50)).json()
        rs = _by_reagent(d["risk_sources"][0])
        # A 的 hcl pure30 转出一半：B 中 hcl pure15，来源标注 (A 容器, hcl 试剂)
        hcl = rs["hcl"]
        assert hcl["total_pure_volume_ml"] == 15.0
        assert hcl["sources"] == [
            {"source": {"type": "initial", "vessel_id": "A", "reagent_id": "hcl", "seq": None},
             "pure_volume_ml": 15.0, "fraction": 1.0}
        ]
        # A 自身剩余 15（不在本 finding 容器内，故不出现在来源里——只列触发容器内仍存的量）
        assert rs["naocl"]["sources"][0]["source"]["vessel_id"] == "B"

    def test_multiple_partial_transfers_merge_same_source_in_destination(self):
        # A: hcl 30%×100；step1 转50(hcl pure15 入 B)，step2 再转25(剩50的一半 pure7.5)。
        # B 初始 naocl 10%×100，step1 即触发（记录 15）；step2 不再产生新禁配 finding。
        payload = self._acid_bleach(50)
        payload["steps"].append(
            {"seq": 2, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
             "volume_ml": 25, "at_seconds": 40}
        )
        d = post(payload).json()
        assert [f["rule_id"] for f in d["precheck"]["findings"]] == ["F01"]
        # 已触发风险的来源不被后续转移改写
        hcl = _by_reagent(d["risk_sources"][0])["hcl"]
        assert hcl["total_pure_volume_ml"] == 15.0

    def test_same_reagent_from_two_vessels_stays_separate(self):
        # B 初始 naocl 2%×100(pure2, 低于3%阈值，初始不触发) + hcl 30%×100；
        # A 初始 naocl 10%×100，全量转入 B：naocl pure12 -> 4% 触发，来源两条。
        d = post({
            "vessels": [
                {"vessel_id": "A", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}]},
                {"vessel_id": "B", "vessel_material": "hdpe",
                 "initial_liquid": [
                     {"reagent_id": "naocl", "concentration": 2, "volume_ml": 100},
                     {"reagent_id": "hcl", "concentration": 30, "volume_ml": 100},
                 ]},
            ],
            "temperature_c": 22,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                 "volume_ml": 100, "at_seconds": 10},
            ],
        }).json()
        na = _by_reagent(d["risk_sources"][0])["naocl"]
        assert na["total_pure_volume_ml"] == 12.0
        by_vessel = {s["source"]["vessel_id"]: s for s in na["sources"]}
        assert by_vessel["A"]["pure_volume_ml"] == 10.0 and by_vessel["A"]["fraction"] == round(10 / 12, 6)
        assert by_vessel["B"]["pure_volume_ml"] == 2.0 and by_vessel["B"]["fraction"] == round(2 / 12, 6)

    def test_source_order_follows_submission_order(self):
        # 提交顺序 B 在前（含转入来源 A 的同试剂）：来源按容器提交序，B 自身来源排在 A 之前
        d = post({
            "vessels": [
                {"vessel_id": "B", "vessel_material": "hdpe",
                 "initial_liquid": [
                     {"reagent_id": "naocl", "concentration": 2, "volume_ml": 100},
                     {"reagent_id": "hcl", "concentration": 30, "volume_ml": 100},
                 ]},
                {"vessel_id": "A", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}]},
            ],
            "temperature_c": 22,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                 "volume_ml": 100, "at_seconds": 10},
            ],
        }).json()
        vessels = [s["source"]["vessel_id"] for s in _by_reagent(d["risk_sources"][0])["naocl"]["sources"]]
        assert vessels == ["B", "A"]


class TestTransferMaterialAndOvertime:
    def test_material_marks_trigger_vessel_and_material(self):
        d = post({
            "vessels": [
                {"vessel_id": "F", "vessel_material": "glass",
                 "initial_liquid": [{"reagent_id": "hf", "concentration": 10, "volume_ml": 40}]},
                {"vessel_id": "X", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "nacl", "concentration": 5, "volume_ml": 100}]},
            ],
            "temperature_c": 20, "steps": [],
        }).json()
        t = d["risk_sources"][0]
        assert t["material"] == {"vessel_id": "F", "material_id": "glass"}
        assert set(t["material"].keys()) == {"vessel_id", "material_id"}

    def test_overtime_snapshot_taken_before_transfer_and_not_emptied(self):
        # A 中 h2o2+nacl（T01，1800s），step1 hold 2000 超时；step2 把 A 全部转入 B。
        # 超时来源必须保留转空前的纯物量（20 / 10），finding 容器为 A。
        d = post({
            "vessels": [
                {"vessel_id": "A", "vessel_material": "hdpe",
                 "initial_liquid": [
                     {"reagent_id": "h2o2", "concentration": 20, "volume_ml": 100},
                     {"reagent_id": "nacl", "concentration": 10, "volume_ml": 100},
                 ]},
                {"vessel_id": "B", "vessel_material": "hdpe", "initial_liquid": []},
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "hold", "at_seconds": 2000},
                {"seq": 2, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                 "volume_ml": 200, "at_seconds": 2001},
            ],
        }).json()
        t = next(x for x in d["risk_sources"] if x["rule_id"] == "T01")
        assert t["seq"] == 1 and t["vessel_id"] == "A"
        rs = _by_reagent(t)
        assert rs["h2o2"]["total_pure_volume_ml"] == 20.0
        assert rs["nacl"]["total_pure_volume_ml"] == 10.0
        # 触发容器 A 的两条来源均标注为 A 初始
        for rid in ("h2o2", "nacl"):
            assert rs[rid]["sources"][0]["source"]["vessel_id"] == "A"

    def test_transferred_into_material_incompatible_vessel_traces_reagent_origin(self):
        # HF 从 PTFE 容器 A 转入玻璃容器 B：材质风险在 B 触发，液体来源仍是 A 的初始 HF
        d = post({
            "vessels": [
                {"vessel_id": "A", "vessel_material": "ptfe",
                 "initial_liquid": [{"reagent_id": "hf", "concentration": 10, "volume_ml": 100}]},
                {"vessel_id": "B", "vessel_material": "glass", "initial_liquid": []},
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                 "volume_ml": 50, "at_seconds": 30},
            ],
        }).json()
        m = next(t for t in d["risk_sources"] if t["category"] == "material_incompatible")
        assert m["vessel_id"] == "B"
        assert m["material"] == {"vessel_id": "B", "material_id": "glass"}
        hf = _by_reagent(m)["hf"]
        assert hf["total_pure_volume_ml"] == 5.0
        assert hf["sources"][0]["source"] == {
            "type": "initial", "vessel_id": "A", "reagent_id": "hf", "seq": None
        }
