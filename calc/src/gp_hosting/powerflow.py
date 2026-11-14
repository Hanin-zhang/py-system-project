"""交流潮流计算封装。

对外只暴露 `run(net, options)`，统一潮流算法参数与结果结构，
上层（timeseries / hc_solver / mitigation）都通过它跑潮流。

关键约定：
- 输入输出一律 **MW / kV / p.u. / degree**，不做隐式单位换算。
- 不收敛不抛 500，返回 `converged=False` + 结构化 error。
- 结果结构稳定，字段名 snake_case（Java 侧 DTO 显式 @JsonProperty）。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

import pandapower as pp
from pandapower.powerflow import LoadflowNotConverged

# --------------------------------------------------------------------------
# 参数与结果
# --------------------------------------------------------------------------


@dataclass
class PowerFlowOptions:
    """潮流求解参数。默认值对应辐射状中压配网的常规做法。

    calculate_voltage_angles=False：配网只有单电源，相角对电压幅值几乎无影响，
    关掉可减少一阶状态量与求解时间。若后续接入多电源，再按需打开。
    """

    algorithm: str = "nr"
    init: str = "auto"
    max_iteration: int = 30
    tolerance_mva: float = 1e-8
    calculate_voltage_angles: bool = False
    trafo_model: str = "t"
    voltage_depend_loads: bool = False


@dataclass
class BusResult:
    index: int
    name: str
    vn_kv: float
    vm_pu: float
    va_degree: float
    p_mw: float
    q_mvar: float


@dataclass
class LineResult:
    index: int
    name: str
    from_bus: int
    to_bus: int
    loading_percent: float
    p_from_mw: float
    q_from_mvar: float
    pl_mw: float
    i_ka: float
    max_i_ka: float


@dataclass
class TrafoResult:
    index: int
    name: str
    hv_bus: int
    lv_bus: int
    loading_percent: float
    p_hv_mw: float
    q_hv_mvar: float
    pl_mw: float


@dataclass
class SgenResult:
    index: int
    name: str
    bus: int
    p_mw: float
    q_mvar: float


@dataclass
class PowerFlowSummary:
    total_load_mw: float
    total_sgen_mw: float
    total_p_loss_mw: float
    min_vm_pu: float
    min_vm_bus: int | None
    min_vm_bus_name: str
    max_loading_percent: float
    max_loading_element_type: str  # "line" | "trafo" | ""
    max_loading_element_index: int | None


@dataclass
class PowerFlowError:
    code: str
    message: str
    detail: dict = field(default_factory=dict)


@dataclass
class PowerFlowResult:
    converged: bool
    bus: list[BusResult] = field(default_factory=list)
    line: list[LineResult] = field(default_factory=list)
    trafo: list[TrafoResult] = field(default_factory=list)
    sgen: list[SgenResult] = field(default_factory=list)
    summary: PowerFlowSummary | None = None
    error: PowerFlowError | None = None
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# 求解
# --------------------------------------------------------------------------


def _extract_result(net: pp.pandapowerNet, elapsed_ms: float) -> PowerFlowResult:
    # res_bus 只含结果列，name/vn_kv 需要回查 net.bus
    buses = [
        BusResult(
            index=int(idx),
            name=str(net.bus.at[idx, "name"] or ""),
            vn_kv=float(net.bus.at[idx, "vn_kv"]),
            vm_pu=float(row["vm_pu"]),
            va_degree=float(row.get("va_degree") or 0.0),
            p_mw=float(row.get("p_mw") or 0.0),
            q_mvar=float(row.get("q_mvar") or 0.0),
        )
        for idx, row in net.res_bus.iterrows()
    ]

    res_line = net.res_line
    lines = [
        LineResult(
            index=int(idx),
            name=str(net.line.at[idx, "name"] or ""),
            from_bus=int(net.line.at[idx, "from_bus"]),
            to_bus=int(net.line.at[idx, "to_bus"]),
            loading_percent=float(row["loading_percent"]),
            p_from_mw=float(row["p_from_mw"]),
            q_from_mvar=float(row["q_from_mvar"]),
            pl_mw=float(row["pl_mw"]),
            i_ka=float(row["i_ka"]),
            max_i_ka=float(net.line.at[idx, "max_i_ka"]),
        )
        for idx, row in res_line.iterrows()
    ]

    res_trafo = net.res_trafo
    trafos = [
        TrafoResult(
            index=int(idx),
            name=str(net.trafo.at[idx, "name"] or ""),
            hv_bus=int(net.trafo.at[idx, "hv_bus"]),
            lv_bus=int(net.trafo.at[idx, "lv_bus"]),
            loading_percent=float(row["loading_percent"]),
            p_hv_mw=float(row["p_hv_mw"]),
            q_hv_mvar=float(row["q_hv_mvar"]),
            pl_mw=float(row["pl_mw"]),
        )
        for idx, row in res_trafo.iterrows()
    ]

    if len(net.sgen):
        res_sgen = net.res_sgen
        sgens = [
            SgenResult(
                index=int(idx),
                name=str(net.sgen.at[idx, "name"] or ""),
                bus=int(net.sgen.at[idx, "bus"]),
                p_mw=float(row["p_mw"]),
                q_mvar=float(row["q_mvar"]),
            )
            for idx, row in res_sgen.iterrows()
        ]
    else:
        sgens = []

    total_load_mw = float(net.res_load.p_mw.sum()) if len(net.load) else 0.0
    total_sgen_mw = float(net.res_sgen.p_mw.sum()) if len(net.sgen) else 0.0
    total_p_loss_mw = float(net.res_line.pl_mw.sum() + net.res_trafo.pl_mw.sum())

    min_vm_bus = None
    min_vm_pu = float("nan")
    min_vm_bus_name = ""
    if buses:
        worst = min(buses, key=lambda b: b.vm_pu)
        min_vm_bus, min_vm_pu, min_vm_bus_name = worst.index, worst.vm_pu, worst.name

    max_loading_percent = 0.0
    max_loading_type = ""
    max_loading_index: int | None = None
    if lines:
        worst_line = max(lines, key=lambda x: x.loading_percent)
        if worst_line.loading_percent > max_loading_percent:
            max_loading_percent = worst_line.loading_percent
            max_loading_type = "line"
            max_loading_index = worst_line.index
    if trafos:
        worst_trafo = max(trafos, key=lambda x: x.loading_percent)
        if worst_trafo.loading_percent > max_loading_percent:
            max_loading_percent = worst_trafo.loading_percent
            max_loading_type = "trafo"
            max_loading_index = worst_trafo.index

    summary = PowerFlowSummary(
        total_load_mw=total_load_mw,
        total_sgen_mw=total_sgen_mw,
        total_p_loss_mw=total_p_loss_mw,
        min_vm_pu=min_vm_pu,
        min_vm_bus=min_vm_bus,
        min_vm_bus_name=min_vm_bus_name,
        max_loading_percent=max_loading_percent,
        max_loading_element_type=max_loading_type,
        max_loading_element_index=max_loading_index,
    )

    return PowerFlowResult(
        converged=True,
        bus=buses,
        line=lines,
        trafo=trafos,
        sgen=sgens,
        summary=summary,
        elapsed_ms=elapsed_ms,
    )


def run(net: pp.pandapowerNet, options: PowerFlowOptions | None = None) -> PowerFlowResult:
    """对 net 执行一次交流潮流。异常一律转成结构化结果，不向调用方抛。"""
    opts = options or PowerFlowOptions()
    start = time.perf_counter()

    try:
        pp.runpp(
            net,
            algorithm=opts.algorithm,
            init=opts.init,
            max_iteration=opts.max_iteration,
            tolerance_mva=opts.tolerance_mva,
            calculate_voltage_angles=opts.calculate_voltage_angles,
            trafo_model=opts.trafo_model,
            voltage_depend_loads=opts.voltage_depend_loads,
            numba=False,
        )
    except LoadflowNotConverged as exc:
        return PowerFlowResult(
            converged=False,
            error=PowerFlowError(
                code="LOADFLOW_NOT_CONVERGED",
                message="潮流计算不收敛",
                detail={"reason": str(exc), "algorithm": opts.algorithm, "max_iteration": opts.max_iteration},
            ),
            elapsed_ms=(time.perf_counter() - start) * 1000,
        )
    except Exception as exc:  # noqa: BLE001 - 兜底为结构化错误
        return PowerFlowResult(
            converged=False,
            error=PowerFlowError(
                code="LOADFLOW_ERROR",
                message="潮流计算异常",
                detail={"exception": type(exc).__name__, "reason": str(exc)},
            ),
            elapsed_ms=(time.perf_counter() - start) * 1000,
        )

    return _extract_result(net, (time.perf_counter() - start) * 1000)
