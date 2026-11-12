"""时序仿真：曲线管理 + 场景组装 + 逐时段潮流 + 结果集。

模块职责边界（刻意划清，避免 I3 时被"顺手改一下"污染）：

- `profiles.load_profile()`   只负责读 CSV 并校验，不碰电网
- `assemble_scenario()`       只负责把「算例 + 曲线 + 光伏接入配置」变成可执行方案
- `run_timeseries()`          只负责逐时段执行潮流、收集矩阵，**不做越限判定**（那是 constraints 的事）
- `write_result_set()`        只负责落盘，不参与计算

**性能设计**：
不逐时段调用 `net.deepcopy()`（96 次深拷贝会成为瓶颈），而是在同一张 net 上
逐时段改 `load.p_mw` / `sgen.p_mw` 的**列**（整列赋值，pandas 层面一次完成），
再跑 `runpp`。这样每时段开销 ≈ 一次潮流本身。

**热启动**：第一时段 `init="auto"`，之后 `init="results"` 复用上一时段解。
不收敛时自动回退 `init="flat"` 重试一次，并记录回退次数。
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import pandapower as pp
import pandas as pd

from gp_hosting import powerflow

# --------------------------------------------------------------------------
# 曲线
# --------------------------------------------------------------------------

STEP_MINUTES = 15
POINTS_PER_DAY = 96


class ProfileError(ValueError):
    def __init__(self, message: str, detail: dict | None = None):
        self.detail = detail or {}
        super().__init__(message)


@dataclass
class Profile:
    """归一化曲线。factor ∈ [0,1]，乘以基准值得到实际值。"""

    name: str
    kind: Literal["load", "pv"]
    factors: list[float]

    def __len__(self) -> int:
        return len(self.factors)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "n_points": len(self.factors),
            "step_minutes": STEP_MINUTES,
            "min": min(self.factors) if self.factors else None,
            "max": max(self.factors) if self.factors else None,
            "factors": self.factors,
        }


def load_profile(path: str | Path, kind: Literal["load", "pv"], name: str | None = None) -> Profile:
    """从 CSV 读取曲线。CSV 必须含 `time_index`（0..N-1 连续）与 `factor` 两列。

    校验是刻意严格的：曲线是后续所有时序结论的输入，静默接受坏数据
    会让"承载力较低"这种结论变得无法证伪。
    """
    p = Path(path)
    if not p.exists():
        raise ProfileError(f"曲线文件不存在: {p}", {"path": str(p)})

    with p.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        raise ProfileError(f"曲线文件为空: {p}", {"path": str(p)})
    if "time_index" not in rows[0] or "factor" not in rows[0]:
        raise ProfileError(
            f"曲线文件缺少必需列 time_index/factor: {p}",
            {"path": str(p), "columns": list(rows[0].keys())},
        )

    factors: list[float] = []
    for i, row in enumerate(rows):
        try:
            idx = int(row["time_index"])
            val = float(row["factor"])
        except (TypeError, ValueError) as exc:
            raise ProfileError(
                f"曲线第 {i} 行无法解析: {exc}", {"path": str(p), "time_index": i}
            ) from exc
        if idx != i:
            raise ProfileError(
                f"time_index 必须从 0 连续递增，第 {i} 行是 {idx}",
                {"path": str(p), "time_index": i},
            )
        factors.append(val)

    return validate_profile(
        Profile(name=name or p.stem, kind=kind, factors=factors),
        source=str(p),
    )


def validate_profile(profile: Profile, source: str = "<memory>") -> Profile:
    """曲线合法性校验。

    - 点数必须在 12..288 之间（1 小时 ~ 5 分钟粒度），且能被 24 整除时可映射到小时
    - load：factor 必须 > 0。**0 负荷视为数据缺失**，不是合法的"全网停电"
    - pv：factor 必须 >= 0（夜间为 0 是物理事实）
    - 两者都要求最大值 > 0
    """
    n = len(profile.factors)
    if not (12 <= n <= 288):
        raise ProfileError(
            f"曲线点数 {n} 超出合理范围 [12, 288]", {"path": source, "n_points": n}
        )
    if 24 * 60 % n != 0:
        raise ProfileError(
            f"曲线点数 {n} 无法均匀映射到 24 小时（24*60 需能被 n 整除）",
            {"path": source, "n_points": n},
        )

    for i, v in enumerate(profile.factors):
        if not np.isfinite(v):
            raise ProfileError(
                f"曲线第 {i} 点非有限数", {"path": source, "time_index": i, "value": str(v)}
            )
        if profile.kind == "load" and v <= 0:
            raise ProfileError(
                f"负荷曲线第 {i} 点（time_index={i}, 对应 {_hhmm(i, n)}）为 {v}，"
                f"负荷系数必须 > 0；若这是数据缺失请先补齐",
                {"path": source, "time_index": i, "field": "factor", "value": v},
            )
        if profile.kind == "pv" and v < 0:
            raise ProfileError(
                f"光伏曲线第 {i} 点（time_index={i}）为 {v}，不允许负出力",
                {"path": source, "time_index": i, "field": "factor", "value": v},
            )

    if max(profile.factors) <= 0:
        raise ProfileError(f"曲线最大值为 {max(profile.factors)}，全零曲线无意义", {"path": source})
    return profile


def _hhmm(index: int, n_points: int) -> str:
    minutes = index * (24 * 60 // n_points)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# --------------------------------------------------------------------------
# 场景
# --------------------------------------------------------------------------


@dataclass
class SgenInjection:
    """一个光伏接入点。"""

    bus: int
    capacity_mw: float = 0.0          # 装机容量（MW），p(t) = capacity_mw * pv_factor(t)
    power_factor: float = 1.0          # 恒定功率因数；1.0 表示不吞吐无功
    name: str = ""

    def __post_init__(self) -> None:
        if self.capacity_mw < 0:
            raise ProfileError(
                f"光伏容量不能为负: {self.capacity_mw}", {"field": "capacity_mw", "bus": self.bus}
            )
        if not (0.0 < self.power_factor <= 1.0):
            raise ProfileError(
                f"功率因数必须在 (0, 1] 内: {self.power_factor}",
                {"field": "power_factor", "bus": self.bus},
            )


@dataclass
class ScenarioSpec:
    """场景 = 算例 + 负荷曲线 + 光伏曲线 + 光伏接入配置。

    同一算例可组合出 N 个场景，这是"承载力批量评估"的前提。
    """

    case_path: str
    load_profile_path: str
    pv_profile_path: str | None = None
    sgens: list[SgenInjection] = field(default_factory=list)
    load_scale: float = 1.0            # 负荷整体缩放（用于压力测试）
    name: str = ""

    def to_dict(self) -> dict:
        return {
            "case_path": self.case_path,
            "load_profile_path": self.load_profile_path,
            "pv_profile_path": self.pv_profile_path,
            "load_scale": self.load_scale,
            "name": self.name,
            "sgens": [asdict(s) for s in self.sgens],
        }


@dataclass
class Scenario:
    """已加载、可执行的场景。"""

    spec: ScenarioSpec
    case_spec: object                     # CaseSpec，避免循环 import 用 object 标注
    load_profile: Profile
    pv_profile: Profile | None
    n_steps: int
    load_base_mw: np.ndarray              # shape (n_load,) 各负荷基准 p_mw
    load_base_mvar: np.ndarray            # shape (n_load,)
    gen_load_index: np.ndarray            # 新增 sgen 在 net.sgen 中的位置（按顺序追加）
    warnings: list[str] = field(default_factory=list)

    def hourly_summary(self) -> dict:
        return {
            "n_steps": self.n_steps,
            "step_minutes": STEP_MINUTES,
            "load_peak_mw": float(self.load_base_mw.sum() * max(self.load_profile.factors)),
            "load_trough_mw": float(self.load_base_mw.sum() * min(self.load_profile.factors)),
            "pv_installed_mw": float(sum(s.capacity_mw for s in self.spec.sgens)),
        }


def assemble_scenario(spec: ScenarioSpec, strict: bool = True) -> Scenario:
    """把一个 ScenarioSpec 变成可直接跑时序的 Scenario。

    这里做"组合层面的校验"：曲线长度一致、光伏节点存在、算例本身合法。
    光伏节点的存在性必须在**这里**拦住——等到 runpp 才发现会变成难懂的 KeyError。
    """
    from gp_hosting.case_loader import load_case_file

    case_spec = load_case_file(spec.case_path)
    bus_ids = {b.index for b in case_spec.buses}

    load_prof = load_profile(spec.load_profile_path, kind="load")
    pv_prof = None
    if spec.pv_profile_path:
        pv_prof = load_profile(spec.pv_profile_path, kind="pv")
    elif spec.sgens:
        raise ProfileError(
            "配置了光伏接入点但没有提供光伏曲线",
            {"field": "pv_profile_path", "n_sgens": len(spec.sgens)},
        )

    if pv_prof is not None and len(pv_prof) != len(load_prof):
        raise ProfileError(
            f"负荷曲线 {len(load_prof)} 点与光伏曲线 {len(pv_prof)} 点长度不一致",
            {"n_load_points": len(load_prof), "n_pv_points": len(pv_prof)},
        )

    for s in spec.sgens:
        if s.bus not in bus_ids:
            raise ProfileError(
                f"光伏接入节点 {s.bus} 不存在于算例中",
                {"field": "sgens.bus", "bus": s.bus, "valid_buses": sorted(bus_ids)[:10]},
            )

    if spec.load_scale <= 0:
        raise ProfileError(f"负荷缩放系数必须 > 0: {spec.load_scale}", {"field": "load_scale"})

    load_base_p = np.array([ld.p_mw for ld in case_spec.loads], dtype=float)
    load_base_q = np.array([ld.q_mvar for ld in case_spec.loads], dtype=float)

    warnings: list[str] = []
    total_pv = sum(s.capacity_mw for s in spec.sgens)
    base_peak = float(load_base_p.sum() * max(load_prof.factors) * spec.load_scale)
    if total_pv > 3 * base_peak and total_pv > 0:
        warnings.append(
            f"光伏装机 {total_pv:.2f} MW 超过负荷峰值 {base_peak:.2f} MW 的 3 倍，"
            "预计会出现显著倒送与过电压"
        )

    return Scenario(
        spec=spec,
        case_spec=case_spec,
        load_profile=load_prof,
        pv_profile=pv_prof,
        n_steps=len(load_prof),
        load_base_mw=load_base_p,
        load_base_mvar=load_base_q,
        gen_load_index=np.arange(len(spec.sgens), dtype=int),
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# 结果集
# --------------------------------------------------------------------------


@dataclass
class TimeseriesSummary:
    n_steps: int
    step_minutes: int
    n_converged: int
    failed_steps: list[int]
    min_vm_pu: float
    min_vm_bus: int
    min_vm_step: int
    min_vm_time: str
    max_vm_pu: float
    max_vm_bus: int
    max_vm_step: int
    max_vm_time: str
    max_line_loading_percent: float
    max_line_index: int
    max_line_step: int
    max_line_time: str
    max_trafo_loading_percent: float
    max_trafo_index: int | None
    energy_loss_mwh: float
    energy_pv_mwh: float
    energy_load_mwh: float
    elapsed_ms: float
    warmstart_fallbacks: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TimeseriesResult:
    summary: TimeseriesSummary
    vm_pu: np.ndarray            # (n_steps, n_bus)
    line_loading: np.ndarray     # (n_steps, n_line)
    trafo_loading: np.ndarray    # (n_steps, n_trafo)
    pl_mw: np.ndarray            # (n_steps,)
    pv_mw: np.ndarray            # (n_steps,)
    bus_index: list[int]
    line_index: list[int]
    trafo_index: list[int]
    steps: list[int]
    converged: np.ndarray        # (n_steps,) bool


def _time_str(step: int, n_steps: int) -> str:
    minutes = step * (24 * 60 // n_steps)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def run_timeseries(scenario: Scenario, keep_solver_results: bool = False) -> TimeseriesResult:
    """逐时段执行潮流，返回矩阵结果。不做越限判定。

    实现要点：
    - 整列赋值 load.p_mw = base * factor，避免逐行 setvalue
    - sgen 在 build 阶段一次性建好（容量为 0），每时段只改 p_mw，避免反复 create
    - 热启动失败自动回退 flat 重试；仍失败则记入 failed_steps 并继续（不中断整天）
    """
    import time

    from gp_hosting.case_loader import build_net

    t_start = time.perf_counter()
    net = build_net(scenario.case_spec)

    # 光伏设备一次性创建，之后只改出力
    for s in scenario.spec.sgens:
        pp.create_sgen(
            net,
            bus=s.bus,
            p_mw=0.0,
            q_mvar=0.0,
            name=s.name or f"PV@{s.bus}",
            type="wye",
        )

    n_steps = scenario.n_steps
    n_bus, n_line, n_trafo = len(net.bus), len(net.line), len(net.trafo)
    n_load = len(net.load)

    vm = np.full((n_steps, n_bus), np.nan)
    line_loading = np.full((n_steps, n_line), np.nan)
    trafo_loading = np.full((n_steps, n_trafo), np.nan)
    pl = np.full(n_steps, np.nan)
    pv_energy = np.full(n_steps, np.nan)
    converged = np.zeros(n_steps, dtype=bool)
    failed: list[int] = []
    fallbacks = 0

    load_factors = np.asarray(scenario.load_profile.factors, dtype=float)
    pv_factors = (
        np.asarray(scenario.pv_profile.factors, dtype=float)
        if scenario.pv_profile is not None
        else np.zeros(n_steps)
    )
    # 各光伏接入点的功率因数 -> 无功比例
    pf_sin = np.array(
        [np.sqrt(max(0.0, 1.0 - s.power_factor**2)) for s in scenario.spec.sgens], dtype=float
    )
    capacities = np.array([s.capacity_mw for s in scenario.spec.sgens], dtype=float)

    for t in range(n_steps):
        # 负荷：整列赋值
        net.load["p_mw"] = scenario.load_base_mw * load_factors[t] * scenario.spec.load_scale
        net.load["q_mvar"] = scenario.load_base_mvar * load_factors[t] * scenario.spec.load_scale

        # 光伏：p(t) = 装机 × 曲线系数；q(t) = -p(t) × tan(acos(pf))
        # 符号约定：sgen 的 q_mvar 为正 = 注入无功（抬电压）。
        # 光伏若按 pf<1 运行，通常处于"吸收无功"状态，因此取负号。
        if len(capacities):
            p_t = capacities * pv_factors[t]
            net.sgen["p_mw"] = p_t
            net.sgen["q_mvar"] = -(p_t * pf_sin)

        opts = powerflow.PowerFlowOptions(
            init="auto" if t == 0 else "results",
            max_iteration=30,
        )
        result = powerflow.run(net, opts)

        if not result.converged and t > 0:
            # 热启动可能因工况突变失败，回退 flat 再试一次
            fallbacks += 1
            result = powerflow.run(net, powerflow.PowerFlowOptions(init="flat", max_iteration=50))

        if not result.converged:
            failed.append(t)
            continue

        converged[t] = True
        vm[t, :] = [b.vm_pu for b in result.bus]
        line_loading[t, :] = [ln.loading_percent for ln in result.line]
        if n_trafo:
            trafo_loading[t, :] = [tr.loading_percent for tr in result.trafo]
        assert result.summary is not None
        pl[t] = result.summary.total_p_loss_mw
        pv_energy[t] = result.summary.total_sgen_mw

    elapsed_ms = (time.perf_counter() - t_start) * 1000

    # ---- 摘要 ----
    hours_per_step = 24.0 / n_steps
    ok = converged
    if ok.any():
        vm_ok = np.where(ok[:, None], vm, np.nan)
        min_flat = np.nanmin(vm_ok)
        min_t, min_b = np.unravel_index(np.nanargmin(vm_ok), vm_ok.shape)
        max_flat = np.nanmax(vm_ok)
        max_t, max_b = np.unravel_index(np.nanargmax(vm_ok), vm_ok.shape)

        ll_ok = np.where(ok[:, None], line_loading, np.nan) if n_line else None
        if ll_ok is not None and np.isfinite(ll_ok).any():
            max_ll = float(np.nanmax(ll_ok))
            ll_t, ll_i = np.unravel_index(np.nanargmax(ll_ok), ll_ok.shape)
        else:
            max_ll, ll_t, ll_i = 0.0, 0, 0

        if n_trafo:
            tl_ok = np.where(ok[:, None], trafo_loading, np.nan)
            if np.isfinite(tl_ok).any():
                max_tl = float(np.nanmax(tl_ok))
                tl_t, tl_i = np.unravel_index(np.nanargmax(tl_ok), tl_ok.shape)
            else:
                max_tl, tl_t, tl_i = 0.0, 0, 0
        else:
            max_tl, tl_t, tl_i = 0.0, 0, 0
    else:
        min_flat = max_flat = max_ll = max_tl = float("nan")
        min_t = min_b = max_t = max_b = ll_t = ll_i = tl_t = tl_i = 0

    energy_loss = float(np.nansum(pl) * hours_per_step) if np.isfinite(pl).any() else 0.0
    energy_pv = float(np.nansum(pv_energy) * hours_per_step) if np.isfinite(pv_energy).any() else 0.0
    energy_load = float(
        scenario.load_base_mw.sum() * float(np.nansum(load_factors)) * hours_per_step
        * scenario.spec.load_scale
    )

    summary = TimeseriesSummary(
        n_steps=n_steps,
        step_minutes=STEP_MINUTES,
        n_converged=int(converged.sum()),
        failed_steps=failed,
        min_vm_pu=float(min_flat),
        min_vm_bus=int(net.bus.index[min_b]) if len(net.bus) else -1,
        min_vm_step=int(min_t),
        min_vm_time=_time_str(int(min_t), n_steps),
        max_vm_pu=float(max_flat),
        max_vm_bus=int(net.bus.index[max_b]) if len(net.bus) else -1,
        max_vm_step=int(max_t),
        max_vm_time=_time_str(int(max_t), n_steps),
        max_line_loading_percent=float(max_ll),
        max_line_index=int(net.line.index[ll_i]) if n_line else -1,
        max_line_step=int(ll_t),
        max_line_time=_time_str(int(ll_t), n_steps),
        max_trafo_loading_percent=float(max_tl),
        max_trafo_index=int(net.trafo.index[tl_i]) if n_trafo else None,
        energy_loss_mwh=energy_loss,
        energy_pv_mwh=energy_pv,
        energy_load_mwh=energy_load,
        elapsed_ms=elapsed_ms,
        warmstart_fallbacks=fallbacks,
    )

    return TimeseriesResult(
        summary=summary,
        vm_pu=vm,
        line_loading=line_loading,
        trafo_loading=trafo_loading,
        pl_mw=pl,
        pv_mw=pv_energy,
        bus_index=[int(i) for i in net.bus.index],
        line_index=[int(i) for i in net.line.index],
        trafo_index=[int(i) for i in net.trafo.index],
        steps=list(range(n_steps)),
        converged=converged,
    )


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------


def write_result_set(
    result: TimeseriesResult,
    scenario: Scenario,
    root: str | Path,
    task_id: str | None = None,
) -> Path:
    """结果落 Parquet，按 taskId 分目录。

    目录结构：
        {root}/{task_id}/
            meta.json            维度、列索引、场景参数
            summary.json         摘要指标
            vm_pu.parquet        (n_steps, n_bus)
            line_loading.parquet (n_steps, n_line)
            trafo_loading.parquet(n_steps, n_trafo)  # 无变压器时不生成，见 meta.empty_matrices
            timeseries.parquet   (n_steps,) pl_mw / pv_mw
            status.parquet       (n_steps,) 是否收敛

    `task_id` 由调用方提供时复用（API 侧已在任务表里生成了 ID），
    不提供则自动生成 —— 两者必须一致，否则会出现 task 与目录对不上。
    """
    import json
    import uuid

    root_path = Path(root)
    task_id = task_id or uuid.uuid4().hex[:16]
    out_dir = root_path / task_id
    out_dir.mkdir(parents=True, exist_ok=True)

    idx = pd.RangeIndex(result.summary.n_steps, name="step")

    def _write_matrix(arr, columns, filename: str) -> str | None:
        """写矩阵文件，返回实际写出的文件名；零列时不写（Parquet 无法表达"96 行 × 0 列"）。

        Parquet 是列式格式，没有任何列时行数不可表达，读回来必然是 (0,0)。
        与其塞一个占位列污染数据，不如不写文件，并在 meta.json 里声明该矩阵为空。
        """
        cols = [str(c) for c in columns]
        if not cols:
            return None
        # 列名用字符串，保证 Parquet 读回来类型稳定（整数列名会被某些 reader 改写）
        pd.DataFrame(arr, index=idx, columns=cols).to_parquet(out_dir / filename)
        return filename

    written: dict[str, str | None] = {
        "vm_pu": _write_matrix(result.vm_pu, result.bus_index, "vm_pu.parquet"),
        "line_loading": _write_matrix(result.line_loading, result.line_index, "line_loading.parquet"),
        "trafo_loading": _write_matrix(result.trafo_loading, result.trafo_index, "trafo_loading.parquet"),
    }
    pd.DataFrame({"pl_mw": result.pl_mw, "pv_mw": result.pv_mw}, index=idx).to_parquet(
        out_dir / "timeseries.parquet"
    )
    pd.DataFrame({"converged": result.converged}, index=idx).to_parquet(out_dir / "status.parquet")

    meta = {
        "task_id": task_id,
        "case": scenario.case_spec.model_dump()["meta"],
        "scenario": scenario.spec.to_dict(),
        "dims": {
            "n_steps": result.summary.n_steps,
            "n_bus": len(result.bus_index),
            "n_line": len(result.line_index),
            "n_trafo": len(result.trafo_index),
        },
        "files": {
            "vm_pu": written["vm_pu"],
            "line_loading": written["line_loading"],
            "trafo_loading": written["trafo_loading"],
            "timeseries": "timeseries.parquet",
            "status": "status.parquet",
        },
        "empty_matrices": [k for k, v in written.items() if v is None],
        "step_minutes": STEP_MINUTES,
        "bus_index": result.bus_index,
        "line_index": result.line_index,
        "trafo_index": result.trafo_index,
        "columns": {
            "vm_pu": "节点电压幅值 p.u.，行=时段，列=bus index",
            "line_loading": "线路负载率 %，行=时段，列=line index",
            "trafo_loading": "变压器负载率 %，行=时段，列=trafo index",
            "timeseries": "pl_mw 全网有功网损 MW；pv_mw 光伏总有功注入 MW",
            "status": "converged 该时段潮流是否收敛",
        },
        "units": {"vm_pu": "p.u.", "line_loading": "percent", "pl_mw": "MW", "pv_mw": "MW"},
        "warnings": scenario.warnings,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps(result.summary.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out_dir


def read_result_set(path: str | Path) -> dict:
    """独立读取一份结果集（用于校验脚本与后续 Java 侧取数）。

    空矩阵（如无变压器）按 meta.empty_matrices 声明，返回形状为 (n_steps, 0) 的
    DataFrame，保持"行数恒等于时段数"这一不变式，调用方无需特判。
    """
    import json

    p = Path(path)
    meta = json.loads((p / "meta.json").read_text(encoding="utf-8"))
    summary = json.loads((p / "summary.json").read_text(encoding="utf-8"))
    n_steps = meta["dims"]["n_steps"]
    empty = set(meta.get("empty_matrices", []))
    idx = pd.RangeIndex(n_steps, name="step")

    def _read_matrix(key: str, fallback_columns: list[str]) -> pd.DataFrame:
        if key in empty:
            return pd.DataFrame(index=idx, columns=pd.Index([], dtype=object))
        return pd.read_parquet(p / f"{key}.parquet")

    return {
        "meta": meta,
        "summary": summary,
        "vm_pu": _read_matrix("vm_pu", meta["bus_index"]),
        "line_loading": _read_matrix("line_loading", meta["line_index"]),
        "trafo_loading": _read_matrix("trafo_loading", meta["trafo_index"]),
        "timeseries": pd.read_parquet(p / "timeseries.parquet"),
        "status": pd.read_parquet(p / "status.parquet"),
    }
