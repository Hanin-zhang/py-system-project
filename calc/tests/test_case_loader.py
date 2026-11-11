"""case_loader 测试：JSON -> pandapowerNet 的一致性与校验。"""

from __future__ import annotations

import copy

import pytest

from gp_hosting.case_loader import (
    CaseValidationError,
    build_net,
    load_case_dict,
    net_to_case_dict,
    validate_case,
)


class TestBuildNet:
    def test_rebuilt_net_matches_official_voltages(self, case_spec, official_net):
        """最强一致性证明：自建网络与官方算例逐节点电压差 < 1e-6。"""
        import pandapower as pp

        net = build_net(case_spec)
        pp.runpp(net, algorithm="nr", calculate_voltage_angles=False, trafo_model="t", numba=False)
        diff = (official_net.res_bus.vm_pu - net.res_bus.vm_pu).abs()
        assert diff.max() < 1e-6, f"最大电压差 {diff.max():.3e} @ bus {diff.idxmax()}"

    def test_element_counts(self, case_spec, net):
        assert len(net.bus) == len(case_spec.buses) == 33
        assert len(net.line) == len(case_spec.lines) == 37
        assert len(net.load) == len(case_spec.loads) == 32
        assert len(net.ext_grid) == 1

    def test_indices_preserved(self, case_spec, net):
        """索引必须原样保留，否则线路引用会错位。"""
        assert set(net.bus.index) == {b.index for b in case_spec.buses}
        assert set(net.line.index) == {ln.index for ln in case_spec.lines}
        for ln in case_spec.lines:
            assert net.line.at[ln.index, "from_bus"] == ln.from_bus
            assert net.line.at[ln.index, "to_bus"] == ln.to_bus

    def test_tie_lines_out_of_service_preserved(self, net):
        """5 条联络线必须保持停运，否则网络变成环网。"""
        tie_lines = [32, 33, 34, 35, 36]
        for idx in tie_lines:
            assert net.line.at[idx, "in_service"] is False or not net.line.at[idx, "in_service"]

    def test_units_preserved(self, case_spec, net):
        """核心量纲：vn_kv 用 kV，p_mw 用 MW，长度用 km。"""
        assert net.bus.vn_kv.min() == pytest.approx(12.66)
        assert net.load.p_mw.sum() == pytest.approx(3.715, abs=1e-6)
        assert net.load.q_mvar.sum() == pytest.approx(2.30, abs=1e-6)
        assert net.line.length_km.max() == pytest.approx(1.0)


class TestRoundTrip:
    def test_net_to_dict_to_net_is_stable(self, case_spec):
        """导出 -> 重建 -> 再导出，两次 JSON 应完全一致。"""
        d1 = net_to_case_dict(build_net(case_spec))
        d2 = net_to_case_dict(build_net(load_case_dict(d1)))
        for key in ("buses", "lines", "loads", "ext_grids"):
            assert d1[key] == d2[key], f"{key} 往返不一致"


class TestValidation:
    def test_valid_case_passes(self, raw_case):
        spec = load_case_dict(copy.deepcopy(raw_case))
        assert validate_case(spec) == []

    def test_missing_bus_reference(self, case_dict):
        case_dict["lines"][3]["to_bus"] = 999
        with pytest.raises(CaseValidationError) as exc_info:
            load_case_dict(case_dict)
        issues = exc_info.value.issues
        assert any("lines[3].to_bus" == i.path and "999" in i.message for i in issues)

    def test_self_loop_line(self, case_dict):
        case_dict["lines"][3]["to_bus"] = case_dict["lines"][3]["from_bus"]
        with pytest.raises(CaseValidationError) as exc_info:
            load_case_dict(case_dict)
        assert any("首末端节点相同" in i.message for i in exc_info.value.issues)

    def test_duplicate_index(self, case_dict):
        case_dict["lines"][2]["index"] = case_dict["lines"][0]["index"]
        with pytest.raises(CaseValidationError) as exc_info:
            load_case_dict(case_dict)
        assert any("重复" in i.message for i in exc_info.value.issues)

    def test_no_ext_grid(self, case_dict):
        case_dict["ext_grids"] = []
        with pytest.raises(CaseValidationError) as exc_info:
            load_case_dict(case_dict)
        assert any("平衡节点" in i.message for i in exc_info.value.issues)

    def test_empty_buses(self, case_dict):
        case_dict["buses"] = []
        with pytest.raises(CaseValidationError) as exc_info:
            load_case_dict(case_dict)
        assert any(i.path == "buses" for i in exc_info.value.issues)

    def test_multiple_issues_reported_together(self, case_dict):
        """一次性给出所有问题，而不是遇到第一个就退出。"""
        case_dict["lines"][0]["to_bus"] = 999
        case_dict["lines"][1]["to_bus"] = 998
        with pytest.raises(CaseValidationError) as exc_info:
            load_case_dict(case_dict)
        assert len(exc_info.value.issues) >= 2


class TestUnitErrors:
    def test_load_in_kw_is_rejected(self, case_dict):
        """把 MW 当 kW 写入，量级校验必须拦截。"""
        for ld in case_dict["loads"]:
            ld["p_mw"] *= 1000
        with pytest.raises(CaseValidationError) as exc_info:
            load_case_dict(case_dict)
        assert any("单位错误" in i.message for i in exc_info.value.issues)

    def test_sgen_in_kw_is_rejected(self, case_dict):
        case_dict["sgens"] = [{"index": 0, "bus": 17, "p_mw": 2000.0}]
        with pytest.raises(CaseValidationError) as exc_info:
            load_case_dict(case_dict)
        assert any("单位错误" in i.message for i in exc_info.value.issues)

    def test_negative_voltage_rejected(self, case_dict):
        case_dict["buses"][0]["vn_kv"] = -1.0
        with pytest.raises(CaseValidationError) as exc_info:
            load_case_dict(case_dict)
        assert any("vn_kv" in i.path and "大于" in i.message for i in exc_info.value.issues)

    def test_negative_ampacity_rejected(self, case_dict):
        case_dict["lines"][0]["max_i_ka"] = -1.0
        with pytest.raises(CaseValidationError) as exc_info:
            load_case_dict(case_dict)
        assert any("max_i_ka" in i.path for i in exc_info.value.issues)

    def test_zero_line_length_rejected(self, case_dict):
        case_dict["lines"][0]["length_km"] = 0.0
        with pytest.raises(CaseValidationError) as exc_info:
            load_case_dict(case_dict)
        assert any("length_km" in i.path for i in exc_info.value.issues)

    def test_unknown_field_rejected(self, case_dict):
        """schema 是 extra=forbid，防止拼写错误被静默忽略。"""
        case_dict["lines"][0]["r_ohm_per_km_typo"] = 1.0
        with pytest.raises(CaseValidationError) as exc_info:
            load_case_dict(case_dict)
        assert any("不允许的字段" in i.message for i in exc_info.value.issues)
