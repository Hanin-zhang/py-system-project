"""W0 冒烟脚本：加载算例 → 跑潮流 → 打印关键指标，并做一次自建网络对拍。

这是"环境 + 单位 + 结果手感"三合一的最小验证，也是 I1 的验收基础。

用法：
    uv run python -W ignore scripts/smoke_powerflow.py
    uv run python -W ignore scripts/smoke_powerflow.py --case data/cases/cigre_mv.json
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandapower as pp
import pandapower.networks as pn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gp_hosting.case_loader import build_net, load_case_file  # noqa: E402
from gp_hosting.constraints import Constraints, check  # noqa: E402
from gp_hosting.powerflow import PowerFlowOptions, run  # noqa: E402

DEFAULT_CASE = Path(__file__).resolve().parent.parent / "data" / "cases" / "ieee33.json"


def _fmt(v: float, nd: int = 4) -> str:
    return f"{v:.{nd}f}"


def report(spec, net) -> int:
    result = run(net, PowerFlowOptions())
    if not result.converged:
        print(f"[FAIL] 潮流不收敛: {result.error}")
        return 1

    s = result.summary
    assert s is not None
    print(f"算例: {spec.meta.name}  ({spec.meta.source})")
    print(f"  节点 {len(spec.buses)} / 线路 {len(spec.lines)} / 变压器 {len(spec.trafos)} / 负荷 {len(spec.loads)}")
    print(f"  总负荷      {_fmt(s.total_load_mw)} MW")
    print(f"  总光伏      {_fmt(s.total_sgen_mw)} MW")
    print(f"  总有功网损  {_fmt(s.total_p_loss_mw)} MW")
    print(f"  最低电压    {_fmt(s.min_vm_pu)} p.u.  @ bus {s.min_vm_bus} ({s.min_vm_bus_name})")
    print(
        f"  最高负载率  {_fmt(s.max_loading_percent, 2)} %  "
        f"@ {s.max_loading_element_type} {s.max_loading_element_index}"
    )
    print(f"  单次潮流耗时 {_fmt(result.elapsed_ms, 1)} ms")

    vr = check(result, Constraints())
    if vr.feasible:
        print("  约束检查    通过（无越限）")
    else:
        print(f"  约束检查    越限 {len(vr.violations)} 处：")
        for v in vr.violations[:5]:
            print(
                f"    - {v.constraint:14s} {v.element_type} {v.element_index}: "
                f"{v.value:.4f} (限值 {v.limit:.4f})"
            )
    return 0


def _fix_nan_numbers(obj):
    """pandapower 的部分列会出现 NaN，对应 schema 里的空值；这里只保留必要列，不再透传。"""
    return obj


def roundtrip_check(spec) -> int:
    """用自建网络与 pandapower 官方算例对拍（仅对 ieee33 生效）。

    验收标准：逐节点 vm_pu 差 < 1e-6。
    """
    if "ieee33" not in str(spec.meta.name).lower() and "33" not in spec.meta.name:
        print("  [跳过对拍] 该算例无官方函数可比对")
        return 0

    official = pn.case33bw()
    pp.runpp(official, algorithm="nr", calculate_voltage_angles=False, trafo_model="t", numba=False)

    mine = build_net(spec)
    pp.runpp(mine, algorithm="nr", calculate_voltage_angles=False, trafo_model="t", numba=False)

    diff = (official.res_bus.vm_pu - mine.res_bus.vm_pu).abs()
    max_diff = float(diff.max())
    worst_bus = int(diff.idxmax())
    print("\n对拍（自建 JSON 网络 vs pandapower.networks.case33bw()）")
    print(f"  节点数一致: {len(official.bus)} == {len(mine.bus)} -> {len(official.bus) == len(mine.bus)}")
    print(f"  线路数一致: {len(official.line)} == {len(mine.line)} -> {len(official.line) == len(mine.line)}")
    print(f"  最大节点电压差 {max_diff:.3e} p.u. @ bus {worst_bus}")
    if max_diff < 1e-6:
        print("  [PASS] 电压逐节点差 < 1e-6")
        return 0
    print("  [FAIL] 电压差超标，检查单位或参数导出")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=Path, default=DEFAULT_CASE)
    args = parser.parse_args()

    t0 = time.perf_counter()
    spec = load_case_file(str(args.case))
    net = build_net(spec)
    print(f"算例加载耗时 {(time.perf_counter() - t0) * 1000:.1f} ms")

    rc = report(spec, net)
    rc |= roundtrip_check(spec)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
