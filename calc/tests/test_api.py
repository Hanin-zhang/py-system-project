"""FastAPI 接口测试（用 TestClient，不启动真实服务器）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gp_hosting.api import app

client = TestClient(app)


class TestHealth:
    def test_health(self):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["service"] == "gp-hosting-calc"


class TestPowerflowRun:
    def test_normal_run(self, raw_case):
        r = client.post("/api/powerflow/run", json={"case": raw_case})
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 0
        data = body["data"]
        assert data["converged"] is True
        assert data["summary"]["min_vm_bus"] == 17
        assert data["summary"]["min_vm_pu"] == pytest.approx(0.9131, abs=1e-3)
        assert "violations" in data
        assert data["violations"]["feasible"] is False  # IEEE 33 基线就有 14 处低压

    def test_violations_structure(self, raw_case):
        r = client.post("/api/powerflow/run", json={"case": raw_case})
        violations = r.json()["data"]["violations"]["violations"]
        assert len(violations) == 14
        first = violations[0]
        for key in ("constraint", "element_type", "element_index", "value", "limit", "severity"):
            assert key in first
        assert all(v["constraint"] == "voltage_low" for v in violations)

    def test_custom_constraints_change_verdict(self, raw_case):
        """放宽电压下限到 0.90，基线算例应变为可行。"""
        r = client.post(
            "/api/powerflow/run",
            json={"case": raw_case, "constraints": {"vm_min_pu": 0.90}},
        )
        data = r.json()["data"]
        assert data["violations"]["feasible"] is True

    def test_options_echoed(self, raw_case):
        r = client.post(
            "/api/powerflow/run",
            json={"case": raw_case, "algorithm": "bfsw", "max_iteration": 20},
        )
        data = r.json()["data"]
        assert data["input"]["algorithm"] == "bfsw"
        assert data["input"]["max_iteration"] == 20

    def test_bad_reference_returns_40001_not_500(self, raw_case):
        """字段级错误必须是 code=40001 + HTTP 200，不能是 500 或堆栈。"""
        raw_case["lines"][3]["to_bus"] = 999
        r = client.post("/api/powerflow/run", json={"case": raw_case})
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 40001
        issues = body["data"]["issues"]
        assert any(i["path"] == "lines[3].to_bus" for i in issues)
        assert "Traceback" not in r.text

    def test_unit_error_returns_40001(self, raw_case):
        for ld in raw_case["loads"]:
            ld["p_mw"] *= 1000
        r = client.post("/api/powerflow/run", json={"case": raw_case})
        assert r.status_code == 200
        assert r.json()["code"] == 40001
        assert "单位" in str(r.json()["data"]["issues"])

    def test_unconverged_returns_42001(self, raw_case):
        for ln in raw_case["lines"]:
            ln["r_ohm_per_km"] *= 1e4
            ln["x_ohm_per_km"] *= 1e4
        r = client.post(
            "/api/powerflow/run",
            json={"case": raw_case, "max_iteration": 3},
        )
        body = r.json()
        assert body["code"] in (0, 42001)
        if body["code"] == 42001:
            assert "不收敛" in body["message"]


class TestCaseValidate:
    def test_valid_case(self, raw_case):
        r = client.post("/api/case/validate", json={"case": raw_case})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["valid"] is True
        assert data["counts"]["buses"] == 33
        assert data["counts"]["lines"] == 37

    def test_invalid_case_reports_issues(self, raw_case):
        raw_case["ext_grids"] = []
        r = client.post("/api/case/validate", json={"case": raw_case})
        body = r.json()
        assert body["code"] == 40001
        assert any("平衡节点" in i["message"] for i in body["data"]["issues"])


class TestOpenAPI:
    def test_openapi_available(self):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        assert "/api/powerflow/run" in spec["paths"]
        assert spec["info"]["title"]

    def test_docs_page(self):
        r = client.get("/docs")
        assert r.status_code == 200
