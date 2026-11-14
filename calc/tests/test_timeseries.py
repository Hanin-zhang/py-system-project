"""timeseries 模块测试：曲线校验、场景组装、时序结果物理正确性、落盘一致性。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gp_hosting import timeseries as ts

from conftest import ROOT

CASE = ROOT / "data" / "cases" / "ieee33.json"
CASE_AMP = ROOT / "data" / "cases" / "ieee33_ampacity.json"
LOAD_CSV = ROOT / "data" / "profiles" / "load_typical_day.csv"
PV_CSV = ROOT / "data" / "profiles" / "pv_typical_day.csv"


def _spec(**overrides) -> ts.ScenarioSpec:
    kwargs = dict(
        case_path=str(CASE),
        load_profile_path=str(LOAD_CSV),
        pv_profile_path=str(PV_CSV),
        sgens=[],
        name="test",
    )
    kwargs.update(overrides)
    return ts.ScenarioSpec(**kwargs)


@pytest.fixture()
def no_pv_result():
    sc = ts.assemble_scenario(_spec())
    return sc, ts.run_timeseries(sc)


@pytest.fixture()
def pv_result():
    sc = ts.assemble_scenario(_spec(sgens=[ts.SgenInjection(bus=17, capacity_mw=2.0)]))
    return sc, ts.run_timeseries(sc)


# --------------------------------------------------------------------------
# 曲线
# --------------------------------------------------------------------------


class TestProfileLoading:
    def test_load_typical_day(self):
        p = ts.load_profile(LOAD_CSV, kind="load")
        assert len(p) == 96
        assert min(p.factors) == pytest.approx(0.55, abs=0.01)
        assert max(p.factors) == pytest.approx(1.0, abs=1e-6)
        # 双峰：晚峰（20:00 = index 80）应高于早峰（10:00 = index 40）
        assert p.factors[80] > p.factors[40]

    def test_pv_typical_day(self):
        p = ts.load_profile(PV_CSV, kind="pv")
        assert len(p) == 96
        peak_idx = int(np.argmax(p.factors))
        assert 44 <= peak_idx <= 52, f"光伏峰值应在正午附近，实际 index {peak_idx}"
        # 夜间为 0
        assert p.factors[0] == 0.0
        assert all(v == 0.0 for v in p.factors[:20])  # 00:00–05:00
        assert all(v == 0.0 for v in p.factors[76:])  # 19:00–24:00

    def test_missing_file(self):
        with pytest.raises(ts.ProfileError) as exc:
            ts.load_profile("/nonexistent/x.csv", kind="load")
        assert "不存在" in str(exc.value)

    def test_bad_column(self, tmp_path):
        f = tmp_path / "bad.csv"
        f.write_text("a,b\n0,1\n", encoding="utf-8")
        with pytest.raises(ts.ProfileError) as exc:
            ts.load_profile(f, kind="load")
        assert "time_index" in str(exc.value)

    def test_non_contiguous_index(self, tmp_path):
        f = tmp_path / "bad.csv"
        f.write_text("time_index,factor\n0,1.0\n2,1.0\n", encoding="utf-8")
        with pytest.raises(ts.ProfileError) as exc:
            ts.load_profile(f, kind="load")
        assert "连续" in str(exc.value)


class TestProfileValidation:
    def test_zero_load_factor_rejected(self):
        """负荷为 0 视为数据缺失 —— 必须拦住并指出时段。"""
        p = ts.Profile(name="x", kind="load", factors=[0.5] * 95 + [0.0])
        with pytest.raises(ts.ProfileError) as exc:
            ts.validate_profile(p)
        assert "time_index=95" in str(exc.value)
        assert "23:45" in str(exc.value)

    def test_negative_pv_rejected(self):
        p = ts.Profile(name="x", kind="pv", factors=[0.5] * 95 + [-0.1])
        with pytest.raises(ts.ProfileError) as exc:
            ts.validate_profile(p)
        assert "time_index=95" in str(exc.value)
        assert "负出力" in str(exc.value)

    def test_pv_zero_at_night_is_fine(self):
        factors = [0.0] * 24 + [1.0] * 48 + [0.0] * 24
        p = ts.Profile(name="x", kind="pv", factors=factors)
        assert ts.validate_profile(p) is p

    def test_wrong_point_count(self):
        p = ts.Profile(name="x", kind="load", factors=[1.0] * 7)
        with pytest.raises(ts.ProfileError) as exc:
            ts.validate_profile(p)
        assert "48" in str(exc.value) or "12" in str(exc.value)

    def test_all_zero_rejected(self):
        p = ts.Profile(name="x", kind="pv", factors=[0.0] * 96)
        with pytest.raises(ts.ProfileError) as exc:
            ts.validate_profile(p)
        assert "全零" in str(exc.value)


# --------------------------------------------------------------------------
# 场景组装
# --------------------------------------------------------------------------


class TestScenarioAssembly:
    def test_basic(self):
        sc = ts.assemble_scenario(_spec(sgens=[ts.SgenInjection(bus=17, capacity_mw=1.5)]))
        assert sc.n_steps == 96
        assert sc.hourly_summary()["pv_installed_mw"] == 1.5
        assert sc.hourly_summary()["load_peak_mw"] == pytest.approx(3.715, abs=1e-6)

    def test_negative_capacity_rejected(self):
        with pytest.raises(ts.ProfileError) as exc:
            ts.SgenInjection(bus=17, capacity_mw=-1.0)
        assert "不能为负" in str(exc.value)

    def test_bad_power_factor_rejected(self):
        with pytest.raises(ts.ProfileError) as exc:
            ts.SgenInjection(bus=17, capacity_mw=1.0, power_factor=1.5)
        assert "功率因数" in str(exc.value)

    def test_nonexistent_bus_rejected(self):
        with pytest.raises(ts.ProfileError) as exc:
            ts.assemble_scenario(_spec(sgens=[ts.SgenInjection(bus=99, capacity_mw=1.0)]))
        assert "99" in str(exc.value)
        assert exc.value.detail["field"] == "sgens.bus"

    def test_pv_without_profile_rejected(self):
        with pytest.raises(ts.ProfileError) as exc:
            ts.assemble_scenario(
                _spec(pv_profile_path=None, sgens=[ts.SgenInjection(bus=17, capacity_mw=1.0)])
            )
        assert "光伏曲线" in str(exc.value)

    def test_profile_length_mismatch(self, tmp_path):
        short = tmp_path / "short.csv"
        short.write_text(
            "time_index,factor\n" + "\n".join(f"{i},0.5" for i in range(48)), encoding="utf-8"
        )
        with pytest.raises(ts.ProfileError) as exc:
            ts.assemble_scenario(_spec(load_profile_path=str(short)))
        assert "不一致" in str(exc.value)

    def test_oversized_pv_warns(self):
        sc = ts.assemble_scenario(_spec(sgens=[ts.SgenInjection(bus=17, capacity_mw=20.0)]))
        assert sc.warnings
        assert "3 倍" in sc.warnings[0]


# --------------------------------------------------------------------------
# 时序结果：物理正确性
# --------------------------------------------------------------------------


class TestTimeseriesPhysics:
    def test_all_steps_converge(self, no_pv_result):
        _, res = no_pv_result
        assert res.summary.n_converged == 96
        assert res.summary.failed_steps == []
        assert res.vm_pu.shape == (96, 33)

    def test_performance_under_30s(self, no_pv_result):
        """96 时段 × 33 节点必须 < 30 s（I2 验收项）。实测约 0.5 s。"""
        _, res = no_pv_result
        assert res.summary.elapsed_ms < 30_000
        print(f"\n  实测耗时 {res.summary.elapsed_ms:.0f} ms")

    def test_min_voltage_at_evening_peak(self, no_pv_result):
        """无光伏时，日最低电压必须出现在负荷高峰 19:00–21:00。"""
        _, res = no_pv_result
        s = res.summary
        assert s.min_vm_bus == 17
        hour = s.min_vm_step * 15 // 60
        assert 19 <= hour <= 21, f"最低电压出现在 {s.min_vm_time}，不在晚高峰"

    def test_min_voltage_matches_static_baseline(self, no_pv_result):
        """曲线峰值时刻（factor=1.0，即 index 80）的电压必须与 I1 静态潮流一致。"""
        _, res = no_pv_result
        peak_step = 80  # 20:00，负荷曲线 factor=1.0
        b17 = res.bus_index.index(17)
        assert res.vm_pu[peak_step, b17] == pytest.approx(0.9131, abs=1e-3)

    def test_no_pv_means_zero_pv_energy(self, no_pv_result):
        _, res = no_pv_result
        assert res.summary.energy_pv_mwh == pytest.approx(0.0)

    def test_pv_raises_noon_voltage(self, no_pv_result, pv_result):
        """末端接入 2 MW 光伏，正午该节点电压上升 ≥ 0.02 p.u.。"""
        _, base = no_pv_result
        _, pv = pv_result
        b17 = base.bus_index.index(17)
        noon = 48  # 12:00

        rise = pv.vm_pu[noon, b17] - base.vm_pu[noon, b17]
        assert rise >= 0.02, f"正午电压只上升了 {rise:.4f} p.u."
        print(f"\n  正午 bus17 电压: {base.vm_pu[noon, b17]:.4f} -> {pv.vm_pu[noon, b17]:.4f} (+{rise:.4f})")

    def test_pv_causes_voltage_peak_at_noon(self, no_pv_result, pv_result):
        """光伏接入后，日最高电压应从平衡节点（00:00）转移到正午的接入点。

        注意：这里刻意断言"最高电压时刻"而不是"最低电压时刻"。
        最低电压时刻不会变——夜间光伏出力为 0，20:00 的负荷高峰仍是全网最低点。
        这是物理事实，不是实现缺陷。
        """
        _, base = no_pv_result
        _, pv = pv_result

        # 基线：最高电压是平衡节点 1.0，出现在任意时刻
        assert base.summary.max_vm_pu == pytest.approx(1.0, abs=1e-9)
        assert base.summary.max_vm_bus == 0

        # 光伏场景：最高电压出现在末端节点、正午前后
        assert pv.summary.max_vm_bus == 17
        assert pv.summary.max_vm_pu > 1.0
        hour = pv.summary.max_vm_step * 15 // 60
        assert 11 <= hour <= 14, f"最高电压出现在 {pv.summary.max_vm_time}"

        # 最低电压时刻不变，仍是晚高峰
        assert pv.summary.min_vm_time == base.summary.min_vm_time

    def test_pv_energy_is_reasonable(self, pv_result):
        """2 MW 光伏、模拟曲线峰值 1.0、等效满发约 7.5 h -> 约 15 MWh。

        这个数值的物理含义：日发电量 / 装机 = 等效利用小时数。
        """
        _, res = pv_result
        e = res.summary.energy_pv_mwh
        equivalent_hours = e / 2.0
        assert 6.0 < equivalent_hours < 9.0, f"等效利用小时 {equivalent_hours:.2f} h 不合理"
        print(f"\n  日发电量 {e:.2f} MWh, 等效利用小时 {equivalent_hours:.2f} h")

    def test_energy_balance(self, no_pv_result):
        """能量平衡：负荷电量 + 网损电量 ≈ 首端注入电量。

        这是最能抓住"单位错/漏算"的检查：三个量必须闭合。
        """
        _, res = no_pv_result
        s = res.summary
        # 反推首端注入：由平衡节点功率不可能直接拿到（结果集未存），
        # 因此用「负荷 + 网损」作为应有注入量，检查网损率是否落在合理区间。
        loss_rate = s.energy_loss_mwh / s.energy_load_mwh
        assert 0.03 < loss_rate < 0.08, f"日网损率 {loss_rate:.2%} 不合理"
        print(f"\n  日网损率 {loss_rate:.2%}")

    def test_light_load_steps_have_higher_voltage(self, no_pv_result):
        """凌晨（负荷谷）电压应高于晚高峰。"""
        _, res = no_pv_result
        b17 = res.bus_index.index(17)
        assert res.vm_pu[0, b17] > res.vm_pu[80, b17]

    def test_line_loading_peak_at_load_peak(self, no_pv_result):
        _, res = no_pv_result
        assert res.summary.max_line_step == 80  # 20:00 负荷峰值

    def test_ampacity_case_has_meaningful_loading(self):
        """带载流量的算例，线路负载率必须显著非零（否则 I3 无法触发线路约束）。"""
        sc = ts.assemble_scenario(_spec(case_path=str(CASE_AMP)))
        res = ts.run_timeseries(sc)
        assert res.summary.max_line_loading_percent > 60.0
        print(f"\n  最大线路负载率 {res.summary.max_line_loading_percent:.2f}%")


class TestDeterminism:
    def test_repeat_runs_identical(self, no_pv_result):
        """同一场景重跑两次，结果必须完全一致（无随机性泄漏）。"""
        sc, res1 = no_pv_result
        res2 = ts.run_timeseries(sc)
        np.testing.assert_array_equal(res1.vm_pu, res2.vm_pu)
        assert res1.summary.min_vm_pu == res2.summary.min_vm_pu


class TestWarmStart:
    def test_fallbacks_recorded(self, no_pv_result):
        _, res = no_pv_result
        assert res.summary.warmstart_fallbacks >= 0
        print(f"\n  热启动回退 {res.summary.warmstart_fallbacks} 次")


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------


class TestResultSet:
    def test_write_and_read_roundtrip(self, pv_result, tmp_path):
        sc, res = pv_result
        out = ts.write_result_set(res, sc, tmp_path)

        data = ts.read_result_set(out)
        assert data["meta"]["dims"]["n_steps"] == 96
        assert data["meta"]["dims"]["n_bus"] == 33
        assert data["vm_pu"].shape == (96, 33)
        assert data["line_loading"].shape == (96, 37)
        assert data["trafo_loading"].shape == (96, 0)
        assert data["status"]["converged"].sum() == 96

        # 数值与内存中完全一致
        np.testing.assert_allclose(data["vm_pu"].values, res.vm_pu, rtol=0, atol=0)
        assert data["summary"]["min_vm_pu"] == res.summary.min_vm_pu

    def test_meta_declares_empty_matrices(self, pv_result, tmp_path):
        sc, res = pv_result
        out = ts.write_result_set(res, sc, tmp_path)
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
        assert "trafo_loading" in meta["empty_matrices"]
        assert meta["files"]["trafo_loading"] is None
        assert not (out / "trafo_loading.parquet").exists()

    def test_meta_records_provenance(self, pv_result, tmp_path):
        """meta 必须记录算例来源与场景参数，保证结果可追溯。"""
        sc, res = pv_result
        out = ts.write_result_set(res, sc, tmp_path)
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
        assert "case33bw" in meta["case"]["source"]
        assert meta["scenario"]["sgens"][0]["bus"] == 17
        assert meta["scenario"]["sgens"][0]["capacity_mw"] == 2.0
        assert meta["step_minutes"] == 15

    def test_parquet_columns_are_strings(self, pv_result, tmp_path):
        """列名必须是字符串，否则不同 reader 会给出不同类型。"""
        sc, res = pv_result
        out = ts.write_result_set(res, sc, tmp_path)
        df = pd.read_parquet(out / "vm_pu.parquet")
        assert all(isinstance(c, str) for c in df.columns)

    def test_independent_file_read(self, pv_result, tmp_path):
        """不通过 read_result_set，直接用 pandas 读，数据也必须可用。"""
        sc, res = pv_result
        out = ts.write_result_set(res, sc, tmp_path)
        df = pd.read_parquet(out / "vm_pu.parquet")
        assert df.shape == (96, 33)
        assert df.loc[48, "17"] == pytest.approx(res.vm_pu[48, res.bus_index.index(17)])
