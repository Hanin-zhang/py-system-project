"""W0 试算：在馈线末端加光伏，观察电压抬升。

这张图/这组数字的目的不是"证明算法"，而是建立对结果的**物理直觉**：
- 末端节点电压为什么会上升
- 上升幅度和容量大致是什么关系（是否近似线性）
- 不同接入位置（末端 vs 首端）差别有多大

用法：
    uv run python -W ignore scripts/pv_trial.py
    uv run python -W ignore scripts/pv_trial.py --bus 17 --mw 1.0
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandapower as pp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gp_hosting.case_loader import build_net, load_case_file  # noqa: E402
from gp_hosting.powerflow import PowerFlowOptions, run  # noqa: E402

CASE = Path(__file__).resolve().parent.parent / "data" / "cases" / "ieee33.json"
OUT_DIR = Path(__file__).resolve().parent.parent / ".." / "docs" / "figures"


def baseline_voltages(net):
    result = run(net, PowerFlowOptions())
    return {b.index: b.vm_pu for b in result.bus}


def with_sgen(net, bus: int, p_mw: float):
    net2 = net.deepcopy()
    pp.create_sgen(net2, bus=bus, p_mw=p_mw, q_mvar=0.0, name=f"PV@{bus}")
    result = run(net2, PowerFlowOptions())
    return {b.index: b.vm_pu for b in result.bus}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bus", type=int, default=17, help="光伏接入节点")
    parser.add_argument("--mw", type=float, default=1.0, help="光伏容量 MW")
    parser.add_argument("--end-bus", type=int, default=None, help="末端节点（默认自动取最低电压节点）")
    args = parser.parse_args()

    spec = load_case_file(str(CASE))
    net = build_net(spec)

    base = baseline_voltages(net)
    worst_bus = min(base, key=base.get)
    end_bus = args.end_bus if args.end_bus is not None else worst_bus

    print(f"基线最低电压节点: bus {worst_bus}, vm = {base[worst_bus]:.4f} p.u.")
    print(f"试算接入节点: bus {args.bus}, 容量 {args.mw:.2f} MW\n")

    after = with_sgen(net, args.bus, args.mw)
    rise = {b: after[b] - base[b] for b in base}
    max_rise_bus = max(rise, key=rise.get)

    print(f"接入点 bus {args.bus} 电压: {base[args.bus]:.4f} -> {after[args.bus]:.4f} "
          f"(+{rise[args.bus]:.4f} p.u.)")
    print(f"全网最低电压: {base[worst_bus]:.4f} @ bus {worst_bus} "
          f"-> {after[min(after, key=after.get)]:.4f} @ bus {min(after, key=after.get)}")
    print(f"电压上升最大的节点: bus {max_rise_bus} (+{rise[max_rise_bus]:.4f} p.u.)")

    if rise[max_rise_bus] > 0.01:
        print(f"[PASS] 至少一个节点电压上升超过 0.01 p.u. ({rise[max_rise_bus]:.4f})")
    else:
        print(f"[FAIL] 电压上升不足 0.01 p.u. ({rise[max_rise_bus]:.4f})")
        return 1

    # ---- 容量扫描：末端 vs 首端 ----
    scans = {}
    for label, bus in (("末端 bus17", end_bus), ("首端 bus1", 1)):
        caps = [0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0]
        vm = []
        for cap in caps:
            v = with_sgen(net, bus, cap)
            vm.append(v[bus])
        scans[label] = (caps, vm, bus)

    print("\n容量扫描（接入点自身电压 p.u.）")
    header = "容量 MW ".ljust(10) + "  ".join(f"{k:>14s}" for k in scans)
    print(header)
    caps = list(scans.values())[0][0]
    for i, cap in enumerate(caps):
        row = f"{cap:<10.2f}" + "  ".join(f"{scans[k][1][i]:>14.4f}" for k in scans)
        print(row)

    # ---- 出图：电压沿馈线分布 ----
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(11, 5))
    main_path = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]
    ax.plot(range(len(main_path)), [base[b] for b in main_path], "o-", label="基线", color="#2c7fb8")
    ax.plot(
        range(len(main_path)),
        [after[b] for b in main_path],
        "s--",
        label=f"接入 {args.mw:.1f} MW @ bus {args.bus}",
        color="#d95f02",
    )
    ax.axhline(0.93, color="red", lw=1, ls=":", label="下限 0.93 p.u.")
    ax.axhline(1.07, color="red", lw=1, ls=":", label="上限 1.07 p.u.")
    ax.set_xticks(range(len(main_path)))
    ax.set_xticklabels(main_path)
    ax.set_xlabel("Bus（主干 0→17）")
    ax.set_ylabel("vm_pu")
    ax.set_title("IEEE 33-bus 主干电压沿馈线分布")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    out = OUT_DIR / "ieee33_voltage_profile.png"
    fig.savefig(out, dpi=150)
    print(f"\n图已保存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
