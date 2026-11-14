"""I3 承载力测试。

分两层：
1. 纯单元测试（毫秒级）：可行性判定、单调性判定、二分边界、非单调分支。
   用构造数据 / 假评估器，不跑潮流 —— 这样"算法对不对"与"潮流对不对"互不干扰。
2. 真实算例验收（ieee33_ampacity + SimBench 光伏最大日）：对应排期 I3 DoD 的每一条。
   多个节点的结果只算一次（module 级 fixture），各测试复用。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from gp_hosting import hc_solver as hc
from gp_hosting import timeseries as ts
from gp_hosting.constraints import Constraints, check_timeseries, constraints_from_preset
from gp_hosting.hc_store import HcStore

ROOT = Path(__file__).resolve().parent.parent
CASE_AMP = ROOT / "data" / "cases" / "ieee33_ampacity.json"
CASE_33 = ROOT / "data" / "cases" / "ieee33.json"
PROFILES = ROOT / "data" / "profiles"
SB_LOAD = PROFILES / "simbench_pv_max_day_20160513_load.csv"
SB_PV = PROFILES / "simbench_pv_max_day_20160513_pv.csv"


# --------------------------------------------------------------------------
# 1. 可行性判定（构造时序结果）
# --------------------------------------------------------------------------


def _fake_result(vm, ll, tl=None, converged=None):
    vm = np.asarray(vm, dtype=float)
    ll = np.asarray(ll, dtype=float)
    n = vm.shape[0]
    tl = np.zeros((n, 0)) if tl is None else np.asarray(tl, dtype=float)
    return SimpleNamespace(
        vm_pu=vm,
        line_loading=ll,
        trafo_loading=tl,
        converged=np.ones(n, dtype=bool) if converged is None else np.asarray(converged),
        bus_index=list(range(10, 10 + vm.shape[1])),
        line_index=list(range(100, 100 + ll.shape[1])),
        trafo_index=list(range(tl.shape[1])),
    )


class TestCheckTimeseries:
    def test_all_within_limits_is_feasible(self):
        r = _fake_result(np.full((4, 3), 1.0), np.full((4, 2), 50.0))
        rep = check_timeseries(r, Constraints())
        assert rep.feasible and rep.violations == [] and rep.first is None

    def test_exactly_at_limit_is_feasible(self):
        """限值是闭区间：等于 1.07 / 0.93 / 100% 都算合规（与 I1 单断面判定口径一致）。"""
        vm = [[1.07, 0.93], [1.0, 1.0]]
        ll = [[100.0], [10.0]]
        assert check_timeseries(_fake_result(vm, ll), Constraints()).feasible

    def test_overvoltage_reports_element_step_and_worst(self):
        vm = np.ones((96, 3))
        vm[50, 2] = 1.08
        vm[52, 2] = 1.09  # 最严重
        vm[54, 2] = 1.075
        rep = check_timeseries(_fake_result(vm, np.zeros((96, 1))), Constraints())
        assert not rep.feasible
        v = rep.first
        assert (v.constraint, v.element_type, v.element_index) == ("voltage_high", "bus", 12)
        assert (v.first_step, v.first_time) == (50, "12:30")
        assert (v.worst_step, v.worst_value) == (52, pytest.approx(1.09))
        assert v.n_steps == 3
        assert v.severity == pytest.approx((1.09 - 1.07) / 1.07)

    def test_violations_sorted_by_first_step(self):
        vm = np.ones((96, 2))
        vm[80, 0] = 0.90       # 20:00 低压
        ll = np.zeros((96, 1))
        ll[48, 0] = 120.0      # 12:00 过载，更早
        rep = check_timeseries(_fake_result(vm, ll), Constraints())
        assert [v.constraint for v in rep.violations] == ["line_overload", "voltage_low"]
        assert rep.constraint_types == ["line_overload", "voltage_low"]

    def test_trafo_overload_detected(self):
        r = _fake_result(np.ones((2, 1)), np.zeros((2, 1)), tl=[[50.0], [101.0]])
        rep = check_timeseries(r, Constraints())
        assert rep.first.constraint == "trafo_overload" and rep.first.first_step == 1

    def test_unconverged_step_is_infeasible_not_skipped(self):
        vm = np.ones((4, 2))
        vm[2, :] = np.nan
        rep = check_timeseries(_fake_result(vm, np.zeros((4, 1)), converged=[1, 1, 0, 1]), Constraints())
        assert not rep.feasible
        assert rep.unconverged_steps == [2]
        assert rep.first.constraint == "not_converged"
        json.dumps(rep.to_dict())  # 不得含 inf / nan 之类不可序列化值

    def test_custom_constraints_respected(self):
        vm = np.full((2, 1), 1.08)
        assert not check_timeseries(_fake_result(vm, np.zeros((2, 1))), Constraints()).feasible
        relaxed = constraints_from_preset("relaxed_0.90_1.10")
        assert check_timeseries(_fake_result(vm, np.zeros((2, 1))), relaxed).feasible


class TestConstraintsConfig:
    def test_default_is_gbt(self):
        assert Constraints() == constraints_from_preset("gbt_12325")

    def test_override(self):
        c = constraints_from_preset("gbt_12325", vm_max_pu=1.05, vm_min_pu=None)
        assert (c.vm_min_pu, c.vm_max_pu) == (0.93, 1.05)

    def test_unknown_preset(self):
        with pytest.raises(ValueError, match="未知约束预设"):
            constraints_from_preset("ieee_9999")

    @pytest.mark.parametrize("kw", [{"vm_min_pu": 1.1}, {"vm_min_pu": 0}, {"line_loading_percent_max": 0}])
    def test_invalid_rejected(self, kw):
        with pytest.raises(ValueError):
            Constraints(**kw)

    def test_options_tol_must_be_finer_than_probe(self):
        with pytest.raises(ValueError, match="tol_mw"):
            hc.HcOptions(tol_mw=0.05, probe_mw=0.05)


# --------------------------------------------------------------------------
# 2. 单调性判定（构造评估点）
# --------------------------------------------------------------------------


def _pt(c, feasible, max_vm=1.0, line=50.0, min_vm=0.95):
    return hc.EvalPoint(
        capacity_mw=c, feasible=feasible, min_vm_pu=min_vm, max_vm_pu=max_vm,
        max_line_loading_percent=line, max_trafo_loading_percent=None,
        n_unconverged=0, first_violation=None if feasible else "voltage_high", phase="coarse",
    )


class TestMonotonicity:
    def test_monotonic_prefix(self):
        pts = [_pt(c, c <= 3, max_vm=1.0 + 0.02 * c, line=50 + c) for c in range(6)]
        m = hc.check_monotonicity(pts)
        assert m.monotonic and m.reasons == [] and m.min_vm_trend == "flat"

    def test_feasibility_revival_detected_with_reason(self):
        feas = [True, True, False, True, False]
        pts = [_pt(c, f, max_vm=1.0 + 0.01 * c) for c, f in enumerate(feas)]
        m = hc.check_monotonicity(pts)
        assert not m.monotonic
        assert any("可行性非前缀" in r and "3.000" in r for r in m.reasons)

    def test_max_vm_decrease_detected(self):
        pts = [_pt(0, True, 1.00), _pt(1, True, 1.03), _pt(2, True, 1.02), _pt(3, False, 1.08)]
        m = hc.check_monotonicity(pts)
        assert not m.monotonic and any("max_vm_pu" in r for r in m.reasons)

    def test_line_loading_decrease_detected(self):
        """光伏倒送初期抵消负荷，线路电流先降后升 —— 这正是现实中可能出现的非单调来源。"""
        pts = [_pt(0, True, line=70), _pt(1, True, line=40), _pt(2, True, line=80)]
        m = hc.check_monotonicity(pts)
        assert not m.monotonic and any("max_line_loading_percent" in r for r in m.reasons)

    def test_unsorted_input_handled(self):
        pts = [_pt(2, False, 1.1), _pt(0, True, 1.0), _pt(1, True, 1.05)]
        assert hc.check_monotonicity(pts).monotonic


# --------------------------------------------------------------------------
# 3. 搜索算法（假评估器，可行性函数解析已知）
# --------------------------------------------------------------------------


class _FakeEvaluator:
    """替换 _Evaluator：feasible(c) 由给定函数决定，指标随容量线性变化。"""

    feasible_fn = staticmethod(lambda c: c <= 2.345)
    vm_fn = staticmethod(lambda c: 1.0 + 0.01 * c)

    def __init__(self, scenario, constraints):
        self._cache = {}

    def __call__(self, c, phase):
        k = round(float(c), 9)
        if k not in self._cache:
            ok = bool(self.feasible_fn(k))
            first = None if ok else SimpleNamespace(
                constraint="voltage_high", element_type="bus", element_index=17,
                first_step=52, first_time="13:00", worst_value=1.071,
            )
            report = SimpleNamespace(
                feasible=ok, first=first, violations=[] if ok else [first],
                constraint_types=[] if ok else ["voltage_high"],
                to_dict=lambda ok=ok: {"feasible": ok},
            )
            self._cache[k] = (_pt(k, ok, max_vm=self.vm_fn(k), line=50.0), report)
        p, r = self._cache[k]
        return hc.EvalPoint(**{**p.to_dict(), "phase": phase}), r

    @property
    def n_evaluations(self):
        return len(self._cache)

    def points(self):
        return sorted((p for p, _ in self._cache.values()), key=lambda p: p.capacity_mw)


@pytest.fixture()
def fake_scenario():
    return SimpleNamespace(
        spec=SimpleNamespace(
            sgens=[ts.SgenInjection(bus=17)], case_path="x", load_scale=1.0,
        ),
        pv_profile=SimpleNamespace(name="pv"),
        load_profile=SimpleNamespace(name="load"),
        case_spec=SimpleNamespace(meta=SimpleNamespace(name="fake")),
        n_steps=96,
    )


@pytest.fixture()
def fake_eval(monkeypatch):
    monkeypatch.setattr(hc, "_Evaluator", _FakeEvaluator)
    yield _FakeEvaluator
    _FakeEvaluator.feasible_fn = staticmethod(lambda c: c <= 2.345)
    _FakeEvaluator.vm_fn = staticmethod(lambda c: 1.0 + 0.01 * c)


class TestSearch:
    def test_bisection_hits_known_boundary(self, fake_eval, fake_scenario):
        r = hc.solve_single(fake_scenario)
        assert r.search_method == "bisection"
        assert 2.345 - 0.010 <= r.max_mw <= 2.345
        assert r.probe["passed"] is True
        assert r.limiting_type == "voltage_high" and r.limiting_time == "13:00"

    def test_boundary_exactly_on_grid_point(self, fake_eval, fake_scenario):
        """边界恰好落在粗扫网格点（括号 4 MW 的 10 等分 -> 0.4 的倍数）。"""
        fake_eval.feasible_fn = staticmethod(lambda c: c <= 2.4)
        r = hc.solve_single(fake_scenario)
        assert r.max_mw == pytest.approx(2.4, abs=1e-9)
        assert r.probe["passed"]

    def test_tolerance_respected(self, fake_eval, fake_scenario):
        fake_eval.feasible_fn = staticmethod(lambda c: c <= 7.77777)
        r = hc.solve_single(fake_scenario, options=hc.HcOptions(tol_mw=0.001, probe_mw=0.005))
        assert 7.77777 - 0.001 <= r.max_mw <= 7.77777

    def test_boundary_below_initial_bracket(self, fake_eval, fake_scenario):
        fake_eval.feasible_fn = staticmethod(lambda c: c <= 0.123)
        r = hc.solve_single(fake_scenario)
        assert 0.113 <= r.max_mw <= 0.123

    def test_not_reached(self, fake_eval, fake_scenario):
        fake_eval.feasible_fn = staticmethod(lambda c: True)
        r = hc.solve_single(fake_scenario, options=hc.HcOptions(max_capacity_mw=5.0))
        assert r.limiting_type == "not_reached" and r.max_mw == 5.0
        assert r.probe["skipped"]

    def test_baseline_infeasible(self, fake_eval, fake_scenario):
        fake_eval.feasible_fn = staticmethod(lambda c: False)
        r = hc.solve_single(fake_scenario)
        assert r.max_mw == 0.0 and r.limiting_type == "baseline_infeasible"
        assert r.search_method == "none" and r.iterations == 1

    def test_non_monotonic_falls_back_to_linear_and_is_conservative(self, fake_eval, fake_scenario):
        """可行域 = [0, 1.5] ∪ [1.7, 3.0]，即括号内有一段"不可行孤岛"。
        直接二分（2 MW 可行、4 MW 不可行）会收敛到 3.0，跳过孤岛给出偏大结果。
        粗扫在 1.6 MW 命中孤岛 -> 判定非单调 -> 线性扫描 -> 取第一段边界 1.5。"""
        fake_eval.feasible_fn = staticmethod(lambda c: c <= 1.5 or 1.7 <= c <= 3.0)
        r = hc.solve_single(fake_scenario)
        assert r.monotonicity.monotonic is False
        assert r.monotonicity.reasons, "非单调必须给出原因，不允许静默降级"
        assert r.search_method == "linear"
        assert 1.49 <= r.max_mw <= 1.5
        assert r.probe["passed"]

    def test_revival_beyond_bracket_does_not_inflate_result(self, fake_eval, fake_scenario):
        """可行域 = [0, 1.5] ∪ [2.5, 3.0]：第二段在括号上界 2 MW 之外，搜索根本不会进入，
        结果仍是保守的 1.5 —— 与承载力定义"[0, C*] 全部可行"一致。"""
        fake_eval.feasible_fn = staticmethod(lambda c: c <= 1.5 or 2.5 <= c <= 3.0)
        r = hc.solve_single(fake_scenario)
        assert 1.49 <= r.max_mw <= 1.5
        assert r.probe["passed"]

    def test_curve_has_at_least_10_points_and_consistent(self, fake_eval, fake_scenario):
        r = hc.solve_single(fake_scenario)
        assert len(r.curve) >= 10
        caps = [p.capacity_mw for p in r.curve]
        assert caps == sorted(caps)
        assert all(p.feasible for p in r.curve if p.capacity_mw <= r.max_mw)
        assert not any(p.feasible for p in r.curve if p.capacity_mw > r.max_mw)

    def test_requires_exactly_one_sgen(self, fake_scenario):
        fake_scenario.spec.sgens = []
        with pytest.raises(ts.ProfileError, match="恰好 1 个"):
            hc.solve_single(fake_scenario)


# --------------------------------------------------------------------------
# 4. 真实算例验收（排期 I3 DoD）
# --------------------------------------------------------------------------

MAIN_FEEDER = [1, 5, 8, 12, 17]   # 主干线由首到末
ACCEPT_BUSES = MAIN_FEEDER + [24, 32]


def _base_spec(case=CASE_AMP, load=SB_LOAD, pv=SB_PV, load_scale=1.0):
    return ts.ScenarioSpec(case_path=str(case), load_profile_path=str(load), pv_profile_path=str(pv),
                           load_scale=load_scale)


@pytest.fixture(scope="module")
def batch():
    results = hc.solve_batch(_base_spec(), ACCEPT_BUSES, Constraints())
    return {r.bus: r for r in results}


class TestAcceptance:
    def test_probe_both_directions(self, batch):
        """强制项：max_mw 下全天无违规；max_mw + 50 kW 至少 1 处违规。"""
        for bus, r in batch.items():
            assert r.at_max.feasible, f"bus{bus} 在 max_mw 下仍有违规"
            assert r.probe["passed"], f"bus{bus} 探针未通过: {r.probe}"
            assert r.probe["above"]["n_violations"] >= 1

    def test_probe_independent_recheck(self, batch):
        """不信任求解器自己的缓存：重新构网、独立跑两次全天潮流复核 bus17。"""
        r = batch[17]
        spec = _base_spec()
        spec.sgens = [ts.SgenInjection(bus=17)]
        runner = ts.TimeseriesRunner(ts.assemble_scenario(spec))
        assert check_timeseries(runner.run([r.max_mw]), Constraints()).feasible
        assert not check_timeseries(runner.run([r.max_mw + 0.050]), Constraints()).feasible

    def test_monotonic_on_all_nodes(self, batch):
        for bus, r in batch.items():
            assert r.monotonicity.monotonic, f"bus{bus}: {r.monotonicity.reasons}"
            assert r.search_method == "bisection"

    def test_min_voltage_independent_of_capacity(self, batch):
        """排期写"容量↑ → 最差电压↓"，对光伏不成立：全天最低电压出现在无光照时段，与容量无关。"""
        for r in batch.values():
            assert r.monotonicity.min_vm_trend == "flat"

    def test_capacity_decreases_along_main_feeder(self, batch):
        caps = [batch[b].max_mw for b in MAIN_FEEDER]
        assert caps == sorted(caps, reverse=True), dict(zip(MAIN_FEEDER, caps))

    def test_two_limiting_types_observed(self, batch):
        types = {r.limiting_type for r in batch.values()}
        assert {"voltage_high", "line_overload"} <= types
        assert batch[1].limiting_type == "line_overload" and batch[1].limiting_element_index == 0
        assert batch[17].limiting_type == "voltage_high" and batch[17].limiting_element_index == 17

    def test_limiting_time_is_daytime(self, batch):
        """光伏导致的越限只能发生在有出力的时段。"""
        pv = ts.load_profile(SB_PV, kind="pv").factors
        for r in batch.values():
            assert pv[r.limiting_step] > 0, f"bus{r.bus} 越限时刻 {r.limiting_time} 光伏出力为 0"

    def test_curve_points_and_consistency(self, batch):
        for r in batch.values():
            assert len(r.curve) >= 10
            assert all(p.feasible for p in r.curve if p.capacity_mw <= r.max_mw)
            assert not any(p.feasible for p in r.curve if p.capacity_mw > r.max_mw)

    def test_single_node_under_40s(self, batch):
        for r in batch.values():
            assert r.elapsed_ms < 40_000

    def test_deterministic_three_runs(self, batch):
        spec = _base_spec()
        spec.sgens = [ts.SgenInjection(bus=17)]
        runs = [hc.solve_single(ts.assemble_scenario(spec)) for _ in range(2)]
        for r in runs:
            assert r.max_mw == batch[17].max_mw
            assert r.limiting_time == batch[17].limiting_time
            assert [p.capacity_mw for p in r.curve] == [p.capacity_mw for p in batch[17].curve]


class TestBaselineInfeasibleReal:
    def test_ieee33_full_load_violates_gbt_lower_limit(self):
        """IEEE 33 在负荷峰值时最低电压 0.9131 < 0.93：GB/T 口径下基线即越限，承载力为 0。
        这是算例本身的属性（Baran & Wu 原始数据），不是程序错误。"""
        spec = _base_spec(case=CASE_33, load=PROFILES / "load_typical_day.csv", pv=PROFILES / "pv_typical_day.csv")
        spec.sgens = [ts.SgenInjection(bus=17)]
        r = hc.solve_single(ts.assemble_scenario(spec))
        assert r.max_mw == 0.0 and r.limiting_type == "baseline_infeasible"
        assert r.limiting_element_index == 17
        assert r.limiting_value == pytest.approx(0.91309, abs=1e-4)


class TestBatchValidation:
    def test_unknown_bus_rejected_before_compute(self):
        with pytest.raises(ts.ProfileError, match="不存在"):
            hc.solve_batch(_base_spec(), [17, 999])

    def test_duplicate_buses_rejected(self):
        with pytest.raises(ts.ProfileError, match="重复"):
            hc.solve_batch(_base_spec(), [17, 17])


class TestStore:
    def test_save_and_query(self, tmp_path, batch):
        store = HcStore(tmp_path)
        for r in batch.values():
            store.save("t1", r)
        rows = store.by_task("t1")
        assert [row["node"] for row in rows] == sorted(batch)
        row17 = next(row for row in rows if row["node"] == 17)
        assert row17["max_mw"] == batch[17].max_mw
        assert row17["limiting_type"] == "voltage_high" and row17["probe_passed"] == 1
        # 表里只存摘要，曲线在明细文件中
        assert "curve" not in row17
        assert len(store.detail("t1", 17)["curve"]) == len(batch[17].curve)
        assert store.detail("t1", 999) is None
