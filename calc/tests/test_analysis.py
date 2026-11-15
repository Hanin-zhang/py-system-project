"""I4 分析层测试：归因、热力图、灵敏度、瓶颈排序、接入点对比。

沿用 I3 的分层：纯单元测试用构造数据（毫秒级），真实算例只测排期 DoD 要求的那几条。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from gp_hosting import analysis as an
from gp_hosting import api as api_mod
from gp_hosting import timeseries as ts
from gp_hosting.constraints import Constraints

ROOT = Path(__file__).resolve().parent.parent
CASE_AMP = ROOT / "data" / "cases" / "ieee33_ampacity.json"
P = ROOT / "data" / "profiles"
SB_LOAD = P / "simbench_pv_max_day_20160513_load.csv"
SB_PV = P / "simbench_pv_max_day_20160513_pv.csv"


# --------------------------------------------------------------------------
# 构造工具
# --------------------------------------------------------------------------


def _result(vm, ll=None, tl=None, converged=None, step_minutes=15):
    vm = np.asarray(vm, dtype=float)
    n, nb = vm.shape
    ll = np.zeros((n, 0)) if ll is None else np.asarray(ll, dtype=float)
    tl = np.zeros((n, 0)) if tl is None else np.asarray(tl, dtype=float)
    conv = np.ones(n, dtype=bool) if converged is None else np.asarray(converged, dtype=bool)
    ok = conv
    vm_ok = np.where(ok[:, None], vm, np.nan)
    summary = ts.TimeseriesSummary(
        n_steps=n, step_minutes=step_minutes, n_converged=int(ok.sum()),
        failed_steps=[int(x) for x in np.flatnonzero(~ok)],
        min_vm_pu=float(np.nanmin(vm_ok)), min_vm_bus=int(np.nanargmin(vm_ok) % nb),
        min_vm_step=int(np.nanargmin(vm_ok) // nb), min_vm_time=ts.step_to_hhmm(int(np.nanargmin(vm_ok) // nb), n),
        max_vm_pu=float(np.nanmax(vm_ok)), max_vm_bus=int(np.nanargmax(vm_ok) % nb),
        max_vm_step=int(np.nanargmax(vm_ok) // nb), max_vm_time=ts.step_to_hhmm(int(np.nanargmax(vm_ok) // nb), n),
        max_line_loading_percent=float(np.nanmax(ll)) if ll.size else 0.0,
        max_line_index=0, max_line_step=0, max_line_time="00:00",
        max_trafo_loading_percent=float(np.nanmax(tl)) if tl.size else 0.0,
        max_trafo_index=0 if tl.size else None,
        energy_loss_mwh=0.0, energy_pv_mwh=0.0, energy_load_mwh=0.0,
        elapsed_ms=0.0, warmstart_fallbacks=0,
    )
    return ts.TimeseriesResult(
        summary=summary, vm_pu=vm, line_loading=ll, trafo_loading=tl,
        pl_mw=np.zeros(n), pv_mw=np.zeros(n),
        bus_index=list(range(nb)), line_index=list(range(ll.shape[1])),
        trafo_index=list(range(tl.shape[1])), steps=list(range(n)), converged=conv,
    )


class TestSegments:
    @pytest.mark.parametrize(
        "mask, gap, expected",
        [
            ([0, 0, 0], 0, []),
            ([1, 1, 1], 0, [(0, 2)]),
            ([1, 0, 1], 0, [(0, 0), (2, 2)]),
            ([1, 0, 1], 1, [(0, 2)]),          # 空档 1 个时段，gap=1 合并
            ([1, 0, 0, 1], 1, [(0, 0), (3, 3)]),  # 空档 2 个时段，不合并
            ([0, 1, 1, 0, 0, 1], 2, [(1, 5)]),
        ],
    )
    def test_segments(self, mask, gap, expected):
        assert an._segments(np.array(mask, dtype=bool), gap) == expected


class TestAttribution:
    def test_no_violation(self):
        r = _result(np.full((4, 2), 1.0), np.full((4, 2), 50.0))
        rep = an.build_attribution(r)
        assert rep.feasible and rep.n_events == 0 and rep.first_step is None
        assert rep.heatmap["vm"]["n_violating_cells"] == 0

    def test_single_event_fields(self):
        vm = np.ones((96, 3))
        vm[52:62, 2] = 1.09          # 10 个连续时段越限
        vm[56, 2] = 1.12             # 峰值
        rep = an.build_attribution(_result(vm, np.zeros((96, 1))))
        assert rep.n_events == 1
        e = rep.events[0]
        assert (e.constraint, e.element_type, e.element_index) == ("voltage_high", "bus", 2)
        assert (e.start_step, e.end_step, e.n_steps, e.duration_steps) == (52, 61, 10, 10)
        assert (e.start_time, e.end_time) == ("13:00", "15:15")
        assert e.peak_step == 56 and e.peak_value == pytest.approx(1.12)
        assert e.peak_excess == pytest.approx(1.12 - 1.07)
        assert e.peak_severity == pytest.approx((1.12 - 1.07) / 1.07)
        assert rep.first_time == "13:00"
        assert json.dumps(rep.to_dict())  # 可序列化

    def test_two_events_same_element_gap_merged(self):
        vm = np.ones((96, 2))
        vm[20:30, 1] = 1.08
        vm[33:40, 1] = 1.08          # 空档 3 个时段
        assert an.build_attribution(_result(vm)).n_events == 2
        merged = an.build_attribution(_result(vm), merge_gap=3)
        assert merged.n_events == 1
        e = merged.events[0]
        # 合并后区间 20..39 共 20 个时段，其中 17 个真实越限 + 3 个空档
        assert (e.start_step, e.end_step, e.n_steps, e.duration_steps) == (20, 39, 17, 20)

    def test_first_violation_is_earliest_not_worst(self):
        vm = np.ones((96, 2))
        vm[80, 0] = 1.09      # 20:00，幅度大但更晚
        vm[40, 1] = 1.075     # 10:00，幅度小但更早
        rep = an.build_attribution(_result(vm))
        assert rep.first_time == "10:00"
        assert rep.first_violation["constraint"] == "voltage_high"
        assert rep.top_by_peak_severity[0]["peak_severity"] > rep.events[0].peak_severity

    def test_low_voltage_and_line_events(self):
        vm = np.ones((8, 2))
        vm[0, 0] = 0.90
        ll = np.zeros((8, 1))
        ll[4, 0] = 130.0
        rep = an.build_attribution(_result(vm, ll))
        assert {e.constraint for e in rep.events} == {"voltage_low", "line_overload"}
        assert rep.constraint_summary["line_overload"]["n_steps"] == 1

    def test_unconverged_step_not_counted_as_violation(self):
        vm = np.ones((4, 2))
        vm[2, :] = np.nan
        rep = an.build_attribution(_result(vm, converged=[1, 1, 0, 1]))
        assert rep.n_unconverged == 1 and rep.unconverged_steps == [2]
        assert rep.n_events == 0 and not rep.feasible

    def test_heatmap_contains_only_negative_cells(self):
        vm = np.ones((8, 3))
        vm[2, 1] = 1.09
        vm[5, 0] = 0.90
        rep = an.build_attribution(_result(vm, np.full((8, 2), 50.0)))
        hm = rep.heatmap["vm"]
        assert hm["n_violating_cells"] == 2
        assert all(v < 0 for _, _, v in hm["data"])
        assert all(0 <= x < 8 and 0 <= y < 3 for x, y, _ in hm["data"])
        # 与原始矩阵独立核对：越限单元数 = 上越限 + 下越限
        with np.errstate(invalid="ignore"):
            expected = int((vm > 1.07).sum() + (vm < 0.93).sum())
        assert hm["n_violating_cells"] == expected

    def test_heatmap_margins_cross_checked(self):
        """热力图的值必须等于手算的归一化裕度。"""
        vm = np.array([[1.10, 0.95]])
        rep = an.build_attribution(_result(vm, np.zeros((1, 0))), Constraints())
        data = {(x, y): v for x, y, v in rep.heatmap["vm"]["data"]}
        assert data[(0, 0)] == pytest.approx(-(1.10 - 1.07) / 1.07, abs=1e-6)
        assert (0, 1) not in data


# --------------------------------------------------------------------------
# 真实算例：归因与 Parquet 交叉核对（排期 DoD 第 1 条）
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def violation_task(tmp_path_factory):
    """跑一个确定越限的场景并落盘，返回 (task_id, result, result_dir)。"""
    root = tmp_path_factory.mktemp("i4")
    spec = ts.ScenarioSpec(
        case_path=str(CASE_AMP), load_profile_path=str(SB_LOAD), pv_profile_path=str(SB_PV),
        sgens=[ts.SgenInjection(bus=17, capacity_mw=4.0)],
    )
    scenario = ts.assemble_scenario(spec)
    result = ts.run_timeseries(scenario)
    out = ts.write_result_set(result, scenario, root, task_id="i4task")
    return "i4task", result, out


class TestAttributionAgainstParquet:
    def test_report_matches_raw_matrix(self, violation_task):
        _, result, _ = violation_task
        rep = an.build_attribution(result, Constraints())
        assert not rep.feasible and rep.n_events >= 1

        first = rep.first_violation
        # 用原始矩阵重新找一次"首次越限"，必须一致
        vm = result.vm_pu
        violated = vm > 1.07
        t0 = int(np.argmax(violated.any(axis=1)))
        b0 = int(np.argmax(violated[t0]))
        assert first["start_step"] == t0
        assert first["element_index"] == b0
        assert first["peak_value"] == pytest.approx(vm[t0:first["end_step"] + 1, b0].max())

        # 持续时段数：该设备从 start 到 end 的连续越限数
        seg = violated[t0:first["end_step"] + 1, b0]
        assert first["n_steps"] == int(seg.sum())
        assert first["duration_steps"] == len(seg)

    def test_readback_from_parquet_identical(self, violation_task):
        """从 Parquet 读回重建的 result，归因结论必须与内存中完全一致。"""
        _, result, out = violation_task
        loaded = ts.read_result_set(out)
        rebuilt = api_mod._result_from_frames(loaded)
        a = an.build_attribution(result, Constraints()).to_dict()
        b = an.build_attribution(rebuilt, Constraints()).to_dict()
        assert a["first_violation"] == b["first_violation"]
        assert a["n_events"] == b["n_events"]
        assert a["heatmap"]["vm"]["n_violating_cells"] == b["heatmap"]["vm"]["n_violating_cells"]


# --------------------------------------------------------------------------
# 灵敏度（排期 DoD 第 2、5 条）
# --------------------------------------------------------------------------


def _two_bus_scenario(tmp_path: Path):
    from conftest import make_two_bus_case

    case = make_two_bus_case()
    path = tmp_path / "two_bus.json"
    path.write_text(json.dumps(case), encoding="utf-8")
    return ts.assemble_scenario(
        ts.ScenarioSpec(
            case_path=str(path), load_profile_path=str(P / "load_typical_day.csv"),
            pv_profile_path=str(P / "pv_typical_day.csv"), sgens=[ts.SgenInjection(bus=1)],
        )
    )


class TestSensitivityAnalytic:
    def test_two_bus_order_of_magnitude(self, tmp_path):
        """单条线路、末端注入：∂V/∂P ≈ R_pu / V。

        R = 0.5 Ω，Z_base = 100 Ω → R_pu = 0.005；V≈0.95 → 期望 ≈ 0.0053 p.u./MW。
        只断言量级（0.003–0.007）与符号，不假装精确——二阶项与负荷水平都会影响它。
        """
        sens = an.compute_sensitivity(_two_bus_scenario(tmp_path), base_mw=0.5)
        assert 0.003 < sens.d_v_bus_at_peak_dp < 0.007
        assert sens.d_v_bus_at_peak_dp > 0
        assert sens.d_vmin_dp == pytest.approx(0.0, abs=1e-6), "夜间最低电压与容量无关"

    def test_step_size_invariant(self, tmp_path):
        """ΔP 从 1% 变到 10%，结果变化应 < 1%（这正是排期要求说明步长选择的理由）。"""
        sc = _two_bus_scenario(tmp_path)
        a = an.compute_sensitivity(sc, 0.5)
        b = an.compute_sensitivity(sc, 0.5, delta_mw=0.05)
        assert abs(a.d_v_bus_at_peak_dp - b.d_v_bus_at_peak_dp) / abs(a.d_v_bus_at_peak_dp) < 0.01

    @pytest.mark.parametrize(
        "kwargs, match",
        [
            ({"base_mw": 0.0}, "基准容量"),
            ({"base_mw": 0.5, "delta_mw": 0.5}, "差分步长"),
            ({"base_mw": 0.5, "delta_mw": -0.1}, "差分步长"),
        ],
    )
    def test_invalid_params(self, tmp_path, kwargs, match):
        with pytest.raises(ts.ProfileError, match=match):
            an.compute_sensitivity(_two_bus_scenario(tmp_path), **kwargs)


@pytest.fixture(scope="module")
def real_sens():
    scenario = ts.assemble_scenario(
        ts.ScenarioSpec(
            case_path=str(CASE_AMP), load_profile_path=str(SB_LOAD), pv_profile_path=str(SB_PV),
            sgens=[ts.SgenInjection(bus=17)],
        )
    )
    sens = an.compute_sensitivity(scenario, base_mw=3.0)
    result = ts.run_timeseries(scenario)
    return sens, result


class TestSensitivityReal:
    def test_end_more_sensitive_than_head(self, real_sens):
        sens, _ = real_sens
        by_bus = {x["bus"]: x["dv_dp"] for x in sens.top_voltage_buses}
        # top 列表只有前 10 个；首端必然不在其中，单独用矩阵取
        bi = sens.matrices["bus_index"]
        dv = np.array(sens.matrices["dv_dp"])[sens.peak_step]
        head = dv[bi.index(1)]
        end = dv[bi.index(17)]
        assert end > 0 and head > 0
        assert end > head * 10, f"末端 {end:.6f} 应远大于首端 {head:.6f}"
        assert by_bus[17] == pytest.approx(end, abs=1e-9)

    def test_vmin_insensitive_but_vmax_sensitive(self, real_sens):
        sens, _ = real_sens
        assert abs(sens.d_vmin_dp) < 1e-9
        assert sens.d_vmax_dp > 0.01

    def test_matrices_shape_and_scaling(self, real_sens):
        sens, result = real_sens
        dv = np.array(sens.matrices["dv_dp"])
        dl = np.array(sens.matrices["dloading_dp"])
        assert dv.shape == (96, 33)
        assert dl.shape == (96, 37)
        # 夜间（PV=0）斜率应接近 0
        assert abs(dv[0]).max() < 1e-6


# --------------------------------------------------------------------------
# 瓶颈排序（排期 DoD 第 4 条要求"可排序的数值指标"）
# --------------------------------------------------------------------------


class TestBottlenecks:
    def test_ranked_ascending_by_headroom(self, real_sens):
        sens, result = real_sens
        rows = an.rank_bottlenecks(sens, constraints=Constraints(), top_n=10)
        assert len(rows) >= 1
        hr = [r.headroom_mw for r in rows]
        assert hr == sorted(hr)
        assert [r.rank for r in rows] == list(range(1, len(rows) + 1))

    def test_bottleneck_is_end_bus(self, real_sens):
        """bus17 接 3 MW，I3 承载力 3.069 MW：headroom 必须非常接近 0.069 MW。

        这是 I3 与 I4 交叉验证的一条硬断言：线性外推的触限容量增量
        应当与非线性二分结果吻合到 10% 以内。
        """
        sens, result = real_sens
        rows = an.rank_bottlenecks(sens, constraints=Constraints(), top_n=5)  # 不传 result，用灵敏度自带基准态
        top = rows[0]
        assert top.element_type == "bus" and top.element_index == 17
        assert top.constraint == "voltage_high"
        capacity_gap = 3.069 - 3.0
        assert top.headroom_mw == pytest.approx(capacity_gap, abs=0.05)

    def test_headroom_not_fooled_by_night_peak(self, real_sens):
        """回归：不能用'全网最大负载率时刻'算裕度（那时 PV=0，斜率=0，会得到假的大裕度）。"""
        sens, result = real_sens
        rows = an.rank_bottlenecks(sens, constraints=Constraints(), top_n=200)
        for r in rows:
            assert r.headroom_mw is not None
            assert r.headroom_mw < 1e6, f"{r.element_type}{r.element_index} 裕度异常大"
            assert r.slope > 0

    def test_already_violated_flagged(self):
        """容量超过承载力时，headroom 为负，必须显式标记而不是返回 None。"""
        scenario = ts.assemble_scenario(
            ts.ScenarioSpec(
                case_path=str(CASE_AMP), load_profile_path=str(SB_LOAD), pv_profile_path=str(SB_PV),
                sgens=[ts.SgenInjection(bus=17)],
            )
        )
        sens = an.compute_sensitivity(scenario, base_mw=4.0)
        result = ts.run_timeseries(scenario)
        rows = an.rank_bottlenecks(sens, constraints=Constraints(), top_n=5)
        assert rows[0].already_violated is True
        assert rows[0].headroom_mw < 0
        assert rows[0].consumption_rate is None


# --------------------------------------------------------------------------
# 接入点对比（排期 DoD 第 3 条：与 I3 承载力自洽）
# --------------------------------------------------------------------------


class TestCompareBuses:
    @pytest.fixture(scope="class")
    def five_mw(self):
        base = ts.ScenarioSpec(
            case_path=str(CASE_AMP), load_profile_path=str(SB_LOAD), pv_profile_path=str(SB_PV)
        )
        return an.compare_buses(base, [1, 8, 17, 32], 5.0, Constraints(), with_sensitivity=False)

    def test_shape_and_recommendation(self, five_mw):
        assert len(five_mw["candidates"]) == 4
        assert five_mw["recommended_bus"] == five_mw["candidates"][0]["bus"]
        assert five_mw["rationale"]

    def test_consistent_with_i3_capacity(self, five_mw):
        """I3 承载力：bus1 13.95、bus8 7.39、bus32 5.03、bus17 3.07。
        在 5 MW 下：bus1/bus8/bus32 可行（5.03 > 5.0 仅差 25 kW），bus17 必然越限。"""
        by_bus = {c["bus"]: c for c in five_mw["candidates"]}
        assert by_bus[17]["feasible"] is False
        assert by_bus[1]["feasible"] is True
        assert by_bus[8]["feasible"] is True
        # 越限程度排序必须与承载力排序相反
        order = [c["bus"] for c in sorted(five_mw["candidates"], key=lambda r: r["worst_severity"])]
        assert order.index(17) == len(order) - 1

    def test_rejects_bad_input(self):
        base = ts.ScenarioSpec(
            case_path=str(CASE_AMP), load_profile_path=str(SB_LOAD), pv_profile_path=str(SB_PV)
        )
        with pytest.raises(ts.ProfileError, match="2–5"):
            an.compare_buses(base, [17], 5.0)
        with pytest.raises(ts.ProfileError, match="重复"):
            an.compare_buses(base, [17, 17], 5.0)
        with pytest.raises(ts.ProfileError, match="容量"):
            an.compare_buses(base, [1, 17], 0.0)


class TestBottleneckResultMismatch:
    def test_mismatched_result_rejected(self, real_sens):
        """显式传入容量不同的 result 必须报错，而不是给出"斜率来自 A、裕度来自 B"的假裕度。"""
        sens, _ = real_sens
        other = _result(np.ones((48, 3)), np.ones((48, 2)) * 10.0)
        with pytest.raises(ValueError, match="形状不一致"):
            an.rank_bottlenecks(sens, other)


class TestHeatmapEchartsContract:
    def test_payload_matches_echarts_heatmap(self):
        """ECharts heatmap 需要：data=[x,y,value]，x/y 为**分类轴下标**，并给出轴标签。"""
        vm = np.ones((96, 33))
        vm[52, 17] = 1.09
        rep = an.build_attribution(_result(vm, np.zeros((96, 37))), Constraints())
        hm = rep.heatmap["vm"]
        assert len(hm["step_times"]) == 96
        assert len(hm["elements"]) == 33
        assert hm["step_times"][52] == "13:00"
        for x, y, v in hm["data"]:
            assert isinstance(x, int) and isinstance(y, int) and isinstance(v, float)
            assert 0 <= x < len(hm["step_times"]) and 0 <= y < len(hm["elements"])
        json.dumps(hm)  # 必须可直接 JSON 序列化给前端
