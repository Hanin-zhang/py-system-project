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

    def to_dict(self) -> dict:
        return asdict(self)


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
