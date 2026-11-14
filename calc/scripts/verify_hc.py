"""I3 验收脚本：逐条核对排期 DoD，独立复算而非读取求解器缓存。

用法：
    uv run python scripts/verify_hc.py                 # 默认节点集
    uv run python scripts/verify_hc.py --buses 17 32   # 指定节点

输出每条 DoD 的 PASS/FAIL；任何一条 FAIL 退出码非 0。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gp_hosting import hc_solver as hc  # noqa: E402
from gp_hosting import timeseries as ts  # noqa: E402
from gp_hosting.constraints import Constraints, check_timeseries  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
P = ROOT / "data" / "profiles"
CASE = ROOT / "data" / "cases" / "ieee33_ampacity.json"
LOAD = P / "simbench_pv_max_day_20160513_load.csv"
PV = P / "simbench_pv_max_day_20160513_pv.csv"
MAIN_FEEDER = [1, 5, 8, 12, 17]

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{('  ' + detail) if detail else ''}")


def spec_for(bus: int) -> ts.ScenarioSpec:
    return ts.ScenarioSpec(
        case_path=str(CASE), load_profile_path=str(LOAD), pv_profile_path=str(PV),
        sgens=[ts.SgenInjection(bus=bus)],
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--buses", type=int, nargs="*", default=MAIN_FEEDER + [24, 30, 32])
    args = ap.parse_args()

    c = Constraints()
    print(f"约束：{c.to_dict()}")
    print(f"算例：{CASE.name}  曲线：{LOAD.name} / {PV.name}\n")

    t0 = time.perf_counter()
    solved = {r.bus: r for r in hc.solve_batch(
        ts.ScenarioSpec(case_path=str(CASE), load_profile_path=str(LOAD), pv_profile_path=str(PV)),
        args.buses, c,
    )}
    total = time.perf_counter() - t0
    for b, r in solved.items():
        print(f"  bus{b:2d}  {r.max_mw:7.3f} MW  {r.limiting_type:13s} "
              f"{r.limiting_element_type or '-':5s}{r.limiting_element_index if r.limiting_element_index is not None else '-':>4} "
              f"@ {r.limiting_time}  iters={r.iterations:2d}  {r.elapsed_ms/1000:5.1f}s")

    print("\n== DoD 核对 ==")

    # 1. ±50 kW 双向探针（独立复算，不读求解器缓存）
    ok, detail = True, []
    for b, r in solved.items():
        runner = ts.TimeseriesRunner(ts.assemble_scenario(spec_for(b)))
        at = check_timeseries(runner.run([r.max_mw]), c).feasible
        above = check_timeseries(runner.run([r.max_mw + 0.050]), c).feasible
        if not (at and not above):
            ok = False
        detail.append(f"bus{b}:{r.max_mw:.3f}{'✓' if at and not above else '✗'}")
    check(ok, "max_mw 可行且 max_mw+50kW 违规（独立复算）", " ".join(detail))

    # 2. 确定性
    r1 = hc.solve_single(ts.assemble_scenario(spec_for(17)), c)
    r2 = hc.solve_single(ts.assemble_scenario(spec_for(17)), c)
    check(r1.max_mw == r2.max_mw == solved[17].max_mw,
          "同一节点重复 3 次结果一致", f"{r1.max_mw:.6f} / {r2.max_mw:.6f} / {solved[17].max_mw:.6f}")

    # 3. 单调性
    bad = [b for b, r in solved.items() if not r.monotonicity.monotonic]
    check(not bad, "所有节点单调性判定为 true", "" if not bad else f"非单调: {bad} {[solved[b].monotonicity.reasons for b in bad]}")

    # 4. 沿主干递减
    caps = [solved[b].max_mw for b in MAIN_FEEDER if b in solved]
    check(caps == sorted(caps, reverse=True), "主干线承载力由首端向末端递减",
          " > ".join(f"{x:.2f}" for x in caps))

    # 5. 两种受限类型
    types = {r.limiting_type for r in solved.values()}
    check({"voltage_high", "line_overload"} <= types, "同时观察到电压上限与线路负载受限",
          f"types={sorted(types)}")

    # 6. 性能
    worst = max(r.elapsed_ms for r in solved.values())
    check(worst < 40_000, "单节点 < 40 s", f"最慢 {worst/1000:.1f}s")
    check(total < 180, "批量 < 3 min", f"{len(solved)} 节点共 {total:.1f}s")

    # 7. 曲线
    fewest = min(len(r.curve) for r in solved.values())
    consistent = all(
        all(p.feasible for p in r.curve if p.capacity_mw <= r.max_mw)
        and not any(p.feasible for p in r.curve if p.capacity_mw > r.max_mw)
        for r in solved.values()
    )
    check(fewest >= 10 and consistent, "扫描曲线 ≥10 点且与二分结果一致", f"最少 {fewest} 点")

    # 8. 越限时刻有光照
    pv = ts.load_profile(PV, kind="pv").factors
    daytime = all(pv[r.limiting_step] > 0 for r in solved.values())
    check(daytime, "所有受限时刻光伏出力 > 0")

    summary = {b: r.to_dict(include_curve=False) for b, r in solved.items()}
    out = ROOT / "data" / "results" / "verify_hc.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写入 {out}")

    failed = [label for ok_, label in results if not ok_]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    if failed:
        print("未通过：" + "; ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
