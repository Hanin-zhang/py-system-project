"""生成 96 点（15 min）典型日曲线：负荷 + 光伏。

输出到 calc/data/profiles/：
- load_typical_day.csv : time_index, factor  （双峰，峰谷比 0.55–1.0）
- pv_typical_day.csv   : time_index, factor  （正午单峰，06:00–18:00，含云遮挡扰动）

曲线是**模拟数据**，不是实测数据。生成参数全部显式写在本文件开头，
函数是确定性的（固定随机种子），重跑结果完全一致——这一点对实验可复现是硬要求。

用法：
    uv run python -m scripts.gen_profiles
    # 或
    uv run python scripts/gen_profiles.py
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

# --------------------------------------------------------------------------
# 生成参数（想改曲线形状就改这里，不要改下面的算法）
# --------------------------------------------------------------------------

POINTS_PER_DAY = 96
STEP_MINUTES = 15

# 负荷：双峰
LOAD_PEAK_MORNING_HOUR = 10.0   # 早峰中心
LOAD_PEAK_EVENING_HOUR = 20.0   # 晚峰中心（略高于早峰）
LOAD_MORNING_AMP = 0.35         # 早峰相对高度
LOAD_EVENING_AMP = 0.45         # 晚峰相对高度
LOAD_BASE = 0.55                # 夜间谷底
LOAD_SIGMA_MORNING = 2.2        # 早峰宽度（小时）
LOAD_SIGMA_EVENING = 2.6        # 晚峰宽度（小时）

# 光伏：单峰 + 云遮挡
PV_SUNRISE_HOUR = 6.0
PV_SUNSET_HOUR = 18.0
PV_PEAK_HOUR = 12.0
PV_CLOUD_SEED = 20261111
PV_CLOUD_DIP_PROB = 0.06        # 每个时段出现云遮挡的概率
PV_CLOUD_DIP_DEPTH = (0.35, 0.7)  # 遮挡时出力降到峰值的 35%–70%


def _gauss(x: float, mu: float, sigma: float) -> float:
    return math.exp(-((x - mu) ** 2) / (2 * sigma**2))


def load_curve() -> list[float]:
    """典型日负荷系数，归一化到 max = 1.0，谷底不低于 LOAD_BASE。"""
    raw = []
    for i in range(POINTS_PER_DAY):
        h = i * STEP_MINUTES / 60.0
        v = (
            LOAD_BASE
            + LOAD_MORNING_AMP * _gauss(h, LOAD_PEAK_MORNING_HOUR, LOAD_SIGMA_MORNING)
            + LOAD_EVENING_AMP * _gauss(h, LOAD_PEAK_EVENING_HOUR, LOAD_SIGMA_EVENING)
        )
        raw.append(v)
    peak = max(raw)
    return [v / peak for v in raw]


def pv_curve() -> list[float]:
    """光伏出力系数：日出日落之间为正弦半波，含确定性云遮挡。"""
    import random

    rng = random.Random(PV_CLOUD_SEED)
    raw = []
    for i in range(POINTS_PER_DAY):
        h = i * STEP_MINUTES / 60.0
        if h <= PV_SUNRISE_HOUR or h >= PV_SUNSET_HOUR:
            raw.append(0.0)
            continue
        # 正弦半波：在日出/日落为 0，正午为 1
        frac = (h - PV_SUNRISE_HOUR) / (PV_SUNSET_HOUR - PV_SUNRISE_HOUR)
        v = math.sin(math.pi * frac)
        # 云遮挡：仅作用于正午附近，日出日落附近本来就低，不再叠加
        if 0.2 < frac < 0.8 and rng.random() < PV_CLOUD_DIP_PROB:
            v *= rng.uniform(*PV_CLOUD_DIP_DEPTH)
        raw.append(v)
    peak = max(raw)
    return [v / peak for v in raw]


def write_csv(path: Path, factors: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time_index", "factor"])
        for i, v in enumerate(factors):
            w.writerow([i, f"{v:.6f}"])


def read_csv(path: Path) -> list[float]:
    with path.open(newline="", encoding="utf-8") as f:
        return [float(row["factor"]) for row in csv.DictReader(f)]


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "data" / "profiles"
    load = load_curve()
    pv = pv_curve()

    write_csv(out_dir / "load_typical_day.csv", load)
    write_csv(out_dir / "pv_typical_day.csv", pv)

    print(f"已生成 {out_dir}")
    print(f"  负荷: {POINTS_PER_DAY} 点, 峰 {max(load):.3f} @ {load.index(max(load)) * STEP_MINUTES // 60:02d}:"
          f"{load.index(max(load)) * STEP_MINUTES % 60:02d}, 谷 {min(load):.3f}, 峰谷比 {min(load)/max(load):.2f}")
    print(f"  光伏: {POINTS_PER_DAY} 点, 峰 {max(pv):.3f} @ {pv.index(max(pv)) * STEP_MINUTES // 60:02d}:"
          f"{pv.index(max(pv)) * STEP_MINUTES % 60:02d}, 非零时段 {sum(1 for v in pv if v > 0)} 个")


if __name__ == "__main__":
    main()
