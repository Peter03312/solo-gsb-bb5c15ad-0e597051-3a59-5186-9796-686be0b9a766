"""双容器配液核对端到端测试：/api/v1/transfer-reconcile。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def post(payload):
    return client.post("/api/v1/transfer-reconcile", json=payload)


def hdpe(vid, initial=None):
    return {"vessel_id": vid, "vessel_material": "hdpe", "initial_liquid": initial or []}


def request_():
    return {
        "vessels": [
            hdpe("A", [{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}]),
            hdpe("B", [{"reagent_id": "koh", "concentration": 4, "volume_ml": 100}]),
        ],
        "temperature_c": 20,
        "steps": [
            {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 40, "at_seconds": 30},
            {"seq": 2, "op": "hold", "at_seconds": 200},
        ],
        "contact_allowances": [],
    }


def body(*, tolerance=0.2, observations=None, request=None):
    return {
        "request": request or request_(),
        "tolerance_pp": tolerance,
        "observations": observations if observations is not None else [],
    }


# ---------------------------------------------------------------------------
# 正常对账
# ---------------------------------------------------------------------------
class TestTransferReconcilePass:
    def test_both_vessels_predictions_after_transfer(self):
        # 转出后 A：60mL, nacl 10%；B：140mL, nacl=4/140*100，koh=4/140*100
        d = post(body(observations=[
            {"seq": 1, "vessel_id": "A", "reagent_id": "nacl", "concentration": 10.0},
            {"seq": 1, "vessel_id": "B", "reagent_id": "nacl", "concentration": 2.86},
            {"seq": 1, "vessel_id": "B", "reagent_id": "koh", "concentration": 2.85},
            {"at_initial": True, "vessel_id": "B", "reagent_id": "koh", "concentration": 4.0},
            {"at_initial": True, "vessel_id": "A", "reagent_id": "koh", "concentration": 0.0},
        ])).json()
        assert d["all_passed"] is True
        pred = {(o["vessel_id"], o["seq"], o["reagent_id"]): o["predicted_concentration"] for o in d["observations"]}
        assert pred[("A", 1, "nacl")] == pytest.approx(10.0)
        assert pred[("B", 1, "nacl")] == pytest.approx(400 / 140)
        assert pred[("B", 1, "koh")] == pytest.approx(400 / 140)
        assert pred[("A", None, "koh")] == 0.0  # 该位置未出现的已知试剂按零计

    def test_hold_step_leaves_concentrations_unchanged(self):
        d = post(body(tolerance=0.0, observations=[
            {"seq": 2, "vessel_id": "A", "reagent_id": "nacl", "concentration": 10.0},
            {"seq": 2, "vessel_id": "B", "reagent_id": "koh", "concentration": 400 / 140},
        ])).json()
        assert d["all_passed"] is True

    def test_blocked_transfer_still_reconciled(self):
        req = {
            "vessels": [
                {"vessel_id": "acid", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "hcl", "concentration": 30, "volume_ml": 100}]},
                {"vessel_id": "bleach", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}]},
            ],
            "temperature_c": 22,
            "steps": [{"seq": 1, "op": "transfer", "from_vessel": "acid", "to_vessel": "bleach",
                       "volume_ml": 50, "at_seconds": 5}],
        }
        d = post(body(request=req, tolerance=1.0, observations=[
            {"seq": 1, "vessel_id": "bleach", "reagent_id": "hcl", "concentration": 10.0},
            {"seq": 1, "vessel_id": "bleach", "reagent_id": "naocl", "concentration": 6.5},
        ])).json()
        assert d["precheck"]["blocked"] is True
        assert d["precheck"]["findings"][0]["vessel_id"] == "bleach"
        assert d["all_passed"] is True

    def test_no_observations_returns_precheck_only(self):
        d = post(body()).json()
        assert d["observations"] == []
        assert d["all_passed"] is True
        assert d["first_out_of_tolerance"] is None
        assert set(d["precheck"].keys()) == {
            "safe", "blocked", "first_blocking_seq", "first_blocking_vessel_id",
            "ruleset_version", "temperature_c", "findings", "steps", "final_states",
        }

    def test_same_position_same_reagent_different_vessels_allowed(self):
        d = post(body(tolerance=0.0, observations=[
            {"seq": 1, "vessel_id": "A", "reagent_id": "nacl", "concentration": 10.0},
            {"seq": 1, "vessel_id": "B", "reagent_id": "nacl", "concentration": 400 / 140},
        ])).json()
        assert d["all_passed"] is True


# ---------------------------------------------------------------------------
# 最早超差位置
# ---------------------------------------------------------------------------
class TestTransferFirstFailure:
    def test_initial_position_precedes_steps_regardless_of_submission(self):
        d = post(body(tolerance=0.05, observations=[
            {"seq": 1, "vessel_id": "A", "reagent_id": "nacl", "concentration": 10.0},
            {"at_initial": True, "vessel_id": "A", "reagent_id": "nacl", "concentration": 10.5},
        ])).json()
        first = d["first_out_of_tolerance"]
        assert first["at_initial"] is True and first["observation_index"] == 1

    def test_same_position_submission_order_tiebreak(self):
        d = post(body(tolerance=0.0, observations=[
            {"seq": 1, "vessel_id": "A", "reagent_id": "nacl", "concentration": 10.0},
            {"seq": 1, "vessel_id": "B", "reagent_id": "koh", "concentration": 99.0},
        ])).json()
        first = d["first_out_of_tolerance"]
        assert first["observation_index"] == 1 and first["vessel_id"] == "B"


# ---------------------------------------------------------------------------
# 整单拒绝
# ---------------------------------------------------------------------------
class TestTransferRejections:
    def _codes(self, payload):
        r = post(payload)
        assert r.status_code == 400, r.text
        return r.json()["details"]

    def test_invalid_original_flow_rejects_whole_order(self):
        bad = request_()
        bad["vessels"] = [hdpe("A")]
        r = post(body(request=bad, observations=[
            {"at_initial": True, "vessel_id": "A", "reagent_id": "nacl", "concentration": 10},
        ]))
        assert r.status_code == 400
        b = r.json()
        assert "precheck" not in b and "observations" not in b
        assert any(e["code"] == "INVALID_VESSEL" for e in b["details"])

    def test_unknown_vessel_in_observation(self):
        details = self._codes(body(observations=[
            {"seq": 1, "vessel_id": "ZZ", "reagent_id": "nacl", "concentration": 10},
        ]))
        assert any(e["code"] == "UNKNOWN_VESSEL" for e in details)

    def test_missing_vessel_id_is_malformed(self):
        r = post(body(observations=[{"at_initial": True, "reagent_id": "nacl", "concentration": 10}]))
        assert r.status_code == 400
        assert r.json()["details"][0]["code"] == "MALFORMED_REQUEST"

    def test_unknown_step_and_reagent(self):
        details = self._codes(body(observations=[
            {"seq": 42, "vessel_id": "A", "reagent_id": "ghost", "concentration": 10},
        ]))
        codes = {e["code"] for e in details}
        assert "UNKNOWN_POSITION" in codes and "UNKNOWN_REAGENT" in codes

    def test_duplicate_same_vessel_position_reagent(self):
        details = self._codes(body(observations=[
            {"seq": 1, "vessel_id": "A", "reagent_id": "nacl", "concentration": 10},
            {"seq": 1, "vessel_id": "A", "reagent_id": "nacl", "concentration": 10.1},
        ]))
        assert any(e["code"] == "DUPLICATE_OBSERVATION" for e in details)

    @pytest.mark.parametrize("conc", [-1, 101])
    def test_concentration_bounds(self, conc):
        details = self._codes(body(observations=[
            {"at_initial": True, "vessel_id": "A", "reagent_id": "nacl", "concentration": conc},
        ]))
        assert any(e["code"] == "CONCENTRATION_OUT_OF_RANGE" for e in details)

    def test_bad_tolerance(self):
        details = self._codes(body(tolerance=-0.0001))
        assert any(e["code"] == "INVALID_TOLERANCE" for e in details)

    def test_no_partial_simulation(self):
        bad = request_()
        bad["steps"].append({"seq": 3, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                             "volume_ml": 9999, "at_seconds": 300})
        r = post(body(request=bad, observations=[
            {"seq": 1, "vessel_id": "A", "reagent_id": "nacl", "concentration": 10},
        ]))
        assert r.status_code == 400
        b = r.json()
        assert "precheck" not in b and "observations" not in b
        assert any(e["code"] == "TRANSFER_EXCEEDS_VOLUME" for e in b["details"])

    def test_bad_vessel_definitions_do_not_cascade_vessel_errors(self):
        # 容器数量非法：只报原流程错误，不级联观测容器/对账结果
        bad = {"vessels": [hdpe("A")], "temperature_c": 20, "steps": []}
        r = post(body(request=bad, observations=[
            {"at_initial": True, "vessel_id": "A", "reagent_id": "nacl", "concentration": 10},
        ]))
        assert r.status_code == 400
        codes = {e["code"] for e in r.json()["details"]}
        assert codes == {"INVALID_VESSEL"}

    def test_duplicate_vessel_id_request_rejected_even_with_good_observations(self):
        bad = {"vessels": [hdpe("A"), hdpe("A")], "temperature_c": 20, "steps": []}
        r = post(body(request=bad, observations=[
            {"at_initial": True, "vessel_id": "A", "reagent_id": "nacl", "concentration": 10},
        ]))
        assert r.status_code == 400
        assert any(e["code"] == "DUPLICATE_VESSEL_ID" for e in r.json()["details"])
