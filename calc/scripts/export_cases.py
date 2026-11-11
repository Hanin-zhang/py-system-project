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
        "注意：pandapower 版本保留了 5 条常开联络线且全部闭合、max_i_ka 设为 99999，"
        "因此原始网络含 5 个环、线路负载率不是有效约束。"
    )
    data["meta"]["vn_kv"] = 12.66
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

    cases = {
        "ieee33.json": export_ieee33(),
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
