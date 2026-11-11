"""powerflow 模块测试。

覆盖 I1 的 6 类验收场景：
    正常 / 末端低压 / 重载 / 不收敛 / 非法引用（由 case_loader 负责）/ 单位错误
"""

from __future__ import annotations

import pandapower as pp
import pytest

from gp_hosting.case_loader import CaseValidationError, load_case_dict
from gp_hosting.powerflow import PowerFlowOptions, run


class TestNormalCase:
    def test_converges_and_returns_structured_result(self, net):
        result = run(net, PowerFlowOptions())
        assert result.converged is True
        assert result.error is None
        assert len(result.bus) == 33
        assert len(result.line) == 37
        assert result.summary is not None

    def test_min_voltage_matches_official(self, net):
        """最低电压必须落在 17 号节点，数值约 0.9131（与 W0 一致）。"""
        result = run(net, PowerFlowOptions())
        s = result.summary
        assert s is not None
        assert s.min_vm_bus == 17
        assert s.min_vm_pu == pytest.approx(0.9131, abs=1e-3)

    def test_total_load_and_loss(self, net):
        result = run(net, PowerFlowOptions())
        s = result.summary
        assert s is not None
        assert s.total_load_mw == pytest.approx(3.715, abs=1e-6)
        assert s.total_p_loss_mw == pytest.approx(0.2027, abs=1e-3)
        # 网损率应在 3%–8% 的合理区间（配网典型值）
        assert 0.03 < s.total_p_loss_mw / s.total_load_mw < 0.08

    def test_bus_result_fields_are_physical(self, net):
        result = run(net, PowerFlowOptions())
        for b in result.bus:
            assert 0.5 < b.vm_pu < 1.5, f"bus {b.index} 电压 {b.vm_pu} 量级异常"
            assert b.vn_kv == pytest.approx(12.66)

    def test_elapsed_ms_recorded(self, net):
        result = run(net, PowerFlowOptions())
        assert result.elapsed_ms > 0

    def test_single_run_fast_enough(self, net):
        """33 节点单次潮流应 < 100 ms（I1 验收项）。"""
        run(net, PowerFlowOptions())  # 预热
        result = run(net, PowerFlowOptions())
        assert result.elapsed_ms < 100


class TestUnconverged:
    def test_absurd_impedance_does_not_converge(self, net):
        """把线路阻抗放大 100 倍，结果必须是不收敛或电压显著异常，而不是静默给出正常值。"""
        net.line["r_ohm_per_km"] *= 100
        net.line["x_ohm_per_km"] *= 100
        result = run(net, PowerFlowOptions(init="flat"))
        if result.converged:
            # 若仍收敛，电压必须显著异常（远低于正常 0.91）
            assert result.summary is not None
            assert result.summary.min_vm_pu < 0.5, "阻抗放大 100 倍后电压居然还正常，说明计算有问题"
        else:
            assert result.error is not None
            assert result.error.code in {"LOADFLOW_NOT_CONVERGED", "LOADFLOW_ERROR"}

    def test_error_is_structured_not_exception(self, net):
        """不收敛必须返回结构体，不能向上抛异常。"""
        net.line["r_ohm_per_km"] *= 1e4
        result = run(net, PowerFlowOptions(max_iteration=3, init="flat"))
        assert isinstance(result.converged, bool)
        if not result.converged:
            assert result.error is not None
            assert result.error.code
            assert isinstance(result.error.detail, dict)


class TestHeavyLoad:
    def test_heavy_load_pushes_voltage_down(self, case_spec):
        from gp_hosting.case_loader import build_net

        net = build_net(case_spec)
        base = run(net, PowerFlowOptions())
        assert base.summary is not None

        net2 = build_net(case_spec)
        net2.load["p_mw"] *= 2.0
        net2.load["q_mvar"] *= 2.0
        heavy = run(net2, PowerFlowOptions())
        assert heavy.converged
        assert heavy.summary is not None
        assert heavy.summary.min_vm_pu < base.summary.min_vm_pu
        assert heavy.summary.min_vm_pu < 0.90, "负荷翻倍后最低电压应明显跌出 0.90"

    def test_light_load_raises_voltage(self, case_spec):
        from gp_hosting.case_loader import build_net

        net = build_net(case_spec)
        base = run(net, PowerFlowOptions())

        net2 = build_net(case_spec)
        net2.load["p_mw"] *= 0.1
        net2.load["q_mvar"] *= 0.1
        light = run(net2, PowerFlowOptions())
        assert light.summary is not None and base.summary is not None
        assert light.summary.min_vm_pu > base.summary.min_vm_pu


class TestPVDistributedGeneration:
    def test_sgen_raises_voltage_at_end_of_feeder(self, case_spec):
        """末端节点加光伏，电压抬升 —— I1 的核心物理现象。"""
        from gp_hosting.case_loader import build_net

        net = build_net(case_spec)
        base = run(net, PowerFlowOptions())
        base_vm = {b.index: b.vm_pu for b in base.bus}

        net2 = build_net(case_spec)
        pp.create_sgen(net2, bus=17, p_mw=1.0, q_mvar=0.0, name="PV17")
        after = run(net2, PowerFlowOptions())
        after_vm = {b.index: b.vm_pu for b in after.bus}

        rise = after_vm[17] - base_vm[17]
        assert rise > 0.01, f"1 MW 光伏只抬升了 {rise:.4f} p.u."

        # 越靠末端，抬升越明显
        assert (after_vm[17] - base_vm[17]) > (after_vm[1] - base_vm[1])

    def test_sgen_output_reported(self, case_spec):
        from gp_hosting.case_loader import build_net

        net = build_net(case_spec)
        pp.create_sgen(net, bus=17, p_mw=0.5, q_mvar=0.0)
        result = run(net, PowerFlowOptions())
        assert result.summary is not None
        assert result.summary.total_sgen_mw == pytest.approx(0.5)
        assert len(result.sgen) == 1


class TestOptions:
    def test_algorithm_selection(self, net):
        """nr 与 bfsw 在辐射网上的结果应高度一致（bfsw 专为配网设计）。"""
        nr = run(net, PowerFlowOptions(algorithm="nr"))
        bfsw = run(net, PowerFlowOptions(algorithm="bfsw"))
        assert nr.converged and bfsw.converged
        v_nr = {b.index: b.vm_pu for b in nr.bus}
        v_bf = {b.index: b.vm_pu for b in bfsw.bus}
        max_diff = max(abs(v_nr[i] - v_bf[i]) for i in v_nr)
        assert max_diff < 1e-6, f"两种算法电压差 {max_diff:.3e}"

    def test_max_iteration_respected(self, net):
        """迭代上限设成 1 时，重载场景应不收敛（证明参数确实生效）。"""
        net.load["p_mw"] *= 3
        result = run(net, PowerFlowOptions(max_iteration=1, init="flat"))
        assert not result.converged


class TestInvalidReferenceFromLoader:
    """非法引用在 case_loader 层就被拦住，根本不会到达潮流计算。"""

    def test_bad_reference_never_reaches_powerflow(self, case_dict):
        case_dict["lines"][3]["to_bus"] = 999
        with pytest.raises(CaseValidationError):
            load_case_dict(case_dict)
