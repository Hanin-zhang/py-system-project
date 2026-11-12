"""结果集落盘校验：独立于计算流程读取 Parquet，检查维度与 meta.json 一致。

这是一个"独立脚本"，刻意不复用 run_timeseries 的任何内存对象——
它必须能证明"别人从磁盘上读到的数据是对的"，否则落盘就没有意义。

用法：
    uv run python -W ignore scripts/verify_parquet.py <结果目录>
    uv run python -W ignore scripts/verify_parquet.py --latest
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np
import pandas as pd  # noqa: E402

RESULT_ROOT = Path(__file__).resolve().parent.parent / "data" / "results"


def check(ok: bool, msg: str) -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {msg}")
    return ok


def verify(result_dir: Path) -> int:
    print(f"校验结果目录: {result_dir}")
    all_ok = True

    meta = json.loads((result_dir / "meta.json").read_text(encoding="utf-8"))
    summary = json.loads((result_dir / "summary.json").read_text(encoding="utf-8"))

    all_ok &= check((result_dir / "meta.json").exists(), "meta.json 存在")
    all_ok &= check((result_dir / "summary.json").exists(), "summary.json 存在")

    dims = meta["dims"]
    n_steps, n_bus, n_line, n_trafo = (
        dims["n_steps"],
        dims["n_bus"],
        dims["n_line"],
        dims["n_trafo"],
    )
    empty = set(meta.get("empty_matrices", []))

    vm = pd.read_parquet(result_dir / "vm_pu.parquet")
    ll = pd.read_parquet(result_dir / "line_loading.parquet")
    ts = pd.read_parquet(result_dir / "timeseries.parquet")
    st = pd.read_parquet(result_dir / "status.parquet")

    print("\n维度校验")
    all_ok &= check(vm.shape == (n_steps, n_bus), f"vm_pu shape {vm.shape} == {(n_steps, n_bus)}")
    all_ok &= check(ll.shape == (n_steps, n_line), f"line_loading shape {ll.shape} == {(n_steps, n_line)}")
    if "trafo_loading" in empty:
        all_ok &= check(
            (result_dir / "trafo_loading.parquet").exists() is False and n_trafo == 0,
            f"trafo_loading 声明为空矩阵（n_trafo={n_trafo}），且未落文件",
        )
    else:
        tl = pd.read_parquet(result_dir / "trafo_loading.parquet")
        all_ok &= check(tl.shape == (n_steps, n_trafo), f"trafo_loading shape {tl.shape} == {(n_steps, n_trafo)}")
    all_ok &= check(len(ts) == n_steps, f"timeseries 行数 {len(ts)} == {n_steps}")
    all_ok &= check(len(st) == n_steps, f"status 行数 {len(st)} == {n_steps}")

    print("\n列索引校验")
    all_ok &= check(
        [int(c) for c in vm.columns] == meta["bus_index"],
        "vm_pu 列名与 meta.bus_index 一致",
    )
    all_ok &= check(
        [int(c) for c in ll.columns] == meta["line_index"],
        "line_loading 列名与 meta.line_index 一致",
    )

    print("\n数值合理性校验")
    finite_vm = vm.values[np.isfinite(vm.values)]
    if len(finite_vm):
        all_ok &= check(
            0.5 < finite_vm.min() < 1.5,
            f"电压范围 {finite_vm.min():.4f} ~ {finite_vm.max():.4f} p.u. 在合理区间",
        )
        all_ok &= check(
            abs(finite_vm.min() - summary["min_vm_pu"]) < 1e-9,
            f"Parquet 最低电压 {finite_vm.min():.6f} 与 summary {summary['min_vm_pu']:.6f} 一致",
        )
        all_ok &= check(
            abs(finite_vm.max() - summary["max_vm_pu"]) < 1e-9,
            "Parquet 最高电压与 summary 一致",
        )
        # 最低电压出现位置
        flat_idx = np.nanargmin(np.where(np.isfinite(vm.values), vm.values, np.nan))
        t, c = np.unravel_index(flat_idx, vm.shape)
        all_ok &= check(
            int(vm.columns[c]) == summary["min_vm_bus"] and int(vm.index[t]) == summary["min_vm_step"],
            f"最低电压位置 bus={int(vm.columns[c])} step={int(vm.index[t])} 与 summary 一致",
        )

    if n_line:
        finite_ll = ll.values[np.isfinite(ll.values)]
        if len(finite_ll):
            all_ok &= check(
                abs(finite_ll.max() - summary["max_line_loading_percent"]) < 1e-9,
                f"Parquet 最大线路负载率 {finite_ll.max():.4f} 与 summary 一致",
            )

    print("\n收敛状态校验")
    n_conv = int(st["converged"].sum())
    all_ok &= check(
        n_conv == summary["n_converged"], f"status 收敛数 {n_conv} == summary {summary['n_converged']}"
    )

    print("\n电量校验")
    hours_per_step = 24.0 / n_steps
    loss_from_parquet = float(ts["pl_mw"].sum() * hours_per_step)
    all_ok &= check(
        abs(loss_from_parquet - summary["energy_loss_mwh"]) < 1e-6,
        f"日网损电量 {loss_from_parquet:.4f} MWh 与 summary {summary['energy_loss_mwh']:.4f} 一致",
    )
    pv_from_parquet = float(ts["pv_mw"].sum() * hours_per_step)
    all_ok &= check(
        abs(pv_from_parquet - summary["energy_pv_mwh"]) < 1e-6,
        f"日光伏电量 {pv_from_parquet:.4f} MWh 与 summary {summary['energy_pv_mwh']:.4f} 一致",
    )

    print("\n结果:", "全部通过" if all_ok else "存在失败项")
    return 0 if all_ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("result_dir", nargs="?", type=Path)
    ap.add_argument("--latest", action="store_true", help="校验最近一次结果")
    args = ap.parse_args()

    if args.latest or args.result_dir is None:
        dirs = sorted(
            [d for d in RESULT_ROOT.iterdir() if d.is_dir() and d.name != "_staged"],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        ) if RESULT_ROOT.exists() else []
        if not dirs:
            print(f"没有找到结果目录: {RESULT_ROOT}")
            return 1
        target = dirs[0]
    else:
        target = args.result_dir

    return verify(target)


if __name__ == "__main__":
    raise SystemExit(main())
