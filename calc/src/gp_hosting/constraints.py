"""运行约束定义与越限判定。

约束判据集中在这里，保证"潮流算得对"和"算完之后判定越限"用的是同一套阈值：
- `PowerFlowResult` 只负责物理量
- 本模块负责"这些物理量算不算违规"

I1 用于单断面判定；I3 的承载力可行性判定会复用同一套逻辑（只是输入变成时序结果）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from gp_hosting.powerflow import PowerFlowResult


@dataclass
class Constraints:
    """运行约束默认值。

    vm_min_pu / vm_max_pu: GB/T 12325 10 kV 及以下三相供电电压偏差 ±7%
    line_loading_percent_max: 设备额定容量
    trafo_loading_percent_max: 同上；可配为 80 作保守场景
    """

    vm_min_pu: float = 0.93
    vm_max_pu: float = 1.07
    line_loading_percent_max: float = 100.0
    trafo_loading_percent_max: float = 100.0

    def __post_init__(self) -> None:
        if not (0.0 < self.vm_min_pu < self.vm_max_pu):
            raise ValueError(
                f"电压约束不合法：需 0 < vm_min_pu < vm_max_pu，实际 {self.vm_min_pu} / {self.vm_max_pu}"
            )
        if self.line_loading_percent_max <= 0 or self.trafo_loading_percent_max <= 0:
            raise ValueError("负载率上限必须 > 0")

    def to_dict(self) -> dict:
        return asdict(self)


# 约束预设。只收录能说清出处的组合，不编造"某某标准"。
CONSTRAINT_PRESETS: dict[str, dict] = {
    "gbt_12325": {
        "values": {"vm_min_pu": 0.93, "vm_max_pu": 1.07,
                   "line_loading_percent_max": 100.0, "trafo_loading_percent_max": 100.0},
        "note": "GB/T 12325 20 kV 及以下三相供电电压偏差 ±7%；设备按额定载流量 100%。本平台默认值。",
    },
    "relaxed_0.90_1.10": {
        "values": {"vm_min_pu": 0.90, "vm_max_pu": 1.10,
                   "line_loading_percent_max": 100.0, "trafo_loading_percent_max": 100.0},
        "note": "±10% 宽松电压带，学术文献中对 IEEE 33 常用的取值；非国标，仅作敏感性对照。",
    },
}


def constraints_from_preset(name: str, **overrides: float) -> Constraints:
    if name not in CONSTRAINT_PRESETS:
        raise ValueError(f"未知约束预设 {name!r}，可选：{sorted(CONSTRAINT_PRESETS)}")
    values = dict(CONSTRAINT_PRESETS[name]["values"])
    values.update({k: v for k, v in overrides.items() if v is not None})
    return Constraints(**values)


@dataclass
class Violation:
    constraint: str  # voltage_low | voltage_high | line_overload | trafo_overload
    element_type: str  # bus | line | trafo
    element_index: int
    element_name: str
    value: float
    limit: float
    severity: float  # 超出限值的相对幅度，> 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ViolationReport:
    feasible: bool
    violations: list[Violation]

    def to_dict(self) -> dict:
        return {"feasible": self.feasible, "violations": [v.to_dict() for v in self.violations]}


def check(result: PowerFlowResult, constraints: Constraints | None = None) -> ViolationReport:
    """判定单断面潮流结果是否越限。不收敛视为不可行。"""
    c = constraints or Constraints()
    violations: list[Violation] = []

    if not result.converged:
        return ViolationReport(feasible=False, violations=violations)

    for bus in result.bus:
        if bus.vm_pu < c.vm_min_pu:
            violations.append(
                Violation(
                    constraint="voltage_low",
                    element_type="bus",
                    element_index=bus.index,
                    element_name=bus.name,
                    value=bus.vm_pu,
                    limit=c.vm_min_pu,
                    severity=(c.vm_min_pu - bus.vm_pu) / c.vm_min_pu,
                )
            )
        elif bus.vm_pu > c.vm_max_pu:
            violations.append(
                Violation(
                    constraint="voltage_high",
                    element_type="bus",
                    element_index=bus.index,
                    element_name=bus.name,
                    value=bus.vm_pu,
                    limit=c.vm_max_pu,
                    severity=(bus.vm_pu - c.vm_max_pu) / c.vm_max_pu,
                )
            )

    for line in result.line:
        if line.loading_percent > c.line_loading_percent_max:
            violations.append(
                Violation(
                    constraint="line_overload",
                    element_type="line",
                    element_index=line.index,
                    element_name=line.name,
                    value=line.loading_percent,
                    limit=c.line_loading_percent_max,
                    severity=(line.loading_percent - c.line_loading_percent_max)
                    / c.line_loading_percent_max,
                )
            )

    for trafo in result.trafo:
        if trafo.loading_percent > c.trafo_loading_percent_max:
            violations.append(
                Violation(
                    constraint="trafo_overload",
                    element_type="trafo",
                    element_index=trafo.index,
                    element_name=trafo.name,
                    value=trafo.loading_percent,
                    limit=c.trafo_loading_percent_max,
                    severity=(trafo.loading_percent - c.trafo_loading_percent_max)
                    / c.trafo_loading_percent_max,
                )
            )

    return ViolationReport(feasible=len(violations) == 0, violations=violations)


# --------------------------------------------------------------------------
# 时序判定（I3）：同一套阈值、同一套比较语义（严格 < / >，等于限值算合规），
# 只是输入从"单断面"变成"时段 × 元件"矩阵。
# --------------------------------------------------------------------------


@dataclass
class TimeseriesViolation:
    """某个元件在某条约束上的全天越限汇总。"""

    constraint: str  # voltage_low | voltage_high | line_overload | trafo_overload | not_converged
    element_type: str  # bus | line | trafo | network
    element_index: int
    limit: float | None
    first_step: int
    first_time: str
    worst_step: int
    worst_time: str
    worst_value: float | None
    n_steps: int  # 越限时段数
    severity: float  # 最严重时段的相对越限幅度

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TimeseriesViolationReport:
    feasible: bool
    unconverged_steps: list[int]
    violations: list[TimeseriesViolation]  # 按首次越限时段升序，同时段按严重度降序

    @property
    def first(self) -> TimeseriesViolation | None:
        return self.violations[0] if self.violations else None

    @property
    def constraint_types(self) -> list[str]:
        return sorted({v.constraint for v in self.violations})

    def to_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "unconverged_steps": self.unconverged_steps,
            "constraint_types": self.constraint_types,
            "first": self.first.to_dict() if self.first else None,
            "violations": [v.to_dict() for v in self.violations],
        }


def check_timeseries(result, constraints: Constraints | None = None) -> TimeseriesViolationReport:
    """判定一天时序结果是否全时段满足约束。

    `result` 为 `timeseries.TimeseriesResult`（这里不做类型导入，避免 constraints 依赖时序层）。
    不收敛时段一律视为不可行，并作为 `not_converged` 违规项返回 —— 承载力搜索中
    "大容量下潮流发散"本身就说明该容量不可接入，不能被静默跳过。
    """
    import numpy as np

    from gp_hosting.timeseries import step_to_hhmm

    c = constraints or Constraints()
    n_steps = len(result.converged)
    violations: list[TimeseriesViolation] = []

    def _collect(matrix, index, mask, constraint, element_type, limit, upper: bool):
        if matrix.size == 0:
            return
        for col in np.flatnonzero(mask.any(axis=0)):
            steps = np.flatnonzero(mask[:, col])
            values = matrix[steps, col]
            k = int(np.argmax(values)) if upper else int(np.argmin(values))
            worst_step, worst_value = int(steps[k]), float(values[k])
            sev = (worst_value - limit) / limit if upper else (limit - worst_value) / limit
            violations.append(
                TimeseriesViolation(
                    constraint=constraint,
                    element_type=element_type,
                    element_index=int(index[col]),
                    limit=limit,
                    first_step=int(steps[0]),
                    first_time=step_to_hhmm(int(steps[0]), n_steps),
                    worst_step=worst_step,
                    worst_time=step_to_hhmm(worst_step, n_steps),
                    worst_value=worst_value,
                    n_steps=int(len(steps)),
                    severity=float(sev),
                )
            )

    # NaN（不收敛时段）参与比较恒为 False，不会被误判为越限
    with np.errstate(invalid="ignore"):
        vm, ll, tl = result.vm_pu, result.line_loading, result.trafo_loading
        _collect(vm, result.bus_index, vm < c.vm_min_pu, "voltage_low", "bus", c.vm_min_pu, False)
        _collect(vm, result.bus_index, vm > c.vm_max_pu, "voltage_high", "bus", c.vm_max_pu, True)
        _collect(ll, result.line_index, ll > c.line_loading_percent_max, "line_overload", "line",
                 c.line_loading_percent_max, True)
        _collect(tl, result.trafo_index, tl > c.trafo_loading_percent_max, "trafo_overload", "trafo",
                 c.trafo_loading_percent_max, True)

    unconverged = [int(s) for s in np.flatnonzero(~np.asarray(result.converged, dtype=bool))]
    if unconverged:
        violations.append(
            TimeseriesViolation(
                constraint="not_converged",
                element_type="network",
                element_index=-1,
                limit=None,
                first_step=unconverged[0],
                first_time=step_to_hhmm(unconverged[0], n_steps),
                worst_step=unconverged[0],
                worst_time=step_to_hhmm(unconverged[0], n_steps),
                worst_value=None,
                n_steps=len(unconverged),
                severity=0.0,  # 无"超出幅度"可言；排序时单独置顶，不用 inf（JSON 不可序列化）
            )
        )

    # 首次越限时段升序；同一时段内不收敛优先，其余按严重度降序
    violations.sort(key=lambda v: (v.first_step, v.constraint != "not_converged", -v.severity))
    return TimeseriesViolationReport(
        feasible=not violations, unconverged_steps=unconverged, violations=violations
    )
