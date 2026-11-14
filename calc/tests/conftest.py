"""共享 fixture。

所有测试用例的算例都来自 data/cases/ieee33.json（导出自 pandapower 官方算例），
不在测试里硬编码网络参数——参数改了，测试跟着变。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pandapower as pp
import pandapower.networks as pn
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from gp_hosting.case_loader import CaseSpec, build_net, load_case_dict  # noqa: E402

CASE_FILE = ROOT / "data" / "cases" / "ieee33.json"


@pytest.fixture()
def raw_case() -> dict:
    """每个测试读取一份全新的算例数据。

    刻意不做 session 级缓存：测试里会故意篡改算例（越界、单位错误等），
    共享同一份 dict 会互相污染，且这种污染会伪装成"被测代码有 bug"。
    """
    return json.loads(CASE_FILE.read_text(encoding="utf-8"))


@pytest.fixture()
def case_dict(raw_case) -> dict:
    """每个测试拿到独立副本，避免相互污染。"""
    return copy.deepcopy(raw_case)


@pytest.fixture()
def case_spec(raw_case) -> CaseSpec:
    return load_case_dict(copy.deepcopy(raw_case))


@pytest.fixture()
def net(case_spec):
    return build_net(case_spec)


@pytest.fixture()
def official_net():
    """pandapower 官方 case33bw，作为对拍金标准。"""
    n = pn.case33bw()
    pp.runpp(n, algorithm="nr", calculate_voltage_angles=False, trafo_model="t", numba=False)
    return n


def make_two_bus_case() -> dict:
    """手算可验证的 2 节点小算例，用于灵敏度/可行性的解析对照。

    参数选择使末端电压 ≈ 0.95 p.u. 量级：
      Z = (0.5 + j0.5) Ω, U = 10 kV, S = 0.5 MW + j0.2 Mvar
    """
    return {
        "version": "1.0",
        "meta": {
            "name": "two-bus-test",
            "source": "hand-built",
            "description": "2 节点手算算例",
            "vn_kv": 10.0,
            "f_hz": 50.0,
        },
        "buses": [
            {"index": 0, "name": "slack", "vn_kv": 10.0},
            {"index": 1, "name": "load", "vn_kv": 10.0},
        ],
        "lines": [
            {
                "index": 0,
                "name": "l0",
                "from_bus": 0,
                "to_bus": 1,
                "length_km": 1.0,
                "r_ohm_per_km": 0.5,
                "x_ohm_per_km": 0.5,
                "c_nf_per_km": 0.0,
                "max_i_ka": 0.2,
            }
        ],
        "trafos": [],
        "loads": [{"index": 0, "bus": 1, "p_mw": 0.5, "q_mvar": 0.2}],
        "sgens": [],
        "ext_grids": [{"index": 0, "bus": 0, "vm_pu": 1.0}],
        "switches": [],
    }


@pytest.fixture()
def two_bus_dict() -> dict:
    return make_two_bus_case()
