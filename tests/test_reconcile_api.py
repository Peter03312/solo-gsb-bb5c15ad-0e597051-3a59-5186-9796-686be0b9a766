"""配液核对（实测浓度对账）端到端测试：单容器 /api/v1/reconcile。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def post(payload):
    return client.post("/api/v1/reconcile", json=payload)


def safe_request(**overrides):
    req = {
        "vessel_material": "hdpe",
        "temperature_c": 20,
        "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
        "steps": [
            {"seq": 1, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 50, "at_seconds": 30},
            {"seq": 2, "op": "dilute", "volume_ml": 50, "at_seconds": 60},
        ],
        "contact_allowances": [],
    }
    req.update(overrides)
    return req


def body(*, tolerance=0.5, observations=None, **req_overrides):
    return {
        "request": safe_request(**req_overrides),
        "tolerance_pp": tolerance,
        "observations": observations if observations is not None else [],
    }


# ---------------------------------------------------------------------------
# 正常对账
# ---------------------------------------------------------------------------
class TestReconcilePass:
    def test_predictions_unrounded_and_present_in_steps(self):
        # 步骤1后：hcl=10%，naocl=6.6666…%（未四舍五入）
        d = post(body(observations=[
            {"seq": 1, "reagent_id": "hcl", "concentration": 10.2},
            {"seq": 1, "reagent_id": "naocl", "concentration": 6.67},
            {"at_initial": True, "reagent_id": "naocl", "concentration": 10.0},
        ])).json()
        by = {(o["seq"], o["reagent_id"]): o for o in d["observations"]}
        assert d["all_passed"] is True
        assert by[(1, "hcl")]["predicted_concentration"] == pytest.approx(10.0)
        assert by[(1, "naocl")]["predicted_concentration"] == pytest.approx(20 / 3)
        assert by[(1, "naocl")]["predicted_concentration"] != pytest.approx(6.6667, abs=1e-6)
        assert by[(1, "naocl")]["deviation"] == pytest.approx(6.67 - 20 / 3)
        assert d["first_out_of_tolerance"] is None

    def test_absent_known_reagent_counts_as_zero(self):
        d = post(body(tolerance=0.1, observations=[
            {"at_initial": True, "reagent_id": "koh", "concentration": 0.1},
            {"seq": 1, "reagent_id": "water", "concentration": 0.0},
        ])).json()
        assert [o["predicted_concentration"] for o in d["observations"]] == [0.0, 0.0]
        assert d["all_passed"] is True

    def test_exact_boundary_passes_with_zero_tolerance(self):
        d = post(body(tolerance=0.0, observations=[
            {"at_initial": True, "reagent_id": "naocl", "concentration": 10.0},
            {"seq": 1, "reagent_id": "hcl", "concentration": 10.0},
        ])).json()
        assert d["all_passed"] is True
        assert all(o["passed"] for o in d["observations"])

    def test_dilution_prediction(self):
        # 步骤2加水50mL（总150->200）：hcl 10->7.5，naocl 6.666..->5
        d = post(body(tolerance=0.0, observations=[
            {"seq": 2, "reagent_id": "hcl", "concentration": 7.5},
            {"seq": 2, "reagent_id": "naocl", "concentration": 5.0},
        ])).json()
        assert d["all_passed"] is True

    def test_blocked_flow_still_returns_full_reconciliation(self):
        d = post(body(tolerance=2.0, observations=[
            {"seq": 1, "reagent_id": "hcl", "concentration": 9.5},
        ])).json()
        assert d["precheck"]["blocked"] is True
        assert d["precheck"]["findings"][0]["rule_id"] == "F01"
        assert d["all_passed"] is True

    def test_precheck_payload_is_compatible_with_legacy_shape(self):
        d = post(body(tolerance=0.0)).json()["precheck"]
        assert set(d.keys()) == {
            "safe", "blocked", "first_blocking_seq", "ruleset_version", "vessel_material",
            "temperature_c", "findings", "final_state", "steps",
        }

    def test_no_observations_returns_precheck_only(self):
        d = post(body()).json()
        assert d["observations"] == []
        assert d["all_passed"] is True
        assert d["first_out_of_tolerance"] is None
        assert d["precheck"]["blocked"] is True
        assert d["precheck"]["findings"][0]["rule_id"] == "F01"

    def test_clean_step_position_predicts_zero(self):
        d = post(body(
            initial_liquid=[],
            steps=[
                {"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 10, "volume_ml": 100},
                {"seq": 2, "op": "clean", "at_seconds": 10},
            ],
            tolerance=0.0,
            observations=[{"seq": 2, "reagent_id": "nacl", "concentration": 0.0}],
        )).json()
        assert d["observations"][0]["predicted_concentration"] == 0.0
        assert d["all_passed"] is True

    def test_unordered_step_submission_identified_by_normalized_seq(self):
        d = post(body(
            steps=[
                {"seq": 2, "op": "dilute", "volume_ml": 50, "at_seconds": 60},
                {"seq": 1, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 50, "at_seconds": 30},
            ],
            tolerance=0.0,
            observations=[{"seq": 1, "reagent_id": "hcl", "concentration": 10.0}],
        )).json()
        assert d["all_passed"] is True


# ---------------------------------------------------------------------------
# 超差与最早超差位置
# ---------------------------------------------------------------------------
class TestOutOfTolerance:
    def test_first_failure_follows_execution_order(self):
        d = post(body(tolerance=0.1, observations=[
            {"seq": 2, "reagent_id": "hcl", "concentration": 7.0},   # 超差，但位置靠后（idx0）
            {"at_initial": True, "reagent_id": "naocl", "concentration": 11.0},  # 初始态超差（idx1）
            {"seq": 1, "reagent_id": "hcl", "concentration": 10.0},  # 通过（idx2）
        ])).json()
        assert d["all_passed"] is False
        first = d["first_out_of_tolerance"]
        assert first["observation_index"] == 1
        assert first["at_initial"] is True
        assert first["deviation"] == pytest.approx(1.0)

    def test_same_position_tie_breaks_by_submission_order(self):
        d = post(body(tolerance=0.1, observations=[
            {"seq": 1, "reagent_id": "hcl", "concentration": 10.0},                  # 通过
            {"seq": 1, "reagent_id": "naocl", "concentration": 6.0},                # 超差（idx1）
        ])).json()
        first = d["first_out_of_tolerance"]
        assert first["observation_index"] == 1
        assert first["reagent_id"] == "naocl"

    def test_all_observations_returned_even_with_failures(self):
        d = post(body(tolerance=0.11, observations=[
            {"seq": 1, "reagent_id": "hcl", "concentration": 9.9},
            {"seq": 1, "reagent_id": "naocl", "concentration": 7.0},
        ])).json()
        assert len(d["observations"]) == 2
        assert [o["passed"] for o in d["observations"]] == [True, False]

    def test_tolerance_is_unified_in_percentage_points(self):
        # 5% 实测 vs 7.5% 预测：偏差 2.5 个百分点
        d = post(body(tolerance=2.5, observations=[
            {"seq": 2, "reagent_id": "hcl", "concentration": 5.0},
        ])).json()
        assert d["all_passed"] is True
        d = post(body(tolerance=2.49, observations=[
            {"seq": 2, "reagent_id": "hcl", "concentration": 5.0},
        ])).json()
        assert d["all_passed"] is False


# ---------------------------------------------------------------------------
# 整单拒绝
# ---------------------------------------------------------------------------
class TestRejections:
    def _codes(self, payload):
        r = post(payload)
        assert r.status_code == 400, r.text
        return r.json()["details"]

    def test_invalid_original_flow_rejects_whole_order(self):
        r = post(body(
            vessel_material="kryptonite",
            steps=[{"seq": 1, "op": "add", "reagent_id": "ghost", "concentration": 10, "volume_ml": 0}],
            tolerance=0.5,
            observations=[{"at_initial": True, "reagent_id": "nacl", "concentration": 10}],
        ))
        assert r.status_code == 400
        b = r.json()
        assert "precheck" not in b and "observations" not in b
        codes = {e["code"] for e in b["details"]}
        assert {"UNKNOWN_MATERIAL", "UNKNOWN_REAGENT", "INVALID_VOLUME"} <= codes

    @pytest.mark.parametrize("tol", [-0.01, -5])
    def test_negative_tolerance_rejected(self, tol):
        details = self._codes(body(tolerance=tol))
        assert any(e["code"] == "INVALID_TOLERANCE" for e in details)

    def test_non_finite_tolerance_rejected_at_model_level(self):
        from app.models import ReconcileRequest
        from app.reconcile import reconcile_single
        import math
        m = ReconcileRequest.model_validate_json(
            b'{"request":{"vessel_material":"hdpe","temperature_c":20,"steps":[]},'
            b'"tolerance_pp":NaN,"observations":[]}'
        )
        assert math.isnan(m.tolerance_pp)
        with pytest.raises(Exception) as ei:
            reconcile_single(m)
        assert any(d["code"] == "INVALID_TOLERANCE" for d in ei.value.details)

    def test_unknown_step_seq(self):
        details = self._codes(body(observations=[{"seq": 99, "reagent_id": "hcl", "concentration": 10}]))
        assert any(e["code"] == "UNKNOWN_POSITION" and e["seq"] == 99 for e in details)

    def test_invalid_position_expression(self):
        for ob in (
            {"reagent_id": "hcl", "concentration": 10},
            {"at_initial": True, "seq": 1, "reagent_id": "hcl", "concentration": 10},
            {"seq": 0, "reagent_id": "hcl", "concentration": 10},
        ):
            details = self._codes(body(observations=[ob]))
            assert any(e["code"] == "INVALID_POSITION" for e in details)

    def test_unknown_reagent_rejected(self):
        details = self._codes(body(observations=[{"seq": 1, "reagent_id": "ghost", "concentration": 10}]))
        assert any(e["code"] == "UNKNOWN_REAGENT" for e in details)

    @pytest.mark.parametrize("conc", [-0.01, 100.01])
    def test_measured_concentration_out_of_range(self, conc):
        details = self._codes(body(observations=[{"at_initial": True, "reagent_id": "nacl", "concentration": conc}]))
        assert any(e["code"] == "CONCENTRATION_OUT_OF_RANGE" for e in details)

    def test_duplicate_same_position_same_reagent_rejected(self):
        details = self._codes(body(observations=[
            {"at_initial": True, "reagent_id": "nacl", "concentration": 10},
            {"at_initial": True, "reagent_id": "nacl", "concentration": 10.2},
        ]))
        assert any(e["code"] == "DUPLICATE_OBSERVATION" for e in details)

    def test_same_reagent_at_different_positions_allowed(self):
        r = post(body(tolerance=0.0, observations=[
            {"at_initial": True, "reagent_id": "nacl", "concentration": 10},
            {"seq": 1, "reagent_id": "nacl", "concentration": 20 / 3},
        ]))
        assert r.status_code == 200

    def test_multiple_observation_errors_all_reported(self):
        details = self._codes(body(tolerance=-1, observations=[
            {"seq": 99, "reagent_id": "ghost", "concentration": 150},
            {"at_initial": True, "reagent_id": "nacl", "concentration": 10},
            {"at_initial": True, "reagent_id": "nacl", "concentration": 11},
        ]))
        codes = [e["code"] for e in details]
        assert "INVALID_TOLERANCE" in codes
        assert "UNKNOWN_POSITION" in codes
        assert "UNKNOWN_REAGENT" in codes
        assert "CONCENTRATION_OUT_OF_RANGE" in codes
        assert "DUPLICATE_OBSERVATION" in codes

    def test_vessel_id_not_allowed_on_single_endpoint(self):
        r = post(body(observations=[
            {"at_initial": True, "vessel_id": "A", "reagent_id": "nacl", "concentration": 10},
        ]))
        assert r.status_code == 400
        assert r.json()["details"][0]["code"] == "MALFORMED_REQUEST"

    def test_rejection_message_and_no_partial(self):
        r = post(body(tolerance=-1, observations=[{"seq": 1, "reagent_id": "hcl", "concentration": 10}]))
        b = r.json()
        assert b["message"] == "核对请求被整单拒绝：原流程无效或容差/观测非法，未进行模拟与对账"
        assert "precheck" not in b

    def test_boolean_and_non_numeric_tolerance_rejected_as_malformed(self):
        for tol in (True, "0.5"):
            r = post(body(tolerance=tol))
            assert r.status_code == 400
            assert r.json()["details"][0]["code"] == "MALFORMED_REQUEST"

    def test_invalid_plan_and_observation_errors_both_reported(self):
        # 原流程无效（未知材质）与观测错误（未知步骤+未知试剂）同时逐项报出
        details = self._codes(body(
            vessel_material="kryptonite",
            observations=[{"seq": 7, "reagent_id": "ghost", "concentration": 10}],
        ))
        codes = {e["code"] for e in details}
        assert {"UNKNOWN_MATERIAL", "UNKNOWN_POSITION", "UNKNOWN_REAGENT"} <= codes
