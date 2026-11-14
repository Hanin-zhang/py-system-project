"""时序 API 接口测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gp_hosting.api import app
from gp_hosting.tasks import registry

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_registry(tmp_path, monkeypatch):
    """每个测试用独立结果目录，避免互相干扰。"""
    registry.clear()
    import gp_hosting.api as api_mod

    monkeypatch.setattr(api_mod, "RESULT_ROOT", tmp_path)
    yield
    registry.clear()


class TestRunTimeseries:
    def test_no_pv_run(self):
        r = client.post(
            "/api/timeseries/run",
            json={"case_ref": "ieee33", "sgens": [], "name": "no-pv"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 0, body
        data = body["data"]
        summary = data["summary"]
        assert summary["n_steps"] == 96
        assert summary["n_converged"] == 96
        assert summary["min_vm_bus"] == 17
        assert summary["min_vm_time"] == "20:00"
        assert data["task"]["status"] == "SUCCEEDED"

    def test_with_pv(self):
        r = client.post(
            "/api/timeseries/run",
            json={
                "case_ref": "ieee33",
                "sgens": [{"bus": 17, "capacity_mw": 2.0}],
                "name": "pv-2mw",
            },
        )
        body = r.json()
        assert body["code"] == 0
        summary = body["data"]["summary"]
        assert summary["min_vm_bus"] == 17
        assert summary["min_vm_time"] == "20:00"
        assert summary["max_vm_bus"] == 17
        assert summary["max_vm_pu"] > 1.0
        assert summary["energy_pv_mwh"] > 0

    def test_task_status_query(self):
        r = client.post("/api/timeseries/run", json={"case_ref": "ieee33"})
        task_id = r.json()["data"]["task"]["task_id"]

        r2 = client.get(f"/api/timeseries/{task_id}")
        assert r2.status_code == 200
        body = r2.json()
        assert body["data"]["status"] == "SUCCEEDED"
        assert body["data"]["summary"]["n_steps"] == 96

    def test_unknown_task(self):
        r = client.get("/api/timeseries/deadbeefdeadbeef")
        assert r.json()["code"] == 40404

    def test_list_tasks(self):
        client.post("/api/timeseries/run", json={"case_ref": "ieee33"})
        client.post("/api/timeseries/run", json={"case_ref": "ieee33"})
        r = client.get("/api/timeseries")
        assert len(r.json()["data"]["tasks"]) == 2


class TestMatrixEndpoint:
    def _run(self, **kwargs):
        payload = {"case_ref": "ieee33"}
        payload.update(kwargs)
        r = client.post("/api/timeseries/run", json=payload)
        assert r.json()["code"] == 0
        return r.json()["data"]["task"]["task_id"]

    def test_bus_series(self):
        task_id = self._run(sgens=[{"bus": 17, "capacity_mw": 2.0}])
        r = client.get(f"/api/timeseries/{task_id}/matrix?type=vm_pu&bus=17")
        body = r.json()
        assert body["code"] == 0
        data = body["data"]
        assert data["element"] == 17
        assert len(data["values"]) == 96
        assert data["unit"] == "p.u."
        assert all(0.5 < v < 1.5 for v in data["values"])

    def test_series_matches_summary(self):
        task_id = self._run(sgens=[{"bus": 17, "capacity_mw": 2.0}])
        series = client.get(f"/api/timeseries/{task_id}/matrix?type=vm_pu&bus=17").json()["data"]
        summary = client.get(f"/api/timeseries/{task_id}").json()["data"]["summary"]

        assert min(series["values"]) == pytest.approx(summary["min_vm_pu"])
        assert series["values"].index(min(series["values"])) == summary["min_vm_step"]

    def test_line_series(self):
        task_id = self._run()
        r = client.get(f"/api/timeseries/{task_id}/matrix?type=line_loading&line=0")
        data = r.json()["data"]
        assert len(data["values"]) == 96
        assert data["unit"] == "%"

    def test_step_slice(self):
        task_id = self._run()
        r = client.get(f"/api/timeseries/{task_id}/matrix?type=vm_pu&bus=17&step_from=40&step_to=48")
        data = r.json()["data"]
        assert len(data["values"]) == 8
        assert data["steps"][0] == 40

    def test_full_matrix(self):
        task_id = self._run()
        r = client.get(f"/api/timeseries/{task_id}/matrix?type=vm_pu")
        data = r.json()["data"]
        assert len(data["values"]) == 96
        assert len(data["values"][0]) == 33

    def test_bad_type(self):
        task_id = self._run()
        r = client.get(f"/api/timeseries/{task_id}/matrix?type=nonsense")
        assert r.json()["code"] == 40003

    def test_bad_element(self):
        task_id = self._run()
        r = client.get(f"/api/timeseries/{task_id}/matrix?type=vm_pu&bus=999")
        body = r.json()
        assert body["code"] == 40003
        assert 17 in body["data"]["available_elements"]


class TestValidationErrors:
    def test_unknown_case_ref(self):
        r = client.post("/api/timeseries/run", json={"case_ref": "no-such-case"})
        body = r.json()
        assert body["code"] == 40002
        assert "ieee33" in body["data"]["detail"]["available"]

    def test_nonexistent_pv_bus(self):
        r = client.post(
            "/api/timeseries/run",
            json={"case_ref": "ieee33", "sgens": [{"bus": 99, "capacity_mw": 1.0}]},
        )
        body = r.json()
        assert body["code"] == 40002
        assert "99" in body["message"]

    def test_negative_capacity_rejected_by_schema(self):
        r = client.post(
            "/api/timeseries/run",
            json={"case_ref": "ieee33", "sgens": [{"bus": 17, "capacity_mw": -1.0}]},
        )
        assert r.status_code == 422  # pydantic 层面就拦住

    def test_invalid_case_dict(self):
        r = client.post(
            "/api/timeseries/run",
            json={"case": {"meta": {}, "buses": [], "ext_grids": []}},
        )
        body = r.json()
        assert body["code"] in (40001, 40002)

    def test_bad_load_profile(self, tmp_path):
        bad = tmp_path / "bad.csv"
        bad.write_text("time_index,factor\n" + "\n".join(f"{i},0.0" for i in range(96)), encoding="utf-8")
        r = client.post(
            "/api/timeseries/run",
            json={"case_ref": "ieee33", "load_profile_path": str(bad)},
        )
        body = r.json()
        assert body["code"] == 40002
        assert "负荷" in body["message"]


class TestOpenAPIExposure:
    def test_timeseries_endpoints_documented(self):
        spec = client.get("/openapi.json").json()
        assert "/api/timeseries/run" in spec["paths"]
        assert "/api/timeseries/{task_id}" in spec["paths"]
        assert "/api/timeseries/{task_id}/matrix" in spec["paths"]
