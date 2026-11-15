"""I4 API 测试。接口层只测编排与错误映射，算法正确性在 test_analysis.py。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import gp_hosting.api as api_mod

SB = "data/profiles/simbench_pv_max_day_20160513_"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "RESULT_ROOT", tmp_path)
    monkeypatch.setattr(api_mod, "_hc_store", None)
    api_mod.registry.clear()
    yield TestClient(api_mod.app)
    api_mod.registry.clear()


@pytest.fixture(scope="module")
def violation_result_dir(tmp_path_factory):
    """模块级：落盘一次越限场景，供归因接口读取。"""
    from gp_hosting import timeseries as ts

    root = tmp_path_factory.mktemp("i4api")
    spec = ts.ScenarioSpec(
        case_path="data/cases/ieee33_ampacity.json",
        load_profile_path=SB + "load.csv",
        pv_profile_path=SB + "pv.csv",
        sgens=[ts.SgenInjection(bus=17, capacity_mw=4.0)],
    )
    scenario = ts.assemble_scenario(spec)
    result = ts.run_timeseries(scenario)
    return ts.write_result_set(result, scenario, root, task_id="i4fix")


@pytest.fixture()
def attribution_client(tmp_path, monkeypatch, violation_result_dir):
    """让 registry 里有一条指向已落盘结果的 SUCCEEDED 任务。"""
    monkeypatch.setattr(api_mod, "RESULT_ROOT", tmp_path)
    api_mod.registry.clear()
    rec = api_mod.registry.create({"kind": "fixture"})
    api_mod.registry.succeed(rec.task_id, result_dir=violation_result_dir, summary={})
    client = TestClient(api_mod.app)
    yield client, rec.task_id, violation_result_dir
    api_mod.registry.clear()


class TestAttribution:
    def test_returns_first_violation_and_heatmap(self, attribution_client):
        client, task_id, result_dir = attribution_client
        body = client.get(f"/api/analysis/attribution/{task_id}").json()
        assert body["code"] == 0
        d = body["data"]
        assert d["feasible"] is False
        assert d["first_time"] == d["first_violation"]["start_time"]
        assert d["first_violation"]["element_type"] == "bus"
        assert d["heatmap"]["vm"]["n_violating_cells"] > 0
        assert d["source"]["result_dir"] == str(result_dir)
        assert d["task_id"] == task_id

    def test_heatmap_can_be_omitted(self, attribution_client):
        client, task_id, _ = attribution_client
        d = client.get(
            f"/api/analysis/attribution/{task_id}", params={"include_heatmap": False}
        ).json()["data"]
        assert "heatmap" not in d

    def test_merge_gap_merges_events(self, attribution_client):
        client, task_id, _ = attribution_client
        a = client.get(f"/api/analysis/attribution/{task_id}").json()["data"]
        b = client.get(
            f"/api/analysis/attribution/{task_id}", params={"merge_gap": 4}
        ).json()["data"]
        assert b["n_events"] <= a["n_events"]

    def test_unknown_task_returns_40404(self, attribution_client):
        client, _, _ = attribution_client
        assert client.get("/api/analysis/attribution/nope").json()["code"] == 40404

    def test_bad_preset_returns_40002(self, attribution_client):
        client, task_id, _ = attribution_client
        body = client.get(
            f"/api/analysis/attribution/{task_id}", params={"constraints_preset": "nope"}
        ).json()
        assert body["code"] == 40002


class TestSensitivityEndpoint:
    def test_returns_sensitivities_and_bottlenecks(self, client):
        body = client.post("/api/analysis/sensitivity", json={"bus": 17, "base_mw": 3.0}).json()
        assert body["code"] == 0
        d = body["data"]
        assert d["bus"] == 17 and d["method"] == "central_difference"
        assert abs(d["d_vmin_dp"]) < 1e-9
        assert d["d_v_bus_at_peak_dp"] > 0
        assert "matrices" not in d
        assert d["bottlenecks"]
        assert d["bottlenecks"][0]["rank"] == 1
        assert d["task"]["status"] == "SUCCEEDED"

    def test_include_matrices(self, client):
        d = client.post(
            "/api/analysis/sensitivity",
            json={"bus": 17, "base_mw": 3.0, "include_matrices": True},
        ).json()["data"]
        assert len(d["matrices"]["dv_dp"]) == 96
        assert len(d["matrices"]["dv_dp"][0]) == 33
        assert len(d["matrices"]["dloading_dp"][0]) == 37

    @pytest.mark.parametrize(
        "payload, match",
        [
            ({"bus": 17, "base_mw": 1.0, "delta_mw": 1.0}, "差分步长"),
            ({"bus": 17, "base_mw": 0.0}, None),  # 被 pydantic 拦（gt=0）
        ],
    )
    def test_bad_params(self, client, payload, match):
        r = client.post("/api/analysis/sensitivity", json=payload)
        if match:
            assert r.json()["code"] == 40002
        else:
            assert r.status_code == 422

    def test_unknown_bus(self, client):
        assert client.post("/api/analysis/sensitivity", json={"bus": 99, "base_mw": 1.0}).json()["code"] == 40002


class TestCompareEndpoint:
    def test_recommends_and_orders(self, client):
        body = client.post(
            "/api/analysis/compare",
            json={"buses": [1, 17, 32], "capacity_mw": 5.0},
        ).json()
        assert body["code"] == 0
        d = body["data"]
        assert len(d["candidates"]) == 3
        assert d["recommended_bus"] == d["candidates"][0]["bus"]
        assert d["recommended_bus"] == 1
        assert d["rationale"]

    @pytest.mark.parametrize("order", [[1, 17, 32], [32, 17, 1], [17, 32, 1], [1, 32, 17]])
    def test_recommendation_independent_of_input_order(self, client, order):
        """回归：可行候选之间曾完全并列（越限幅度都为 0、线路最大负载率与接入点无关），
        排序退化成输入顺序，换个 buses 顺序就换推荐结果。现已用 headroom 破平。"""
        d = client.post(
            "/api/analysis/compare", json={"buses": order, "capacity_mw": 5.0}
        ).json()["data"]
        assert d["recommended_bus"] == 1
        assert [c["bus"] for c in d["candidates"]] == [1, 32, 17]
        assert [c["rank"] for c in d["candidates"]] == [1, 2, 3]

    def test_headroom_separates_feasible_candidates(self, client):
        """可行不等于同样好：bus32 在 5 MW 下只剩 0.03 MW 余量，bus1 还有 14 MW。"""
        d = client.post(
            "/api/analysis/compare", json={"buses": [1, 32], "capacity_mw": 5.0}
        ).json()["data"]
        by = {c["bus"]: c for c in d["candidates"]}
        assert by[1]["feasible"] and by[32]["feasible"]
        assert by[1]["headroom_mw"] > 5.0
        # I3 承载力 bus32 = 5.025 MW，在 5 MW 下余量应约 25 kW
        assert by[32]["headroom_mw"] == pytest.approx(0.025, abs=0.05)
        assert by[1]["recommendation_score"] > by[32]["recommendation_score"]

    def test_tie_still_deterministic_without_sensitivity(self, client):
        """关闭灵敏度时没有 headroom 可破平，按节点编号兜底，仍与输入顺序无关。"""
        for order in ([1, 17, 32], [32, 17, 1]):
            d = client.post(
                "/api/analysis/compare",
                json={"buses": order, "capacity_mw": 5.0, "with_sensitivity": False},
            ).json()["data"]
            assert d["recommended_bus"] == 1
            assert [c["bus"] for c in d["candidates"]] == [1, 32, 17]

    def test_too_few_buses_rejected_by_schema(self, client):
        assert client.post("/api/analysis/compare", json={"buses": [17], "capacity_mw": 5.0}).status_code == 422

    def test_too_many_buses_rejected_by_schema(self, client):
        assert client.post(
            "/api/analysis/compare", json={"buses": [1, 5, 8, 12, 17, 24], "capacity_mw": 5.0}
        ).status_code == 422

    def test_duplicate_buses_returns_40002(self, client):
        body = client.post("/api/analysis/compare", json={"buses": [17, 17], "capacity_mw": 5.0}).json()
        assert body["code"] == 40002

    def test_minimal_two_bus_comparison(self, client):
        """2 个候选点是允许的下限。"""
        d = client.post(
            "/api/analysis/compare",
            json={"buses": [1, 17], "capacity_mw": 5.0, "with_sensitivity": False},
        ).json()["data"]
        assert len(d["candidates"]) == 2
        assert {c["bus"] for c in d["candidates"]} == {1, 17}
