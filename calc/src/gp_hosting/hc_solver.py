"""光伏承载力（Hosting Capacity）求解。

一句话能力：**给定节点，算出它最多能接多少光伏**，并说明被哪条约束、在什么时刻卡住。

承载力定义（本平台口径，写死在这里，文档与接口都引用这一处）：
    在给定算例、负荷曲线、光伏曲线、约束下，单个接入点的最大装机容量 C*，
    使得容量区间 [0, C*] 内**全天所有时段**都满足约束且潮流收敛。

    注意是"区间内都可行"，而不是"存在一个可行的大容量"。若可行性在容量上不单调
    （小容量不可行、大容量又可行），取第一次变为不可行之前的容量——这是保守口径。

求解流程（每步都写入结果，便于事后核对）：
    1. 基线（C=0）可行性。不可行 => C*=0，limiting_type=baseline_infeasible，直接返回
    2. 括号：从 initial_mw 起倍增，直到不可行或达到 max_capacity_mw
    3. 单调性粗扫：在 (0, 括号上界] 上均匀取 10 点，检查
         a) 可行性呈"前缀"形态（可行...可行 不可行...不可行）
         b) max_vm（全天最高电压）随容量不降
         c) max_line_loading（全天最大线路负载率）随容量不降
       任何一条不满足 => monotonic=False，原因逐条写入 monotonicity.reasons，绝不静默降级
    4. 搜索：
         monotonic=True  -> 在粗扫的最后可行点与第一个不可行点之间二分
         monotonic=False -> 线性加密扫描（步长 = 括号上界/100），在首个不可行点前再二分
       两者共用同一个可行性函数 `_evaluate`
    5. 探针：评估 C* + probe_mw（默认 50 kW），必须不可行；否则 probe.passed=False

关于"最差电压随容量下降"：排期原文这样写，但对光伏而言不成立。全天最低电压出现在
光伏出力为 0 的夜间负荷高峰，与装机容量无关（实测恒为同一数值）；随容量单调变化的是
正午的**最高电压**。因此 3b 检查的是 max_vm 而非 min_vm，min_vm 仅记录不判定。

性能：同一节点的全部评估复用一张网（`TimeseriesRunner`），不重建、不深拷贝。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

import numpy as np

from gp_hosting import timeseries
from gp_hosting.constraints import Constraints, TimeseriesViolationReport, check_timeseries

CAP_DECIMALS = 9      # 容量统一舍入到 1e-9 MW：缓存键、曲线点、max_mw 必须是同一个数，否则会出现
                      # "max_mw=2.3437499999 而曲线上被评估的点是 2.34375"这种比 max_mw 大一个浮点误差的点
MONOTONIC_TOL = 1e-6  # 指标单调性比较容差（p.u. 与 % 都远大于潮流收敛精度 1e-8 MVA 带来的抖动）


@dataclass
class HcOptions:
    tol_mw: float = 0.010          # 二分终止精度：10 kW
    initial_mw: float = 0.5        # 括号起点
    max_capacity_mw: float = 50.0  # 括号上限；达到仍可行 => limiting_type=not_reached
    coarse_points: int = 10        # 单调性粗扫点数（排期要求 10 点）
    probe_mw: float = 0.050        # 双向探针偏移：50 kW
    linear_divisions: int = 100    # 非单调时线性扫描的分段数

    def __post_init__(self) -> None:
        if self.tol_mw <= 0 or self.tol_mw >= self.probe_mw:
            # 精度必须细于探针偏移，否则"+50 kW 必不可行"在构造上就无法保证
            raise ValueError(f"tol_mw 必须在 (0, probe_mw) 内：tol={self.tol_mw}, probe={self.probe_mw}")
        if not (0 < self.initial_mw <= self.max_capacity_mw):
            raise ValueError("需 0 < initial_mw <= max_capacity_mw")
        if self.coarse_points < 3:
            raise ValueError("coarse_points 至少为 3")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvalPoint:
    """一次全天评估的结果（承载力曲线上的一个点）。"""

    capacity_mw: float
    feasible: bool
    min_vm_pu: float | None
    max_vm_pu: float | None
    max_line_loading_percent: float | None
    max_trafo_loading_percent: float | None
    n_unconverged: int
    first_violation: str | None        # 首个越限的约束类型
    phase: str                         # baseline | bracket | coarse | bisect | linear | probe

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Monotonicity:
    monotonic: bool
    checked_points: int
    reasons: list[str] = field(default_factory=list)
    min_vm_trend: str = ""             # 仅记录：flat / increasing / decreasing / mixed

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HcResult:
    bus: int
    max_mw: float
    limiting_type: str                 # voltage_high | voltage_low | line_overload | trafo_overload
                                       # | not_converged | baseline_infeasible | not_reached
    limiting_element_type: str | None
    limiting_element_index: int | None
    limiting_step: int | None
    limiting_time: str | None
    limiting_value: float | None       # 受限元件在越限时段内的最严重值（p.u. 或 %）
    search_method: str                 # bisection | linear | none
    monotonicity: Monotonicity
    probe: dict
    iterations: int                    # 全天评估次数（含探针）
    elapsed_ms: float
    at_max: EvalPoint | None
    curve: list[EvalPoint]
    violations_above: dict | None      # 刚越过 C* 时的违规报告
    constraints: dict
    options: dict
    scenario: dict

    def to_dict(self, include_curve: bool = True) -> dict:
        d = {
            "bus": self.bus,
            "max_mw": self.max_mw,
            "limiting_type": self.limiting_type,
            "limiting_element_type": self.limiting_element_type,
            "limiting_element_index": self.limiting_element_index,
            "limiting_step": self.limiting_step,
            "limiting_time": self.limiting_time,
            "limiting_value": self.limiting_value,
            "search_method": self.search_method,
            "monotonicity": self.monotonicity.to_dict(),
            "probe": self.probe,
            "iterations": self.iterations,
            "elapsed_ms": self.elapsed_ms,
            "at_max": self.at_max.to_dict() if self.at_max else None,
            "violations_above": self.violations_above,
            "constraints": self.constraints,
            "options": self.options,
            "scenario": self.scenario,
        }
        if include_curve:
            d["curve"] = [p.to_dict() for p in self.curve]
        return d


class _Evaluator:
    """对单个接入节点做"容量 -> 全天可行性"评估，带缓存。"""

    def __init__(self, scenario: timeseries.Scenario, constraints: Constraints):
        self.runner = timeseries.TimeseriesRunner(scenario)
        self.constraints = constraints
        self._cache: dict[float, tuple[EvalPoint, TimeseriesViolationReport]] = {}

    @staticmethod
    def _key(c: float) -> float:
        return _cap(c)

    def __call__(self, capacity_mw: float, phase: str) -> tuple[EvalPoint, TimeseriesViolationReport]:
        k = self._key(capacity_mw)
        if k in self._cache:
            return self._cache[k]
        res = self.runner.run([k])
        report = check_timeseries(res, self.constraints)
        s = res.summary

        def _f(x):
            return None if x is None or not np.isfinite(x) else float(x)

        point = EvalPoint(
            capacity_mw=k,
            feasible=report.feasible,
            min_vm_pu=_f(s.min_vm_pu),
            max_vm_pu=_f(s.max_vm_pu),
            max_line_loading_percent=_f(s.max_line_loading_percent) if self.runner.n_line else None,
            max_trafo_loading_percent=_f(s.max_trafo_loading_percent) if self.runner.n_trafo else None,
            n_unconverged=len(report.unconverged_steps),
            first_violation=report.first.constraint if report.first else None,
            phase=phase,
        )
        self._cache[k] = (point, report)
        return point, report

    @property
    def n_evaluations(self) -> int:
        return len(self._cache)

    def points(self) -> list[EvalPoint]:
        return sorted((p for p, _ in self._cache.values()), key=lambda p: p.capacity_mw)


def _cap(c: float) -> float:
    return round(float(c), CAP_DECIMALS)


def _trend(values: list[float]) -> str:
    d = np.diff(values)
    if np.all(np.abs(d) <= MONOTONIC_TOL):
        return "flat"
    if np.all(d >= -MONOTONIC_TOL):
        return "increasing"
    if np.all(d <= MONOTONIC_TOL):
        return "decreasing"
    return "mixed"


def check_monotonicity(points: list[EvalPoint]) -> Monotonicity:
    """对按容量升序排列的评估点做单调性判定。纯函数，便于用构造数据单测。"""
    pts = sorted(points, key=lambda p: p.capacity_mw)
    reasons: list[str] = []

    feas = [p.feasible for p in pts]
    if True in feas[feas.index(False):] if False in feas else False:
        first_bad = feas.index(False)
        revived = [pts[i].capacity_mw for i in range(first_bad, len(pts)) if feas[i]]
        reasons.append(
            f"可行性非前缀：{pts[first_bad].capacity_mw:.3f} MW 已不可行，"
            f"但更大的 {', '.join(f'{c:.3f}' for c in revived)} MW 又可行"
        )

    def _non_decreasing(attr: str, label: str) -> None:
        series = [(p.capacity_mw, getattr(p, attr)) for p in pts if getattr(p, attr) is not None]
        for (c0, v0), (c1, v1) in zip(series, series[1:]):
            if v1 < v0 - MONOTONIC_TOL:
                reasons.append(f"{label} 在 {c0:.3f}→{c1:.3f} MW 由 {v0:.6f} 降至 {v1:.6f}")
                return

    _non_decreasing("max_vm_pu", "max_vm_pu")
    _non_decreasing("max_line_loading_percent", "max_line_loading_percent")

    min_vm = [p.min_vm_pu for p in pts if p.min_vm_pu is not None]
    return Monotonicity(
        monotonic=not reasons,
        checked_points=len(pts),
        reasons=reasons,
        min_vm_trend=_trend(min_vm) if len(min_vm) >= 2 else "",
    )


def _bisect(evaluate: _Evaluator, lo: float, hi: float, tol: float, phase: str) -> tuple[float, float]:
    """前提：lo 可行、hi 不可行。返回收紧后的 (lo, hi)，hi - lo <= tol。"""
    while hi - lo > tol:
        mid = _cap(0.5 * (lo + hi))
        if mid in (lo, hi):  # 已到舍入分辨率，无法再细分
            break
        point, _ = evaluate(mid, phase)
        if point.feasible:
            lo = mid
        else:
            hi = mid
    return lo, hi


def solve_single(
    scenario: timeseries.Scenario,
    constraints: Constraints | None = None,
    options: HcOptions | None = None,
) -> HcResult:
    """单节点承载力。`scenario.spec.sgens` 必须恰好 1 个接入点（容量字段被忽略）。"""
    c = constraints or Constraints()
    opts = options or HcOptions()
    if len(scenario.spec.sgens) != 1:
        raise timeseries.ProfileError(
            f"单节点承载力要求恰好 1 个光伏接入点，实际 {len(scenario.spec.sgens)} 个",
            {"field": "sgens"},
        )
    if scenario.pv_profile is None:
        raise timeseries.ProfileError("承载力评估必须提供光伏曲线", {"field": "pv_profile_path"})

    bus = scenario.spec.sgens[0].bus
    t0 = time.perf_counter()
    ev = _Evaluator(scenario, c)

    def _finish(max_mw, limiting_report, method, mono, probe, override: str | None = None) -> HcResult:
        at_max = ev(max_mw, "bisect")[0] if max_mw > 0 else ev(0.0, "baseline")[0]
        first = limiting_report.first if limiting_report is not None else None
        return HcResult(
            bus=bus,
            max_mw=float(max_mw),
            limiting_type=override or (first.constraint if first else "not_reached"),
            limiting_element_type=first.element_type if first else None,
            limiting_element_index=first.element_index if first else None,
            limiting_step=first.first_step if first else None,
            limiting_time=first.first_time if first else None,
            limiting_value=first.worst_value if first else None,
            search_method=method,
            monotonicity=mono,
            probe=probe,
            iterations=ev.n_evaluations,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            at_max=at_max,
            curve=ev.points(),
            violations_above=limiting_report.to_dict() if limiting_report is not None else None,
            constraints=c.to_dict(),
            options=opts.to_dict(),
            scenario={
                "case": scenario.case_spec.meta.name,
                "case_path": scenario.spec.case_path,
                "load_profile": scenario.load_profile.name,
                "pv_profile": scenario.pv_profile.name if scenario.pv_profile else None,
                "load_scale": scenario.spec.load_scale,
                "n_steps": scenario.n_steps,
                "power_factor": scenario.spec.sgens[0].power_factor,
            },
        )

    # ---- 1. 基线 ----
    base, base_report = ev(0.0, "baseline")
    if not base.feasible:
        # 基线就越限：与光伏无关，是网络本身的问题；reasons 不写内容，避免被误读为"非单调"
        mono = Monotonicity(monotonic=True, checked_points=1)
        return _finish(
            0.0, base_report, "none", mono,
            {"skipped": True, "reason": "baseline_infeasible"}, override="baseline_infeasible",
        )

    # ---- 2. 括号 ----
    lo, hi = 0.0, opts.initial_mw
    while True:
        p, _ = ev(hi, "bracket")
        if not p.feasible:
            break
        lo = hi
        if hi >= opts.max_capacity_mw:
            hi = None
            break
        hi = _cap(min(hi * 2, opts.max_capacity_mw))

    if hi is None:
        coarse = [ev(_cap(x), "coarse")[0] for x in np.linspace(0, opts.max_capacity_mw, opts.coarse_points + 1)[1:]]
        mono = check_monotonicity([base, *coarse])
        return _finish(
            opts.max_capacity_mw, None, "none", mono,
            {"skipped": True, "reason": f"达到搜索上限 {opts.max_capacity_mw} MW 仍可行"},
            override="not_reached",
        )

    # ---- 3. 单调性粗扫 ----
    grid = [_cap(x) for x in np.linspace(0, hi, opts.coarse_points + 1)[1:]]
    coarse = [ev(x, "coarse")[0] for x in grid]
    mono = check_monotonicity([base, *coarse])

    # ---- 4. 搜索 ----
    if mono.monotonic:
        method = "bisection"
        last_ok = max((p.capacity_mw for p in [base, *coarse] if p.feasible), default=0.0)
        first_bad = min(p.capacity_mw for p in coarse if not p.feasible)
        lo, hi = _bisect(ev, last_ok, first_bad, opts.tol_mw, "bisect")
    else:
        method = "linear"
        step = hi / opts.linear_divisions
        prev = 0.0
        first_bad = hi
        for i in range(1, opts.linear_divisions + 1):
            x = _cap(step * i)
            if not ev(x, "linear")[0].feasible:
                first_bad = x
                break
            prev = x
        lo, hi = _bisect(ev, prev, first_bad, opts.tol_mw, "linear")

    _, hi_report = ev(hi, "bisect")

    # ---- 5. 双向探针 ----
    below, _ = ev(lo, "bisect")
    above, above_report = ev(_cap(lo + opts.probe_mw), "probe")
    probe = {
        "probe_mw": opts.probe_mw,
        "at_max": {"capacity_mw": below.capacity_mw, "feasible": below.feasible},
        "above": {
            "capacity_mw": above.capacity_mw,
            "feasible": above.feasible,
            "n_violations": len(above_report.violations),
            "constraint_types": above_report.constraint_types,
        },
        "passed": below.feasible and not above.feasible,
    }
    return _finish(lo, hi_report, method, mono, probe)


# --------------------------------------------------------------------------
# 多节点批量（排期 3.6）
# --------------------------------------------------------------------------


def _scenario_for_bus(base: timeseries.ScenarioSpec, bus: int, power_factor: float) -> timeseries.Scenario:
    spec = timeseries.ScenarioSpec(
        case_path=base.case_path,
        load_profile_path=base.load_profile_path,
        pv_profile_path=base.pv_profile_path,
        sgens=[timeseries.SgenInjection(bus=bus, capacity_mw=0.0, power_factor=power_factor)],
        load_scale=base.load_scale,
        name=base.name,
    )
    return timeseries.assemble_scenario(spec)


def _solve_bus_worker(args: tuple) -> HcResult:
    """进程池入口（必须是模块级函数才能被 pickle）。"""
    base, bus, power_factor, constraints, options = args
    return solve_single(_scenario_for_bus(base, bus, power_factor), constraints, options)


def solve_batch(
    base: timeseries.ScenarioSpec,
    buses: list[int],
    constraints: Constraints | None = None,
    options: HcOptions | None = None,
    power_factor: float = 1.0,
    workers: int = 1,
) -> list[HcResult]:
    """对多个节点分别求单点承载力（各节点独立，互不叠加）。

    并行策略（实测，见 docs/hosting-capacity.md）：
    - 线程池比串行更慢（GIL + pandas 争用），因此不提供线程选项
    - 进程池 workers>1 有约 1.5× 加速，但每个进程要重新 import pandapower（~2 s）
    默认串行：5 节点约 1 min，已满足 < 3 min 的要求，且结果顺序、日志更易追踪。
    """
    if not buses:
        raise timeseries.ProfileError("节点列表为空", {"field": "buses"})
    if len(set(buses)) != len(buses):
        raise timeseries.ProfileError("节点列表有重复", {"field": "buses", "buses": buses})
    # 先在主进程完成全部校验（节点存在性等），避免进程池里抛出难读的错误
    for b in buses:
        _scenario_for_bus(base, b, power_factor)

    jobs = [(base, b, power_factor, constraints, options) for b in buses]
    if workers <= 1:
        return [_solve_bus_worker(j) for j in jobs]

    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(max_workers=min(workers, len(buses))) as pool:
        return list(pool.map(_solve_bus_worker, jobs))
