"""constraints 模块测试：越限判定必须精确到"哪个设备、哪条约束、超了多少"。"""

from __future__ import annotations

import pytest

from gp_hosting.constraints import Constraints, check
from gp_hosting.powerflow import (
    BusResult,
    LineResult,
    PowerFlowResult,
    PowerFlowSummary,
    TrafoResult,
)


def _result(buses=(), lines=(), trafos=(), converged=True) -> PowerFlowResult:
    return PowerFlowResult(
        converged=converged,
        bus=list(buses),
        line=list(lines),
        trafo=list(trafos),
        summary=PowerFlowSummary(
            total_load_mw=1.0,
            total_sgen_mw=0.0,
            total_p_loss_mw=0.05,
            min_vm_pu=min((b.vm_pu for b in buses), default=1.0),
            min_vm_bus=0,
            min_vm_bus_name="",
            max_loading_percent=max((ln.loading_percent for ln in lines), default=0.0),
            max_loading_element_type="",
            max_loading_element_index=None,
        ),
    )


def _bus(index: int, vm: float) -> BusResult:
    return BusResult(index=index, name=f"b{index}", vn_kv=12.66, vm_pu=vm, va_degree=0.0, p_mw=0.0, q_mvar=0.0)


def _line(index: int, loading: float) -> LineResult:
    return LineResult(
        index=index,
        name=f"l{index}",
        from_bus=0,
        to_bus=1,
        loading_percent=loading,
        p_from_mw=0.0,
        q_from_mvar=0.0,
        pl_mw=0.0,
        i_ka=0.0,
        max_i_ka=0.3,
    )


def _trafo(index: int, loading: float) -> TrafoResult:
    return TrafoResult(
        index=index,
        name=f"t{index}",
        hv_bus=0,
        lv_bus=1,
        loading_percent=loading,
        p_hv_mw=0.0,
        q_hv_mvar=0.0,
        pl_mw=0.0,
    )


class TestVoltageViolations:
    def test_all_within_limits(self):
        r = _result(buses=[_bus(0, 1.0), _bus(1, 0.95), _bus(2, 1.05)])
        report = check(r, Constraints())
        assert report.feasible is True
        assert report.violations == []

    def test_low_voltage_detected(self):
        r = _result(buses=[_bus(0, 1.0), _bus(7, 0.91)])
        report = check(r, Constraints())
        assert report.feasible is False
        assert len(report.violations) == 1
        v = report.violations[0]
        assert v.constraint == "voltage_low"
        assert v.element_index == 7
        assert v.value == pytest.approx(0.91)
        assert v.limit == pytest.approx(0.93)
        assert v.severity == pytest.approx((0.93 - 0.91) / 0.93)

    def test_high_voltage_detected(self):
        """光伏倒送导致过电压，是承载力最常见的受限类型。"""
        r = _result(buses=[_bus(0, 1.0), _bus(17, 1.08)])
        report = check(r, Constraints())
        assert report.feasible is False
        v = report.violations[0]
        assert v.constraint == "voltage_high"
        assert v.element_index == 17
        assert v.value == pytest.approx(1.08)

    def test_boundary_exactly_at_limit_is_feasible(self):
        """恰好等于限值不算越限（约束是闭区间）。"""
        r = _result(buses=[_bus(0, 0.93), _bus(1, 1.07)])
        assert check(r, Constraints()).feasible is True

    def test_multiple_violations_all_reported(self):
        r = _result(buses=[_bus(0, 0.70), _bus(1, 1.20), _bus(2, 0.95)])
        report = check(r, Constraints())
        assert len(report.violations) == 2
        assert {v.constraint for v in report.violations} == {"voltage_low", "voltage_high"}


class TestLoadingViolations:
    def test_line_overload(self):
        r = _result(buses=[_bus(0, 1.0)], lines=[_line(0, 80.0), _line(1, 120.0)])
        report = check(r, Constraints())
        assert len(report.violations) == 1
        v = report.violations[0]
        assert v.constraint == "line_overload"
        assert v.element_index == 1
        assert v.severity == pytest.approx(0.2)

    def test_trafo_overload(self):
        r = _result(buses=[_bus(0, 1.0)], trafos=[_trafo(0, 150.0)])
        report = check(r, Constraints())
        assert report.violations[0].constraint == "trafo_overload"

    def test_custom_loading_limit(self):
        """变压器按 80% 保守配置时，85% 的负载率应被判越限。"""
        r = _result(buses=[_bus(0, 1.0)], trafos=[_trafo(0, 85.0)])
        assert check(r, Constraints()).feasible is True
        assert check(r, Constraints(trafo_loading_percent_max=80.0)).feasible is False

    def test_mixed_violation_types(self):
        r = _result(
            buses=[_bus(0, 0.90)],
            lines=[_line(0, 110.0)],
            trafos=[_trafo(0, 130.0)],
        )
        report = check(r, Constraints())
        kinds = {v.constraint for v in report.violations}
        assert kinds == {"voltage_low", "line_overload", "trafo_overload"}


class TestUnconverged:
    def test_unconverged_is_infeasible(self):
        """不收敛本身就是一种不可行，必须让承载力搜索正确地停下。"""
        r = _result(converged=False)
        report = check(r, Constraints())
        assert report.feasible is False


class TestCustomConstraints:
    def test_tighter_voltage_band(self):
        r = _result(buses=[_bus(0, 0.96), _bus(1, 1.04)])
        assert check(r, Constraints()).feasible is True
        assert check(r, Constraints(vm_min_pu=0.97, vm_max_pu=1.03)).feasible is False

    def test_to_dict_roundtrip(self):
        c = Constraints(vm_min_pu=0.95, vm_max_pu=1.05, line_loading_percent_max=90.0)
        d = c.to_dict()
        assert d["vm_min_pu"] == 0.95
        assert Constraints(**d) == c
