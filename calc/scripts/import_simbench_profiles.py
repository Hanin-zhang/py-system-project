"""从 SimBench（pandapower 官方配套数据集）导出典型日曲线。

数据来源：
    simbench 1.5.3，算例 `1-MV-rural--0-sw`，2016 全年 15 min 分辨率（35136 点）。
    SimBench 是 pandapower 团队（Fraunhofer IEE / Uni Kassel）发布的公开基准数据集，
    曲线为标准化 p.u. 乘子：绝对值 = 曲线 × 元件 p_mw（本脚本会逐点复核这一点）。

导出两个设计日（都排除 3/27、10/30 两个夏令时切换日，它们不是 96 点）：
    - pv_max_day  : 光伏 PV3 日发电量最大的一天 —— 过电压最严重，承载力评估的默认工况
    - load_peak_day: 馈线总负荷出现全年峰值的一天 —— 低电压最严重

归一化口径（刻意写清楚，这是唯一的"人为选择"）：
    - 负荷：SimBench 该馈线全部负荷逐时段求和，再除以**全年峰值**。
      因此 factor=1.0 表示全年最大负荷时刻；把 IEEE 33 的 3.715 MW 视为"全年峰值负荷"。
    - 光伏：直接使用 PV3 原始 p.u. 值（相对装机容量），**不再归一化到 1.0**。
      真实光伏极少达到铭牌出力，人为拉到 1.0 会系统性低估承载力。
    - 时间：SimBench 时间戳为当地时间（CET/CEST），5/13 处于夏令时，正午峰值在 13:00 左右属正常。

运行：uv run python scripts/import_simbench_profiles.py
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import simbench as sb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "profiles"
GRID = "1-MV-rural--0-sw"
PV_PROFILE = "PV3"


def main() -> None:
    net = sb.get_simbench_net(GRID)
    absolute = sb.get_absolute_values(net, profiles_instead_of_study_cases=True)
    load_prof, renew = net.profiles["load"], net.profiles["renewables"]
    t = pd.to_datetime(renew["time"], format="%d.%m.%Y %H:%M")

    # ---- 复核：曲线确实是 p.u. 乘子 ----
    i = net.load.index[0]
    assert np.allclose(
        absolute[("load", "p_mw")][i].to_numpy(),
        load_prof[net.load.at[i, "profile"] + "_pload"].to_numpy() * net.load.at[i, "p_mw"],
    ), "负荷曲线不是 p.u. 乘子，口径与预期不符"
    j = net.sgen.index[net.sgen.profile == PV_PROFILE][0]
    assert np.allclose(
        absolute[("sgen", "p_mw")][j].to_numpy(),
        renew[PV_PROFILE].to_numpy() * net.sgen.at[j, "p_mw"],
    ), "光伏曲线不是 p.u. 乘子，口径与预期不符"

    total_load = absolute[("load", "p_mw")].sum(axis=1).to_numpy()
    annual_peak = float(total_load.max())
    df = pd.DataFrame({"date": t.dt.date.to_numpy(), "load": total_load, "pv": renew[PV_PROFILE].to_numpy()})
    counts = df["date"].value_counts()
    dst_days = sorted(counts[counts != 96].index)
    full = df[~df["date"].isin(dst_days)]
    g = full.groupby("date")

    days = {
        "pv_max_day": g["pv"].sum().idxmax(),
        "load_peak_day": g["load"].max().idxmax(),
    }

    manifest = {
        "source": f"simbench {sb.__version__}, grid {GRID}, year 2016, 15 min",
        "load_normalization": f"馈线总负荷 / 全年峰值 {annual_peak:.4f} MW",
        "pv_normalization": f"{PV_PROFILE} 原始 p.u.（相对装机容量），未二次归一化",
        "excluded_dst_days": [str(d) for d in dst_days],
        "days": {},
    }

    for tag, day in days.items():
        x = full[full["date"] == day]
        assert len(x) == 96
        lf = x["load"].to_numpy() / annual_peak
        pv = x["pv"].to_numpy()
        stamp = day.strftime("%Y%m%d")
        files = {
            "load": f"simbench_{tag}_{stamp}_load.csv",
            "pv": f"simbench_{tag}_{stamp}_pv.csv",
        }
        pd.DataFrame({"time_index": range(96), "factor": np.round(lf, 6)}).to_csv(OUT / files["load"], index=False)
        pd.DataFrame({"time_index": range(96), "factor": np.round(pv, 6)}).to_csv(OUT / files["pv"], index=False)
        manifest["days"][tag] = {
            "date": str(day),
            "files": files,
            "load_factor_min": round(float(lf.min()), 4),
            "load_factor_max": round(float(lf.max()), 4),
            "pv_factor_max": round(float(pv.max()), 4),
            "pv_peak_step": int(pv.argmax()),
            "pv_equivalent_hours": round(float(pv.sum() / 4), 3),
            "load_factor_at_pv_peak": round(float(lf[pv.argmax()]), 4),
        }
        print(tag, json.dumps(manifest["days"][tag], ensure_ascii=False))

    (OUT / "simbench_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("written:", OUT / "simbench_manifest.json")


if __name__ == "__main__":
    main()
