"""单位与量级校验。

目的不是"跑通"，而是**证明校验确实能抓到单位错误**。
脚本会故意构造几组错误数据，要求它们全部被拦下；任何一组没有报错即视为 FAIL。

用法：
    uv run python -W ignore scripts/unit_check.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gp_hosting.case_loader import (  # noqa: E402
    CaseValidationError,
    load_case_dict,
    validate_case,
    CaseSpec,
)

CASE = Path(__file__).resolve().parent.parent / "data" / "cases" / "ieee33.json"


def expect_invalid(name: str, data: dict, must_mention: str | None = None) -> bool:
    """期望 data 被判为非法。返回 True 表示符合预期。"""
    try:
        load_case_dict(data)
    except CaseValidationError as exc:
        text = str(exc)
        if must_mention and must_mention not in text:
            print(f"  [FAIL] {name}: 报错了，但错误信息未提到 {must_mention!r}")
            print(f"         实际: {text}")
            return False
        print(f"  [PASS] {name}: 已拦截 -> {text.splitlines()[0][:110]}")
        return True
    print(f"  [FAIL] {name}: 未拦截！")
    return False


def expect_valid(name: str, data: dict) -> bool:
    try:
        load_case_dict(data)
    except CaseValidationError as exc:
        print(f"  [FAIL] {name}: 合法数据被误判 -> {exc}")
        return False
    print(f"  [PASS] {name}: 通过")
    return True


def main() -> int:
    raw = json.loads(CASE.read_text(encoding="utf-8"))
    ok = True

    print("一、正常数据应通过")
    ok &= expect_valid("原始 IEEE 33 算例", raw)

    print("\n二、单位错误应被拦截")
    # 1) 负荷 kW 当作 MW
    bad = copy.deepcopy(raw)
    for ld in bad["loads"]:
        ld["p_mw"] *= 1000
        ld["q_mvar"] *= 1000
    ok &= expect_invalid("负荷写成 kW（×1000）", bad, "单位")

    # 2) 光伏 MW 当作 kW
    bad = copy.deepcopy(raw)
    bad["sgens"] = [{"index": 0, "bus": 17, "p_mw": 2000.0}]
    ok &= expect_invalid("光伏容量写成 kW（2000）", bad, "单位")

    # 3) 线路长度单位 km 写成 m
    bad = copy.deepcopy(raw)
    for ln in bad["lines"]:
        ln["length_km"] *= 1000
    ok &= expect_valid("线路长度 ×1000（不拦截，但会造成电压异常）", bad)

    print("\n三、引用完整性应被拦截")
    bad = copy.deepcopy(raw)
    bad["lines"][3]["to_bus"] = 999
    ok &= expect_invalid("线路末端节点不存在", bad, "节点 999 不存在")

    bad = copy.deepcopy(raw)
    bad["lines"][3]["to_bus"] = bad["lines"][3]["from_bus"]
    ok &= expect_invalid("线路首末端相同", bad, "首末端节点相同")

    bad = copy.deepcopy(raw)
    bad["loads"][0]["bus"] = 888
    ok &= expect_invalid("负荷挂在不存在的节点", bad, "节点 888 不存在")

    bad = copy.deepcopy(raw)
    bad["ext_grids"] = []
    ok &= expect_invalid("缺少外部电网", bad, "平衡节点")

    bad = copy.deepcopy(raw)
    bad["lines"][0]["index"] = bad["lines"][1]["index"]
    ok &= expect_invalid("线路索引重复", bad, "重复")

    print("\n四、数值范围应被拦截")
    bad = copy.deepcopy(raw)
    bad["buses"][0]["vn_kv"] = -1.0
    ok &= expect_invalid("节点电压为负", bad, "必须大于 0")

    bad = copy.deepcopy(raw)
    bad["lines"][0]["max_i_ka"] = -1
    ok &= expect_invalid("载流量为负", bad, "必须大于 0")

    print("\n五、量级合理性（电压/负荷基准值）")
    spec = CaseSpec.model_validate(raw)
    total_load = sum(ld.p_mw for ld in spec.loads)
    assert 3.0 < total_load < 4.5, f"IEEE 33 总负荷应在 ~3.7 MW，实际 {total_load}"
    print(f"  [PASS] 总负荷 {total_load:.3f} MW 在预期区间")
    assert abs(spec.buses[0].vn_kv - 12.66) < 1e-9
    print(f"  [PASS] 主干电压 {spec.buses[0].vn_kv} kV 正确")

    # 若把负荷当 kW 使用（除以 1000），量级会明显偏小 -> 说明"只要量级对，单位就对"
    tiny = copy.deepcopy(raw)
    for ld in tiny["loads"]:
        ld["p_mw"] /= 1000
    tiny_spec = CaseSpec.model_validate(tiny)
    tiny_total = sum(ld.p_mw for ld in tiny_spec.loads)
    assert tiny_total < 0.01
    print(f"  [PASS] 若误把 MW 当 kW，总负荷变成 {tiny_total:.4f} MW —— 量级直接暴露")

    print("\n结果:", "全部通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
