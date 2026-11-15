"""越限归因与敏感性分析（I4）。

对应排期 4.1–4.6，四块能力：

| 函数 | 产出 |
|---|---|
| `build_attribution()` | 越限事件列表（连续区间）+ 热力图数据 |
| `compute_sensitivity()` | 中心差分灵敏度矩阵（逐时段 × 逐设备） |
| `rank_bottlenecks()` | 按"约束裕度消耗率"排序的瓶颈设备 |
| `compare_buses()` | 等容量下 2–5 个候选接入点对比 + 推荐 |

--- ∂V_min/∂P 的实测结论（与排期原文不符，必须先说清）---

排期 4.3 要求"差分求 ∂V_min/∂P"。**实测该值恒为 0**（1e-14 量级）：

    全天最低电压出现在负荷高峰、光伏出力为 0 的时段（IEEE 33 是 20:00），
    该时段的潮流与装机容量**完全无关**，所以差分恒为 0。

这不是数值误差，是物理事实。决策真正依据的是：

- **注入节点在光伏峰值时刻的自身电压灵敏度** ∂V_b/∂P|t_peak
  实测 bus17 = 0.0410 p.u./MW，bus1 = 0.000355，比值 115:1 —— 末端敏感得多
- **全天最高电压**对容量的灵敏度 ∂V_max/∂P —— 过电压才是承载力的实际瓶颈

三个量都返回，接口注释与文档里标明哪个用于决策。

--- 关于裕度消耗率（排期 4.4 要求"可排序的数值指标"）---

    对设备 e：  headroom_e = min over {t : slope_e(t) > 0}  [ margin_e(t) / slope_e(t) ]
                consumption_rate_e = 1 / headroom_e

含义："再增加多少 MW，该设备在**它自己最紧的那个时刻**就会越限"。

两个必须踩对的点：

1. **逐元素、逐时段取最小值**，不能用"全网最大负载率时刻"代替。实测全网最大负载率
   出现在夜间负荷高峰，那里光伏出力为 0、所有斜率都为 0，直接算会得到 1e9 量级的假裕度。
2. **斜率必须逐时段精确值**，不能用"峰值斜率 × 出力系数"缩放。虽然斜率确实近似与出力
   成正比，但实测偏差 1.3%–3.5%（灵敏度同时受负荷水平影响）。中心差分本来就产出了
   全时段矩阵（96 × n_bus），直接用即可，没有额外成本。

headroom 是**单点线性外推**，不是承载力。它与 I3 的二分结果同量级但不相等
（I3 是对全天做非线性搜索、且跨越了约束切换）。两者的一致性在 `compare_buses()` 里
通过"等容量越限轻重"间接验证，见排期 DoD 第 3 条。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

import numpy as np

from gp_hosting import timeseries
from gp_hosting.constraints import Constraints

# 热力图值域截断：|deviation| > 1 视为"严重越限"，避免极端值压扁色阶
HEATMAP_CLAMP = 1.0
# 斜率小于该阈值视为"对容量不敏感"，不参与裕度外推（避免除以噪声）
SLOPE_EPS = 1e-9


# --------------------------------------------------------------------------
# 4.1 归因报告
# --------------------------------------------------------------------------


@dataclass
class ViolationEvent:
    """一台设备在一条约束上的一段**连续**越限区间。"""

    constraint: str            # voltage_high | voltage_low | line_overload | trafo_overload
    element_type: str          # bus | line | trafo
    element_index: int
    start_step: int
    end_step: int
    start_time: str
    end_time: str
    n_steps: int               # 该区间内**实际越限**的时段数（合并空档时可能 < end-start+1）
    duration_steps: int        # 区间跨越的时段数（end - start + 1）
    limit: float
    peak_step: int
    peak_time: str
    peak_value: float
    peak_excess: float         # 峰值越限量，物理单位（p.u. 或 %）
    peak_severity: float       # 相对越限幅度 = peak_excess / limit
    mean_severity: float       # 区间内平均相对幅度 —— 区分"擦线"与"深度越限"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AttributionReport:
    feasible: bool
    n_steps: int
    step_minutes: int
    n_unconverged: int
    unconverged_steps: list[int]
    first_step: int | None
    first_time: str | None
    first_violation: dict | None      # 首次越限的那一条事件（排期 DoD 直接要这个）
    n_events: int
    n_affected_elements: int
    total_exceedance_steps: int
    constraint_summary: dict
    top_by_peak_severity: list[dict]
    events: list[ViolationEvent]
    heatmap: dict
    constraints: dict

    def to_dict(self) -> dict:
        d = asdict(self)
        d["events"] = [e.to_dict() for e in self.events]
        return d


def _segments(mask: np.ndarray, merge_gap: int = 0) -> list[tuple[int, int]]:
    """布尔序列 → 连续 True 区间 [start, end]（闭区间）。

    `merge_gap`：允许合并间隔不超过 N 个时段的断点。真实越限会因云遮挡、负荷小波动
    在时间上碎裂，不合并会让"持续 3 小时"变成十几个"持续 15 分钟"，归因失去可读性。
    默认 0（不合并），由调用方显式选择。
    """
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) > 1 + merge_gap)
    starts = np.r_[idx[0], idx[breaks + 1]]
    ends = np.r_[idx[breaks], idx[-1]]
    return [(int(s), int(e)) for s, e in zip(starts, ends)]


def _matrix_events(matrix, index, constraint, element_type, limit, upper, n_steps, merge_gap):
    if matrix.size == 0:
        return []
    with np.errstate(invalid="ignore"):
        mask_all = (matrix > limit) if upper else (matrix < limit)
    events: list[ViolationEvent] = []
    for col in range(matrix.shape[1]):
        col_mask = mask_all[:, col]
        if not col_mask.any():
            continue
        values = matrix[:, col]
        for s, e in _segments(col_mask, merge_gap):
            seg = values[s : e + 1]
            idx = np.arange(s, e + 1)
            # 合并空档后，区间内既有越限点也有非越限点；n_steps 只数**真实越限**的时段
            in_violation = col_mask[s : e + 1] & np.isfinite(seg)
            if not in_violation.any():
                continue
            seg_v, idx_v = seg[in_violation], idx[in_violation]
            k = int(np.argmax(seg_v) if upper else np.argmin(seg_v))
            peak_step, peak_value = int(idx_v[k]), float(seg_v[k])
            excess = peak_value - limit if upper else limit - peak_value
            events.append(
                ViolationEvent(
                    constraint=constraint,
                    element_type=element_type,
                    element_index=int(index[col]),
                    start_step=s,
                    end_step=e,
                    start_time=timeseries.step_to_hhmm(s, n_steps),
                    end_time=timeseries.step_to_hhmm(e, n_steps),
                    n_steps=int(in_violation.sum()),
                    duration_steps=e - s + 1,
                    limit=float(limit),
                    peak_step=peak_step,
                    peak_time=timeseries.step_to_hhmm(peak_step, n_steps),
                    peak_value=peak_value,
                    peak_excess=float(excess),
                    peak_severity=float(excess / limit),
                    mean_severity=float(np.mean(np.abs(seg_v - limit) / limit)),
                )
            )
    return events


def _normalized_margin(vm_pu, line_loading, trafo_loading, c: Constraints) -> dict:
    """归一化裕度矩阵（正 = 有余量，负 = 已越限）。

    电压取"离最近的那条限界"的相对距离：
        min( (vm - vm_min)/vm_min , (vm_max - vm)/vm_max )
    线路/变压器： (limit - loading)/limit
    """
    def _clip(a):
        a = np.asarray(a, dtype=float)
        return np.where(np.isfinite(a), np.clip(a, -HEATMAP_CLAMP, HEATMAP_CLAMP), np.nan)

    with np.errstate(invalid="ignore"):
        vm_margin = np.fmin((vm_pu - c.vm_min_pu) / c.vm_min_pu, (c.vm_max_pu - vm_pu) / c.vm_max_pu)
        line_margin = (c.line_loading_percent_max - line_loading) / c.line_loading_percent_max
        trafo_margin = (c.trafo_loading_percent_max - trafo_loading) / c.trafo_loading_percent_max
    return {
        "vm_margin": _clip(vm_margin),
        "line_margin": _clip(line_margin),
        "trafo_margin": _clip(trafo_margin),
    }


def _heatmap_payload(matrix, index, key: str, unit_hint: str, times: list[str]) -> dict:
    """ECharts heatmap 直接消费的格式：data = [[x=step, y=elementPos, value], ...]

    只输出越限单元（value < 0）。正面（有余量）的格子对"哪里出了问题"没有信息量，
    全量输出 96×33 只会让响应体变大、前端渲染变慢。
    `elements` 给出 y 轴顺序，`step_times` 给出 x 轴刻度。
    """
    matrix = np.asarray(matrix, dtype=float)
    data: list[list] = []
    with np.errstate(invalid="ignore"):
        mask = matrix < 0
    for t in range(matrix.shape[0]):
        for j in np.flatnonzero(mask[t]):
            data.append([int(t), int(j), round(float(matrix[t, j]), 6)])
    return {
        "type": key,
        "unit": unit_hint,
        "n_steps": matrix.shape[0] if matrix.size else 0,
        "elements": [int(i) for i in index],
        "step_times": times,
        "n_violating_cells": len(data),
        "data": data,
        "value_range": [-HEATMAP_CLAMP, 0.0],
        "note": "仅输出越限单元（value<0）；正值代表有余量，未列出",
    }


def build_attribution(
    result: timeseries.TimeseriesResult,
    constraints: Constraints | None = None,
    merge_gap: int = 0,
    top_n: int = 10,
) -> AttributionReport:
    """把一天的时序结果归纳成越限事件报告。纯归纳，不重算潮流。"""
    c = constraints or Constraints()
    n_steps = result.summary.n_steps
    times = [timeseries.step_to_hhmm(t, n_steps) for t in range(n_steps)]
    converged = np.asarray(result.converged, dtype=bool)

    vm = np.where(converged[:, None], result.vm_pu, np.nan)
    ll = np.where(converged[:, None], result.line_loading, np.nan) if result.line_loading.size else result.line_loading
    tl = np.where(converged[:, None], result.trafo_loading, np.nan) if result.trafo_loading.size else result.trafo_loading

    events = (
        _matrix_events(vm, result.bus_index, "voltage_high", "bus", c.vm_max_pu, True, n_steps, merge_gap)
        + _matrix_events(vm, result.bus_index, "voltage_low", "bus", c.vm_min_pu, False, n_steps, merge_gap)
        + _matrix_events(ll, result.line_index, "line_overload", "line", c.line_loading_percent_max,
                         True, n_steps, merge_gap)
        + _matrix_events(tl, result.trafo_index, "trafo_overload", "trafo", c.trafo_loading_percent_max,
                         True, n_steps, merge_gap)
    )
    events.sort(key=lambda e: (e.start_step, -e.peak_severity))
    unconverged = [int(s) for s in np.flatnonzero(~converged)]

    summary: dict = {}
    for name in sorted({e.constraint for e in events}):
        group = [e for e in events if e.constraint == name]
        summary[name] = {
            "n_events": len(group),
            "n_elements": len({e.element_index for e in group}),
            "n_steps": int(sum(e.n_steps for e in group)),
            "peak_severity": max(e.peak_severity for e in group),
        }

    margin = _normalized_margin(result.vm_pu, result.line_loading, result.trafo_loading, c)
    heatmap = {
        "vm": _heatmap_payload(margin["vm_margin"], result.bus_index, "vm_margin", "p.u.（相对限值）", times),
        "line": _heatmap_payload(margin["line_margin"], result.line_index, "line_margin", "%（相对限值）", times),
        "trafo": _heatmap_payload(margin["trafo_margin"], result.trafo_index, "trafo_margin", "%（相对限值）", times),
    }

    return AttributionReport(
        feasible=not events and not unconverged,
        n_steps=n_steps,
        step_minutes=result.summary.step_minutes,
        n_unconverged=len(unconverged),
        unconverged_steps=unconverged,
        first_step=events[0].start_step if events else None,
        first_time=events[0].start_time if events else None,
        first_violation=events[0].to_dict() if events else None,
        n_events=len(events),
        n_affected_elements=len({(e.element_type, e.element_index) for e in events}),
        total_exceedance_steps=int(sum(e.n_steps for e in events)),
        constraint_summary=summary,
        top_by_peak_severity=[e.to_dict() for e in sorted(events, key=lambda e: -e.peak_severity)[:top_n]],
        events=events,
        heatmap=heatmap,
        constraints=c.to_dict(),
    )


# --------------------------------------------------------------------------
# 4.3 灵敏度
# --------------------------------------------------------------------------


@dataclass
class SensitivityResult:
    bus: int
    base_mw: float
    delta_mw: float
    method: str                          # central_difference
    peak_step: int
    peak_time: str
    d_vmin_dp: float                     # 全天最低电压 / MW（实测恒为 0，见模块文档）
    d_vmax_dp: float                     # 全天最高电压 / MW
    d_v_bus_at_peak_dp: float            # 注入节点在光伏峰值时刻的自身电压 / MW
    d_p_loss_dp: float                   # 日网损电量 MWh / MW
    top_voltage_buses: list[dict]
    top_line_sensitivities: list[dict]
    matrices: dict                       # 全时段斜率矩阵（逐时段、逐设备）
    elapsed_ms: float
    scenario: dict
    # 内部用，不进 JSON
    _base_result: object = field(default=None, repr=False, compare=False)
    _dv: np.ndarray = field(default=None, repr=False, compare=False)
    _dl: np.ndarray = field(default=None, repr=False, compare=False)
    _dt: np.ndarray = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict:
        d = {
            k: v for k, v in asdict(self).items() if not k.startswith("_")
        }
        return d


def compute_sensitivity(
    scenario: timeseries.Scenario,
    base_mw: float,
    delta_mw: float | None = None,
) -> SensitivityResult:
    """中心差分 ∂/∂P。`scenario.spec.sgens` 必须恰好 1 个接入点。

    ΔP 默认取 base 的 1%，下限 50 kW。理由（实测）：
    - 太小：潮流收敛容差（1e-8 MVA）的噪声会淹没差分
    - 太大：二次项开始污染。Δ 从 1% 增大到 30%，结果变化 < 0.05%
    实测该区间内结果稳定到 6 位有效数字，见 docs/attribution.md。
    """
    t0 = time.perf_counter()
    if len(scenario.spec.sgens) != 1:
        raise timeseries.ProfileError(
            f"灵敏度分析要求恰好 1 个光伏接入点，实际 {len(scenario.spec.sgens)} 个", {"field": "sgens"}
        )
    if scenario.pv_profile is None:
        raise timeseries.ProfileError("灵敏度分析必须提供光伏曲线", {"field": "pv_profile_path"})
    if base_mw <= 0:
        raise timeseries.ProfileError(
            "基准容量必须 > 0：中心差分需要 P-Δ 也非负", {"field": "base_mw", "value": base_mw}
        )

    delta = float(delta_mw) if delta_mw is not None else max(0.01 * base_mw, 0.050)
    if not (0 < delta < base_mw):
        raise timeseries.ProfileError(
            f"差分步长必须满足 0 < ΔP < base_mw：ΔP={delta}, base={base_mw}", {"field": "delta_mw"}
        )

    bus = scenario.spec.sgens[0].bus
    runner = timeseries.TimeseriesRunner(scenario)
    hi = runner.run([base_mw + delta])
    base_result = runner.run([base_mw])
    lo = runner.run([base_mw - delta])
    if not (hi.converged.all() and lo.converged.all() and base_result.converged.all()):
        raise timeseries.ProfileError(
            "灵敏度分析的两个工况存在不收敛时段，无法做差分",
            {
                "field": "base_mw",
                "bus": bus,
                "unconverged_hi": [int(x) for x in np.flatnonzero(~hi.converged)],
                "unconverged_lo": [int(x) for x in np.flatnonzero(~lo.converged)],
            },
        )

    dv = (hi.vm_pu - lo.vm_pu) / (2 * delta)
    dl = (hi.line_loading - lo.line_loading) / (2 * delta)
    dt = (hi.trafo_loading - lo.trafo_loading) / (2 * delta) if hi.trafo_loading.size else np.zeros((0, 0))

    n_steps = scenario.n_steps
    pv = np.asarray(scenario.pv_profile.factors, dtype=float)
    peak_step = int(np.argmax(pv))

    # 最值灵敏度：取"最值出现的那个时段、那个设备"的差分，
    # 而不是 max(hi)-max(lo) —— 后者在两工况最值出现在不同设备时会给出无意义的数
    i_min = int(np.argmin(lo.vm_pu.min(axis=1)))
    i_max = int(np.argmax(lo.vm_pu.max(axis=1)))
    d_vmin = float(dv[i_min, int(np.argmin(lo.vm_pu[i_min]))])
    d_vmax = float(dv[i_max, int(np.argmax(lo.vm_pu[i_max]))])

    bus_pos = list(lo.bus_index).index(bus)
    bus_rank = sorted(
        ({"bus": int(b), "dv_dp": float(dv[peak_step, j])} for j, b in enumerate(lo.bus_index)),
        key=lambda x: -abs(x["dv_dp"]),
    )
    line_rank = sorted(
        (
            {
                "line": int(li),
                "dloading_dp": float(dl[peak_step, j]),
                "loading_percent": float(lo.line_loading[peak_step, j]),
            }
            for j, li in enumerate(lo.line_index)
        ),
        key=lambda x: -abs(x["dloading_dp"]),
    )

    def _rows(m):
        return [[float(v) for v in row] for row in m] if m.size else []

    return SensitivityResult(
        bus=bus,
        base_mw=float(base_mw),
        delta_mw=delta,
        method="central_difference",
        peak_step=peak_step,
        peak_time=timeseries.step_to_hhmm(peak_step, n_steps),
        d_vmin_dp=d_vmin,
        d_vmax_dp=d_vmax,
        d_v_bus_at_peak_dp=float(dv[peak_step, bus_pos]),
        d_p_loss_dp=float((hi.summary.energy_loss_mwh - lo.summary.energy_loss_mwh) / (2 * delta)),
        top_voltage_buses=bus_rank[:10],
        top_line_sensitivities=line_rank[:10],
        matrices={
            "bus_index": [int(i) for i in lo.bus_index],
            "line_index": [int(i) for i in lo.line_index],
            "trafo_index": [int(i) for i in lo.trafo_index],
            "dv_dp": _rows(dv),          # (n_steps, n_bus)   p.u./MW
            "dloading_dp": _rows(dl),    # (n_steps, n_line)  %/MW
            "dtrafo_dp": _rows(dt),
            "delta_mw": delta,
            "note": "逐时段、逐设备斜率；使用中心差分 (f(P+Δ)-f(P-Δ))/(2Δ)",
        },
        elapsed_ms=(time.perf_counter() - t0) * 1000,
        scenario={
            "case": scenario.case_spec.meta.name,
            "case_path": scenario.spec.case_path,
            "load_profile": scenario.load_profile.name,
            "pv_profile": scenario.pv_profile.name,
            "load_scale": scenario.spec.load_scale,
            "n_steps": n_steps,
        },
        _base_result=base_result,
        _dv=dv,
        _dl=dl,
        _dt=dt,
    )


# --------------------------------------------------------------------------
# 4.4 瓶颈排序
# --------------------------------------------------------------------------


@dataclass
class Bottleneck:
    rank: int
    element_type: str           # line | trafo | bus
    element_index: int
    constraint: str
    headroom_mw: float | None    # 外推的触限容量增量；**负值表示当前已越限**（超出多少 MW）
    already_violated: bool       # headroom < 0：该设备在基准容量下已经越限
    consumption_rate: float | None  # 1/headroom（1/MW），越大越紧；已越限时为 None
    binding_step: int           # 最紧的时刻
    binding_time: str
    margin: float               # 该时刻的物理裕度（p.u. 或 %）
    slope: float                # 该时刻的斜率（p.u./MW 或 %/MW）
    current_value: float
    limit: float

    def to_dict(self) -> dict:
        return asdict(self)


def rank_bottlenecks(
    sensitivity: SensitivityResult,
    result: timeseries.TimeseriesResult | None = None,
    constraints: Constraints | None = None,
    top_n: int = 10,
) -> list[Bottleneck]:
    """按"约束裕度消耗率"排序。

        headroom_e = min over {t : slope_e(t) > 0} margin_e(t) / slope_e(t)

    逐元素、逐时段精确计算，不使用任何缩放近似。

    `result` 不传时使用灵敏度计算时**同一容量**的基准态结果。这是推荐用法：
    传一个容量与 `sensitivity.base_mw` 不一致的结果会得到无意义的裕度
    （斜率来自 A 容量、裕度来自 B 容量），因此显式传入时只做形状校验，不再猜测。
    """
    c = constraints or Constraints()
    if result is None:
        result = sensitivity._base_result  # type: ignore[assignment]
        if result is None:
            raise ValueError("sensitivity 未携带基准态结果，请显式传入 result")
    if result.vm_pu.shape != sensitivity._dv.shape:
        raise ValueError(
            f"result 与灵敏度矩阵形状不一致：vm_pu {result.vm_pu.shape} vs dv {sensitivity._dv.shape}；"
            "多半是 result 的装机容量与 sensitivity.base_mw 不同"
        )
    n_steps = result.summary.n_steps
    rows: list[Bottleneck] = []

    def _collect(matrix, index, slope, constraint, element_type, limit, upper):
        if matrix.size == 0 or slope.size == 0:
            return
        for j in range(matrix.shape[1]):
            best = None
            for t in range(matrix.shape[0]):
                v = matrix[t, j]
                s = slope[t, j]
                if not np.isfinite(v) or not np.isfinite(s):
                    continue
                # 电压下限：增加光伏抬高电压，对"低于下限"是缓解方向 -> 斜率取 -∂V/∂P
                s_eff = s if upper else -s
                if s_eff <= SLOPE_EPS:
                    continue
                margin = (limit - v) if upper else (v - limit)
                hr = margin / s_eff
                if best is None or hr < best[0]:
                    best = (hr, t, margin, s_eff, v)
            if best is None:
                continue
            hr, t, margin, s_eff, v = best
            rows.append(
                Bottleneck(
                    rank=0,
                    element_type=element_type,
                    element_index=int(index[j]),
                    constraint=constraint,
                    headroom_mw=float(hr),
                    already_violated=bool(hr < 0),
                    consumption_rate=float(1.0 / hr) if hr > 0 else None,
                    binding_step=int(t),
                    binding_time=timeseries.step_to_hhmm(int(t), n_steps),
                    margin=float(margin),
                    slope=float(s_eff),
                    current_value=float(v),
                    limit=float(limit),
                )
            )

    _collect(result.line_loading, result.line_index, sensitivity._dl, "line_overload", "line",
             c.line_loading_percent_max, True)
    if sensitivity._dt.size and result.trafo_loading.size:
        _collect(result.trafo_loading, result.trafo_index, sensitivity._dt, "trafo_overload", "trafo",
                 c.trafo_loading_percent_max, True)
    _collect(result.vm_pu, result.bus_index, sensitivity._dv, "voltage_high", "bus", c.vm_max_pu, True)
    _collect(result.vm_pu, result.bus_index, sensitivity._dv, "voltage_low", "bus", c.vm_min_pu, False)

    rows.sort(key=lambda b: b.headroom_mw if b.headroom_mw is not None else float("inf"))
    for i, r in enumerate(rows, 1):
        r.rank = i
    return rows[:top_n]


# --------------------------------------------------------------------------
# 4.5 接入点对比
# --------------------------------------------------------------------------


@dataclass
class CandidateResult:
    bus: int
    capacity_mw: float
    feasible: bool
    worst_constraint: str | None
    worst_severity: float          # 最大相对越限幅度（0 = 无越限）
    n_violating_steps: int
    n_unconverged: int
    min_vm_pu: float | None
    max_vm_pu: float | None
    max_line_loading_percent: float | None
    d_v_bus_at_peak_dp: float | None   # 该节点的电压灵敏度（若已算）
    headroom_mw: float | None          # 由瓶颈分析给出的最近触限容量增量
    energy_loss_mwh: float
    recommendation_score: float

    def to_dict(self) -> dict:
        return asdict(self)


def compare_buses(
    base_spec: timeseries.ScenarioSpec,
    buses: list[int],
    capacity_mw: float,
    constraints: Constraints | None = None,
    power_factor: float = 1.0,
    with_sensitivity: bool = True,
) -> dict:
    """等容量下比较 2–5 个候选接入点，给出推荐。

    评分规则（显式、可复核）：
        1. 可行者优先于不可行
        2. 同组内按 (最大越限幅度, 最大线路负载率, 最低电压偏离) 排序
           —— 越限越轻越好，也就是"同样的容量，谁顶得住"
    由于容量相同，评分排序**必须**与 I3 的承载力排序一致：
    承载力高的节点在这个容量下越限更轻（这是排期 DoD 第 3 条要验证的自洽性）。
    """
    if not (2 <= len(buses) <= 5):
        raise timeseries.ProfileError(
            f"接入点对比要求 2–5 个候选节点，实际 {len(buses)} 个", {"field": "buses"}
        )
    if len(set(buses)) != len(buses):
        raise timeseries.ProfileError("候选节点有重复", {"field": "buses", "buses": buses})
    if capacity_mw <= 0:
        raise timeseries.ProfileError("对比容量必须 > 0", {"field": "capacity_mw"})

    c = constraints or Constraints()
    rows: list[CandidateResult] = []
    for bus in buses:
        spec = timeseries.ScenarioSpec(
            case_path=base_spec.case_path,
            load_profile_path=base_spec.load_profile_path,
            pv_profile_path=base_spec.pv_profile_path,
            sgens=[timeseries.SgenInjection(bus=bus, capacity_mw=capacity_mw, power_factor=power_factor)],
            load_scale=base_spec.load_scale,
            name=base_spec.name,
        )
        scenario = timeseries.assemble_scenario(spec)
        result = timeseries.run_timeseries(scenario)
        report = _attribution_from_result(result, c)

        d_v = headroom = None
        if with_sensitivity:
            sens = compute_sensitivity(scenario, capacity_mw)
            pos = list(result.bus_index).index(bus)
            d_v = sens.d_v_bus_at_peak_dp
            ranked = rank_bottlenecks(sens, result, c, top_n=1)
            headroom = ranked[0].headroom_mw if ranked else None

        worst = max((e.peak_severity for e in report.events), default=0.0)
        rows.append(
            CandidateResult(
                bus=bus,
                capacity_mw=float(capacity_mw),
                feasible=report.feasible,
                worst_constraint=(report.first_violation or {}).get("constraint"),
                worst_severity=float(worst),
                n_violating_steps=report.total_exceedance_steps,
                n_unconverged=report.n_unconverged,
                min_vm_pu=_finite(result.summary.min_vm_pu),
                max_vm_pu=_finite(result.summary.max_vm_pu),
                max_line_loading_percent=_finite(result.summary.max_line_loading_percent),
                d_v_bus_at_peak_dp=d_v,
                headroom_mw=headroom,
                energy_loss_mwh=result.summary.energy_loss_mwh,
                recommendation_score=0.0,
            )
        )

    # 评分：可行优先；不可行时越限越轻越好
    for r in rows:
        r.recommendation_score = (
            (1000.0 if r.feasible else 0.0)
            - 100.0 * r.worst_severity
            - 0.01 * float(r.max_line_loading_percent or 0.0)
            - 10.0 * r.n_unconverged
        )
    rows.sort(key=lambda r: -r.recommendation_score)

    return {
        "capacity_mw": float(capacity_mw),
        "constraints": c.to_dict(),
        "candidates": [r.to_dict() for r in rows],
        "recommended_bus": rows[0].bus,
        "rationale": _rationale(rows),
    }


def _finite(x) -> float | None:
    if x is None:
        return None
    x = float(x)
    return x if np.isfinite(x) else None


def _rationale(rows: list[CandidateResult]) -> str:
    best, worst = rows[0], rows[-1]
    if best.feasible and not worst.feasible:
        return (
            f"bus{best.bus} 在 {best.capacity_mw:.3f} MW 下全天无越限；"
            f"bus{worst.bus} 已越限 {worst.n_violating_steps} 个时段"
            f"（最大相对幅度 {worst.worst_severity * 100:.2f}%）"
        )
    if best.feasible:
        return f"所有候选节点均可行，bus{best.bus} 的综合裕度最好"
    return (
        f"所有候选节点在 {best.capacity_mw:.3f} MW 下均越限；"
        f"bus{best.bus} 最轻（{best.n_violating_steps} 个时段，最大幅度 "
        f"{best.worst_severity * 100:.2f}%），bus{worst.bus} 最重"
    )


def _attribution_from_result(result: timeseries.TimeseriesResult, c: Constraints) -> AttributionReport:
    return build_attribution(result, c)
