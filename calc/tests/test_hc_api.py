"""I3 承载力 API 测试。

与 test_timeseries_api.py 同样把 RESULT_ROOT 指向 tmp_path，避免污染 data/results。
接口层只测"编排 + 错误映射"，算法正确性在 test_hc_solver.py。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import gp_hosting.api as api_mod
from gp_hosting import hc_solver


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "RESULT_ROOT", tmp_path)
    monkeypatch.setattr(api_mod, "_hc_store", None)
    api_mod.registry.clear()
    yield TestClient(api_mod.app)
    api_mod.registry.clear()


# 真实算例单点约 10 s，用 24 点（1 h）曲线把接口层测试压到秒级；
# 96 点全精度只在 test_hc_solver.py 的验收用例里跑。
HOUR_LOAD = "data/profiles/load_typical_day.csv"
HOUR_PV = "data/profiles/pv_typical_day.csv"


def _quick(**kw):
    payload = {
        "case_ref": "ieee33_ampacity",
        "bus": 17,
        "load_profile_path": HOUR_LOAD,
        "pv_profile_path": HOUR_PV,
        "constraints": {"preset": "relaxed_0.90_1.10"},
        "options": {"max_capacity_mw": 6.0},
        "include_curve": False,
    }
    payload.update(kw)
    return payload


class TestHcSingle:
    def test_success_envelope_and_fields(self, client):
        r = client.post("/api/hc/single", json=_quick())
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 0 and body["message"] == "ok"
        d = body["data"]
        for key in ("bus", "max_mw", "limiting_type", "limiting_time", "search_method",
                    "monotonicity", "probe", "iterations", "task"):
            assert key in d, key
        assert d["bus"] == 17 and d["max_mw"] > 0
        assert d["probe"]["passed"] is True
        assert d["task"]["status"] == "SUCCEEDED"

    def test_bus_in_response_not_list(self, client):
        """单点接口返回的是单个结果对象，不是长度为 1 的数组。"""
        d = client.post("/api/hc/single", json=_quick()).json()["data"]
        assert isinstance(d, dict) and "max_mw" in d

    def test_unknown_bus_returns_40002(self, client):
        body = client.post("/api/hc/single", json=_quick(bus=999)).json()
        assert body["code"] == 40002
        assert body["data"]["detail"]["field"] == "sgens.bus"

    def test_unknown_case_ref_returns_40002(self, client):
        body = client.post("/api/hc/single", json=_quick(case_ref="no_such_case")).json()
        assert body["code"] == 40002
        assert body["data"]["detail"]["field"] == "case_ref"

    def test_unknown_preset_returns_40002(self, client):
        body = client.post("/api/hc/single", json=_quick(constraints={"preset": "nope"})).json()
        assert body["code"] == 40002
        assert body["data"]["detail"]["field"] == "constraints"

    def test_tol_coarser_than_probe_rejected(self, client):
        body = client.post("/api/hc/single", json=_quick(options={"tol_mw": 0.2, "probe_mw": 0.05})).json()
        assert body["code"] == 40002
        assert body["data"]["detail"]["field"] == "options"

    def test_invalid_power_factor_returns_422(self, client):
        """请求体本身不合法（pydantic 校验）走 FastAPI 的 422，与业务校验的 40002 区分开。"""
        assert client.post("/api/hc/single", json=_quick(power_factor=1.5)).status_code == 422

    def test_baseline_infeasible_reported_not_error(self, client):
        """ieee33 原始算例 + 合成曲线（负荷峰值 1.0）在 GB/T 下基线即越限：
        这是有效结果（max_mw=0），不是错误码。"""
        body = client.post(
            "/api/hc/single",
            json=_quick(case_ref="ieee33", constraints={"preset": "gbt_12325"}),
        ).json()
        assert body["code"] == 0
        assert body["data"]["max_mw"] == 0.0
        assert body["data"]["limiting_type"] == "baseline_infeasible"
        assert body["data"]["limiting_element_index"] == 17


class TestHcBatch:
    def test_batch_returns_one_result_per_bus(self, client, monkeypatch):
        real = hc_solver.solve_batch

        def single_node(base, buses, constraints=None, options=None, power_factor=1.0, workers=1):
            # 接口层测试只验证编排，算法用真实的但把节点数压到 2 个
            return real(base, buses[:2], constraints, options, power_factor, workers=1)

        monkeypatch.setattr(api_mod.hc_solver, "solve_batch", single_node)
        body = client.post("/api/hc/batch", json=_quick(buses=[17, 32])).json()
        assert body["code"] == 0
        rows = body["data"]["results"]
        assert [r["bus"] for r in rows] == [17, 32]
        assert all("max_mw" in r for r in rows)
        assert body["data"]["task"]["status"] == "SUCCEEDED"

    def test_duplicate_buses_rejected(self, client):
        body = client.post("/api/hc/batch", json=_quick(buses=[17, 17])).json()
        assert body["code"] == 40002
        assert "重复" in body["message"]

    def test_empty_buses_rejected_by_schema(self, client):
        assert client.post("/api/hc/batch", json=_quick(buses=[])).status_code == 422


class TestHcQuery:
    def test_task_and_curve_roundtrip(self, client):
        d = client.post("/api/hc/single", json=_quick(include_curve=True)).json()["data"]
        task_id = d["task"]["task_id"]

        rows = client.get(f"/api/hc/{task_id}").json()["data"]["rows"]
        assert len(rows) == 1
        row = rows[0]
        assert row["node"] == 17 and row["max_mw"] == d["max_mw"]
        assert row["limiting_type"] == d["limiting_type"]
        assert row["probe_passed"] == 1
        # 摘要表不存明细
        assert "curve" not in row

        curve = client.get(f"/api/hc/{task_id}/curve", params={"bus": 17}).json()["data"]
        assert len(curve["points"]) >= 10
        assert curve["points"] == d["curve"]

    def test_unknown_task_returns_40404(self, client):
        assert client.get("/api/hc/deadbeef").json()["code"] == 40404
        assert client.get("/api/hc/deadbeef/curve", params={"bus": 17}).json()["code"] == 40404

    def test_curve_for_absent_bus_returns_40404(self, client):
        task_id = client.post("/api/hc/single", json=_quick()).json()["data"]["task"]["task_id"]
        assert client.get(f"/api/hc/{task_id}/curve", params={"bus": 8}).json()["code"] == 40404


class TestPresetsEndpoint:
    def test_lists_presets_with_notes(self, client):
        data = client.get("/api/hc/presets").json()["data"]["presets"]
        assert "gbt_12325" in data and "relaxed_0.90_1.10" in data
        assert "GB/T" in data["gbt_12325"]["note"]
        assert data["gbt_12325"]["values"]["vm_min_pu"] == 0.93
