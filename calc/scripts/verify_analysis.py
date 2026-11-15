"""I4 验收脚本：逐条核对排期 DoD。

用法：
    uv run python scripts/verify_analysis.py

任何一条 FAIL 退出码非 0。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402

from gp_hosting import analysis as an  # noqa: E402
from gp_hosting import timeseries as ts  # noqa: E402
from gp_hosting.constraints import Constraints  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
P = ROOT / "data" / "profiles"
CASE = ROOT / "data" / "cases" / "ieee33_ampacity.json"
LOAD = P / "simbench_pv_max_day_20160513_load.csv"
PV = P / "simbench_pv_max_day_20160513_pv.csv"

# I3 实测承载力（docs/hosting-capacity.md），用于交叉验证
I3_CAPACITY = {1: 13.950, 5: 12.900, 8: 7.394, 12: 4.506, 17: 3.069, 24: 8.838, 32: 5.025}

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{('  ' + detail) if detail else ''}")


def scenario(bus: int, cap: float = 0.0) -> ts.Scenario:
    return ts.assemble_scenario(
        ts.ScenarioSpec(
            case_path=str(CASE), load_profile_path=str(LOAD), pv_profile_path=str(PV),
            sgens=[ts.SgenInjection(bus=bus, capacity_mw=cap)],
        )
    )


def main() -> int:
    c = Constraints()
    print(f"算例 {CASE.name}  曲线 {LOAD.name} / {PV.name}")
    print(f"约束 {c.to_dict()}\n")

    # ---- 用 bus17 接 4 MW 造一个确定越限的场景 ----
    sc = scenario(17, 4.0)
    t0 = time.perf_counter()
    result = ts.run_timeseries(sc)
    t_sim = time.perf_counter() - t0

    print("== 4.1 归因报告 ==")
    t0 = time.perf_counter()
    rep = an.build_attribution(result, c)
    t_attr = time.perf_counter() - t0
    print(f"  仿真 {t_sim:.1f}s，归因 {t_attr:.2f}s；{rep.n_events} 条事件，涉及 {rep.n_affected_elements} 台设备")
    for e in rep.events:
        print(f"    {e.constraint:13s} {e.element_type}{e.element_index:<3d} "
              f"{e.start_time}–{e.end_time} ({e.n_steps} 段) 峰 {e.peak_value:.5f} @ {e.peak_time}")

    first = rep.first_violation or {}
    check(not rep.feasible and rep.n_events > 0, "越限场景能生成事件列表",
          f"first={rep.first_time}")
    check(first.get("duration_steps", 0) > 1, "首次越限给出持续时间",
          f"{first.get('duration_steps')} 个时段（{first.get('start_time')}–{first.get('end_time')}）")

    # 与原始矩阵交叉核对（排期：与 Parquet 原始数据一致）
    vm = result.vm_pu
    bad = vm > c.vm_max_pu
    t_first = int(np.argmax(bad.any(axis=1)))
    b_first = int(np.argmax(bad[t_first]))
    seg = bad[t_first : first["end_step"] + 1, b_first]
    check(
        first["start_step"] == t_first and first["element_index"] == b_first
        and first["duration_steps"] == len(seg)
        and first["peak_value"] == float(vm[t_first : first["end_step"] + 1, b_first].max()),
        "归因结论与原始矩阵逐项一致",
        f"first=bus{b_first}@{ts.step_to_hhmm(t_first, 96)} 跨度 {len(seg)}",
    )

    print("\n== 4.2 热力图 ==")
    hm = rep.heatmap["vm"]
    with np.errstate(invalid="ignore"):
        expect = int((vm > c.vm_max_pu).sum() + (vm < c.vm_min_pu).sum())
    check(hm["n_violating_cells"] == expect, "热力图越限单元数与原始矩阵一致",
          f"{hm['n_violating_cells']} 个")
    check(all(v < 0 for _, _, v in hm["data"]), "越限单元的值均为负偏离量")
    check(len(hm["step_times"]) == 96 and len(hm["elements"]) == 33, "ECharts 轴标签完整",
          f"{len(hm['step_times'])} 时刻 × {len(hm['elements'])} 节点")
    check(all(isinstance(x, int) and isinstance(y, int) for x, y, _ in hm["data"]),
          "data 为 [x=step, y=elementPos, value] 整数下标（ECharts categorical 轴要求）")

    print("\n== 4.3 灵敏度 ==")
    sens = an.compute_sensitivity(scenario(17), base_mw=3.0)
    bi = sens.matrices["bus_index"]
    dv = np.array(sens.matrices["dv_dp"])[sens.peak_step]
    head, end = dv[bi.index(1)], dv[bi.index(17)]
    print(f"  ∂V/∂P: bus1 {head:.6f}  bus17 {end:.6f}  比值 {end / head:.1f}")
    print(f"  ∂V_min/∂P = {sens.d_vmin_dp:.2e}   ∂V_max/∂P = {sens.d_vmax_dp:.6f}")
    check(end > 0, "末端节点 ∂V/∂P 为正（光伏抬电压）", f"{end:.6f} p.u./MW")
    check(abs(end) > abs(head), "末端灵敏度绝对值 > 首端", f"{end:.6f} vs {head:.6f}")
    check(abs(sens.d_vmin_dp) < 1e-9,
          "∂V_min/∂P 恒为 0（夜间无光照，与容量无关）—— 如实报告", f"{sens.d_vmin_dp:.2e}")

    # 2 节点解析对照
    two = _two_bus_check()
    check(two, "2 节点小算例的数量级与解析值一致（R_pu = 0.005 p.u./MW）", "")

    print("\n== 4.4 瓶颈排序 ==")
    rows = an.rank_bottlenecks(sens, constraints=c, top_n=5)
    for r in rows[:3]:
        print(f"  #{r.rank} {r.element_type}{r.element_index} {r.constraint} "
              f"headroom={r.headroom_mw:.3f} MW @ {r.binding_time}")
    check(rows and rows[0].element_type == "bus" and rows[0].element_index == 17,
          "最紧设备为 bus17 的过电压")
    gap = I3_CAPACITY[17] - 3.0
    check(abs(rows[0].headroom_mw - gap) < 0.05,
          "headroom 与 I3 承载力交叉一致", f"{rows[0].headroom_mw:.3f} vs {gap:.3f} MW")
    check(all(r.slope > 0 and r.headroom_mw is not None and r.headroom_mw < 1e4 for r in rows),
          "所有设备裕度有限且斜率 > 0（未被夜间零斜率污染）")

    print("\n== 4.5 接入点对比 ==")
    base = ts.ScenarioSpec(case_path=str(CASE), load_profile_path=str(LOAD), pv_profile_path=str(PV))
    for cap in (5.0, 9.5):
        cmp5 = an.compare_buses(base, [1, 8, 17, 32], cap, c, with_sensitivity=False)
        by = {x["bus"]: x for x in cmp5["candidates"]}
        print(f"  {cap} MW → 推荐 bus{cmp5['recommended_bus']}；"
              + "  ".join(f"bus{b}:{'可行' if by[b]['feasible'] else f'越限{by[b]['worst_severity'] * 100:.2f}%'}"
                          for b in (1, 8, 17, 32)))
        order = sorted((x for x in cmp5["candidates"]), key=lambda r: r["worst_severity"])
        cap_order = sorted((x["bus"] for x in cmp5["candidates"]), key=lambda b: -I3_CAPACITY[b])
        check([x["bus"] for x in order] == cap_order,
              f"{cap} MW 下越限轻重排序与 I3 承载力排序相反（自洽）")
    check(by[17]["feasible"] is False and by[1]["feasible"] is True,
          "5 MW 下 bus17（承载力 3.07）越限、bus1（13.95）可行")

    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    if failed:
        print("未通过：" + "; ".join(failed))
        return 1
    return 0


def _two_bus_check() -> bool:
    import json

    sys.path.insert(0, str(ROOT / "tests"))
    from conftest import make_two_bus_case

    path = ROOT / "data" / "_two_bus_tmp.json"
    path.write_text(json.dumps(make_two_bus_case()), encoding="utf-8")
    try:
        sc = ts.assemble_scenario(
            ts.ScenarioSpec(
                case_path=str(path), load_profile_path=str(P / "load_typical_day.csv"),
                pv_profile_path=str(P / "pv_typical_day.csv"), sgens=[ts.SgenInjection(bus=1)],
            )
        )
        s = an.compute_sensitivity(sc, base_mw=0.5)
        # 线性化：V2 ≈ 1 + R_pu·P（松弛节点 1.0 p.u.，忽略负荷电流变化）
        analytic = 0.005
        print(f"  2 节点：有限差分 {s.d_v_bus_at_peak_dp:.6f}  解析 {analytic:.6f}  "
              f"相对误差 {abs(s.d_v_bus_at_peak_dp - analytic) / analytic * 100:.1f}%")
        return s.d_v_bus_at_peak_dp > 0 and abs(s.d_v_bus_at_peak_dp - analytic) / analytic < 0.1
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
