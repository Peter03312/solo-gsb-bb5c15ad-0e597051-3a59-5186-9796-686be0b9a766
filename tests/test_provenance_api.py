"""风险物料来源溯源接口测试（单容器 POST /api/v1/precheck-provenance）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def post(payload):
    return client.post("/api/v1/precheck-provenance", json=payload)


def _by_reagent(trace):
    return {rs["reagent_id"]: rs for rs in trace["reagent_sources"]}


class TestProvenanceBasics:
    def test_response_embeds_original_precheck(self):
        payload = {
            "vessel_material": "glass",
            "temperature_c": 22,
            "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 50},
            ],
        }
        d = post(payload).json()
        pre = client.post("/api/v1/precheck", json=payload).json()
        assert d["precheck"] == pre
        assert d["precheck"]["blocked"] is True

    def test_finding_index_aligned_and_sorted(self):
        d = post({
            "vessel_material": "glass",  # glass + 初始 hcl/hf 等多风险时验证对齐
            "temperature_c": 22,
            "initial_liquid": [{"reagent_id": "hf", "concentration": 10, "volume_ml": 50}],
            "steps": [],
        }).json()
        assert len(d["risk_sources"]) == len(d["precheck"]["findings"]) == 1
        t = d["risk_sources"][0]
        assert t["finding_index"] == 0
        assert t["seq"] == 0
        assert t["rule_id"] == d["precheck"]["findings"][0]["rule_id"]

    def test_safe_request_has_empty_sources(self):
        d = post({
            "vessel_material": "hdpe",
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 5, "volume_ml": 100},
            ],
        }).json()
        assert d["precheck"]["safe"] is True
        assert d["risk_sources"] == []

    def test_invalid_input_rejected_like_precheck(self):
        r = post({
            "vessel_material": "hdpe",
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 5, "volume_ml": 100},
                {"seq": 1, "op": "pour", "volume_ml": 10, "at_seconds": 5},
            ],
        })
        assert r.status_code == 400
        assert {x["code"] for x in r.json()["details"]} == {"DUPLICATE_STEP_SEQ"}
        assert "risk_sources" not in r.json()

    def test_malformed_body_400(self):
        r = client.post("/api/v1/precheck-provenance", json={"vessel_material": "hdpe", "bogus": 1})
        assert r.status_code == 400


class TestSourceIdentity:
    def test_initial_identified_by_reagent_add_by_seq(self):
        d = post({
            "vessel_material": "hdpe",
            "temperature_c": 22,
            "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 50},
            ],
        }).json()
        rs = _by_reagent(d["risk_sources"][0])
        assert rs["naocl"]["sources"] == [
            {"source": {"type": "initial", "reagent_id": "naocl", "vessel_id": None, "seq": None},
             "pure_volume_ml": 10.0, "fraction": 1.0}
        ]
        assert rs["hcl"]["sources"] == [
            {"source": {"type": "add", "reagent_id": None, "vessel_id": None, "seq": 1},
             "pure_volume_ml": 15.0, "fraction": 1.0}
        ]

    def test_same_reagent_readded_kept_as_separate_source(self):
        # 初始 naocl + 步骤1 再加 naocl（同试剂，旧来源/新来源分开），步骤2 加 hcl 触发
        d = post({
            "vessel_material": "hdpe",
            "temperature_c": 22,
            "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "naocl", "concentration": 10, "volume_ml": 100},
                {"seq": 2, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 40,
                 "at_seconds": 5},
            ],
        }).json()
        rs = _by_reagent(d["risk_sources"][0])
        srcs = rs["naocl"]["sources"]
        assert [s["source"]["type"] for s in srcs] == ["initial", "add"]
        assert srcs[0]["source"]["reagent_id"] == "naocl"
        assert srcs[1]["source"]["seq"] == 1
        assert srcs[0]["pure_volume_ml"] == 10.0 and srcs[1]["pure_volume_ml"] == 10.0
        assert srcs[0]["fraction"] == 0.5 and srcs[1]["fraction"] == 0.5
        assert rs["naocl"]["total_pure_volume_ml"] == 20.0

    def test_dilute_creates_no_source_but_changes_share_base_only(self):
        # 稀释只加水：先加 hcl 并稀释（hcl 跌到阈值以下，无风险），再加 naocl 触发 F01。
        # hcl 纯物量仍只有 step1 一份来源，水不产生任何来源，fraction 以该试剂纯物量为基数。
        d = post({
            "vessel_material": "hdpe",
            "temperature_c": 22,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 100},
                {"seq": 2, "op": "dilute", "volume_ml": 100, "at_seconds": 1},
                {"seq": 3, "op": "add", "reagent_id": "naocl", "concentration": 15, "volume_ml": 200,
                 "at_seconds": 2},
            ],
        }).json()
        rs = _by_reagent(d["risk_sources"][0])
        hcl = rs["hcl"]
        assert len(hcl["sources"]) == 1
        assert hcl["sources"][0]["source"] == {"type": "add", "reagent_id": None,
                                               "vessel_id": None, "seq": 1}
        assert hcl["sources"][0]["pure_volume_ml"] == 30.0
        assert hcl["sources"][0]["fraction"] == 1.0
        assert hcl["total_pure_volume_ml"] == 30.0

    def test_pour_splits_sources_proportionally(self):
        # 初始 naocl(pure10) + step1 加 naocl(pure10)，step2 倒出一半 -> 两批各剩 5
        d = post({
            "vessel_material": "hdpe",
            "temperature_c": 22,
            "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "naocl", "concentration": 10, "volume_ml": 100},
                {"seq": 2, "op": "pour", "volume_ml": 100, "at_seconds": 5},
                {"seq": 3, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 50,
                 "at_seconds": 10},
            ],
        }).json()
        rs = _by_reagent(d["risk_sources"][0])
        na = rs["naocl"]
        assert na["total_pure_volume_ml"] == 10.0
        assert [s["pure_volume_ml"] for s in na["sources"]] == [5.0, 5.0]
        assert [s["fraction"] for s in na["sources"]] == [0.5, 0.5]

    def test_clean_clears_sources(self):
        d = post({
            "vessel_material": "hdpe",
            "temperature_c": 22,
            "initial_liquid": [{"reagent_id": "naocl", "concentration": 6, "volume_ml": 100}],
            "steps": [
                {"seq": 1, "op": "clean"},
                {"seq": 2, "op": "add", "reagent_id": "naocl", "concentration": 10, "volume_ml": 100,
                 "at_seconds": 5},
                {"seq": 3, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 50,
                 "at_seconds": 6},
            ],
        }).json()
        rs = _by_reagent(d["risk_sources"][0])
        assert rs["naocl"]["sources"][0]["source"] == {
            "type": "add", "reagent_id": None, "vessel_id": None, "seq": 2
        }


class TestMaterialRisk:
    def test_material_marked_no_liquid_share_for_material(self):
        d = post({
            "vessel_material": "glass",
            "temperature_c": 20,
            "initial_liquid": [{"reagent_id": "hf", "concentration": 10, "volume_ml": 50}],
            "steps": [],
        }).json()
        t = d["risk_sources"][0]
        assert t["category"] == "material_incompatible"
        assert t["material"] == {"material_id": "glass"}
        # 材质不给液体份额：material 仅一个 material_id，没有 pure/fraction 字段
        assert set(t["material"].keys()) == {"material_id"}
        assert _by_reagent(t)["hf"]["sources"][0]["pure_volume_ml"] == 5.0

    def test_timed_material_overtime_is_overtime_category(self):
        # 强碱与玻璃限时 1800s（M04）：超时类别、仍给出材质与液体来源
        d = post({
            "vessel_material": "glass",
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "naoh", "concentration": 20, "volume_ml": 100},
                {"seq": 2, "op": "hold", "at_seconds": 2000},
            ],
        }).json()
        cats = {(t["rule_id"], t["category"]) for t in d["risk_sources"]}
        assert ("M04", "overtime_contact") in cats
        m04 = next(t for t in d["risk_sources"] if t["rule_id"] == "M04")
        assert m04["material"] == {"material_id": "glass"}
        assert _by_reagent(m04)["naoh"]["total_pure_volume_ml"] == 20.0


class TestOvertimeSnapshotTiming:
    BASE = {
        "vessel_material": "glass",
        "temperature_c": 20,
        "steps": [
            {"seq": 1, "op": "add", "reagent_id": "naocl", "concentration": 12, "volume_ml": 100},
            {"seq": 2, "op": "add", "reagent_id": "nh4cl", "concentration": 20, "volume_ml": 100,
             "at_seconds": 60},
            {"seq": 3, "op": "hold", "at_seconds": 700},
            {"seq": 4, "op": "pour", "volume_ml": 100, "at_seconds": 800},
        ],
    }

    def test_overtime_uses_state_before_step_and_not_rewritten(self):
        d = post(self.BASE).json()
        traces = d["risk_sources"]
        assert [t["rule_id"] for t in traces] == ["T02"]
        t = traces[0]
        assert t["seq"] == 3  # 超时在 step3 时刻判定
        rs = _by_reagent(t)
        # step4 倒出一半不得改写：仍是倒出前的 12 / 20
        assert rs["naocl"]["total_pure_volume_ml"] == 12.0
        assert rs["nh4cl"]["total_pure_volume_ml"] == 20.0

    def test_later_clean_does_not_rewrite_overtime_trace(self):
        payload = {
            "vessel_material": "glass",
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "naocl", "concentration": 12, "volume_ml": 100},
                {"seq": 2, "op": "add", "reagent_id": "nh4cl", "concentration": 20, "volume_ml": 100,
                 "at_seconds": 60},
                {"seq": 3, "op": "hold", "at_seconds": 700},
                {"seq": 4, "op": "clean", "at_seconds": 800},
            ],
        }
        d = post(payload).json()
        t = d["risk_sources"][0]
        rs = _by_reagent(t)
        assert rs["naocl"]["sources"][0]["source"]["seq"] == 1
        assert rs["naocl"]["total_pure_volume_ml"] == 12.0
