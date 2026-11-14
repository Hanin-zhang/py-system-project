"""把 pandapower 内置的公开算例导出成本平台算例 JSON。

算例来源全部是公开标准算例，导出过程只做"取数 + 校验量级"，不改动任何物理参数。

用法：
    uv run python -m scripts.export_cases      # 从 calc/ 目录执行
    uv run python scripts/export_cases.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandapower as pp
import pandapower.networks as pn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gp_hosting.case_loader import net_to_case_dict  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "cases"


def export_ieee33() -> dict:
    net = pn.case33bw()
    data = net_to_case_dict(net)
    data["meta"]["name"] = "IEEE 33-bus"
    data["meta"]["source"] = "pandapower.networks.case33bw()"
    data["meta"]["description"] = (
        "IEEE 33 节点 12.66 kV 辐射状配电算例（Baran & Wu, 1989）。"
        "pandapower 版本含 37 条线路：32 条在运（辐射状）+ 5 条联络线（in_service=False）。"
        "全部线路 max_i_ka=99999，即原算例不设载流量约束，线路负载率不是有效约束。"
    )
    data["meta"]["vn_kv"] = 12.66
    return data


# 派生算例使用的导线型号：pandapower 内置标准线型库（pp.available_std_types）中的 10 kV 架空线。
# 取 70 mm² 这一档：基线最大电流 0.210 kA 下负载率约 72.5%，既不过载也留出了光伏倒送触发过载的空间。
AMPACITY_STD_TYPE = "70-AL1/11-ST1A 10.0"


def export_ieee33_ampacity(base: dict) -> dict:
    """IEEE 33 + 载流量。只改 max_i_ka，阻抗等其余参数保持原算例不变。"""
    import copy

    std = pp.load_std_type(pp.create_empty_network(), AMPACITY_STD_TYPE, "line")
    max_i_ka = float(std["max_i_ka"])
    data = copy.deepcopy(base)
    for line in data["lines"]:
        line["max_i_ka"] = max_i_ka
    data["meta"]["name"] = "IEEE 33-bus (with ampacity)"
    data["meta"]["source"] = (
        f"derived from pandapower.networks.case33bw(); max_i_ka from pandapower std line type {AMPACITY_STD_TYPE!r}"
    )
    data["meta"]["description"] = (
        "IEEE 33 节点 12.66 kV 辐射状配电算例（Baran & Wu, 1989）。唯一改动：全部线路 max_i_ka 统一设为 "
        f"{max_i_ka} kA，取自 pandapower 标准线型 {AMPACITY_STD_TYPE}（10 kV 架空线）的额定载流量。"
        "原论文不给出载流量，此值是选型假设而非原论文数据；阻抗仍为原算例参数（未改成该线型的阻抗）。"
    )
    return data


def export_cigre_mv() -> dict:
    net = pn.create_cigre_network_mv(with_der=False)
    data = net_to_case_dict(net)
    data["meta"]["name"] = "CIGRE MV"
    data["meta"]["source"] = "pandapower.networks.create_cigre_network_mv(with_der=False)"
    data["meta"]["description"] = "CIGRE 中压配电网基准算例（20 kV 主干），混合负荷模型。"
    data["meta"]["vn_kv"] = float(net.bus.vn_kv.max())
    return data


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    ieee33 = export_ieee33()
    cases = {
        "ieee33.json": ieee33,
        "ieee33_ampacity.json": export_ieee33_ampacity(ieee33),
        "cigre_mv.json": export_cigre_mv(),
    }

    for filename, data in cases.items():
        path = OUT_DIR / filename
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        n_bus = len(data["buses"])
        n_line = len(data["lines"])
        n_trafo = len(data["trafos"])
        n_load = len(data["loads"])
        print(f"已导出 {path.name:14s} buses={n_bus:3d} lines={n_line:3d} trafos={n_trafo:2d} loads={n_load:3d}")


if __name__ == "__main__":
    main()
