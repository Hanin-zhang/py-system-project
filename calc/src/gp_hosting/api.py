"""FastAPI 计算服务。

接口分层：
- `/api/powerflow/*`   单断面潮流（I1）
- `/api/timeseries/*`  时序仿真（I2）
- `/api/hc/*`          承载力评估（I3）
- `/api/case/*`        算例校验

约定：
- 所有响应统一为 `{code, message, data}`，`code=0` 表示成功。
  这样 Java 侧 DTO 可以统一处理，无需按接口区分。
- 参数错误返回 **HTTP 200 + code!=0**，只在真正的服务端故障才用 5xx。
  目的是让前端/Java 侧拿到字段级错误信息，而不是一个被网关吞掉的 500。
- 结果字段 snake_case，与算出结构一一对应。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gp_hosting import hc_solver, powerflow, timeseries
from gp_hosting.case_loader import (
    CaseValidationError,
    load_case_dict,
    build_net,
)
from gp_hosting.constraints import CONSTRAINT_PRESETS, Constraints, check, constraints_from_preset
from gp_hosting.hc_store import HcStore
from gp_hosting.tasks import registry

logger = logging.getLogger("gp_hosting.api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(
    title="配电网光伏承载力平台 · 计算服务",
    version="0.3.0",
    description="基于 pandapower 的潮流、时序仿真与承载力评估。全部算例基于公开标准算例与模拟数据。",
)

# 结果落盘根目录。可用环境变量覆盖（容器化时挂卷到宿主机）。
RESULT_ROOT = Path(os.environ.get("GP_RESULT_ROOT", Path(__file__).resolve().parent.parent.parent / "data" / "results"))

DEFAULT_CASE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cases"
DEFAULT_PROFILE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "profiles"


# --------------------------------------------------------------------------
# 请求 / 响应模型
# --------------------------------------------------------------------------


class PowerFlowRequest(BaseModel):
    case: dict = Field(description="算例 JSON，结构见 docs/data-dictionary.md")
    algorithm: str = Field(default="nr", description="潮流算法：nr / iw / bfsw / gs")
    max_iteration: int = Field(default=30, ge=1, le=200)
    calculate_voltage_angles: bool = Field(default=False)
    constraints: Constraints = Field(default_factory=Constraints, description="越限判定阈值")


class ResultEnvelope(BaseModel):
    code: int = 0
    message: str = "ok"
    data: dict | None = None


# --------------------------------------------------------------------------
# 异常处理
# --------------------------------------------------------------------------


@app.exception_handler(CaseValidationError)
async def _case_validation_handler(_: Request, exc: CaseValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={
            "code": 40001,
            "message": "算例校验失败",
            "data": {"issues": [i.model_dump() for i in exc.issues]},
        },
    )


@app.exception_handler(timeseries.ProfileError)
async def _profile_error_handler(_: Request, exc: timeseries.ProfileError) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={
            "code": 40002,
            "message": str(exc),
            "data": {"detail": exc.detail},
        },
    )


@app.exception_handler(Exception)
async def _unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"code": 50000, "message": "计算服务内部错误", "data": {"exception": type(exc).__name__}},
    )


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------


@app.get("/health", summary="健康检查")
def health() -> dict:
    return {"status": "ok", "service": "gp-hosting-calc", "version": app.version}


@app.post("/api/powerflow/run", response_model=ResultEnvelope, summary="执行一次交流潮流")
def run_powerflow(req: PowerFlowRequest) -> ResultEnvelope:
    spec = load_case_dict(req.case)
    net = build_net(spec)

    result = powerflow.run(
        net,
        powerflow.PowerFlowOptions(
            algorithm=req.algorithm,
            max_iteration=req.max_iteration,
            calculate_voltage_angles=req.calculate_voltage_angles,
        ),
    )

    if not result.converged:
        assert result.error is not None
        return ResultEnvelope(code=42001, message=result.error.message, data=result.to_dict())

    violations = check(result, req.constraints)
    data = result.to_dict()
    data["violations"] = violations.to_dict()
    data["input"] = {
        "algorithm": req.algorithm,
        "max_iteration": req.max_iteration,
        "calculate_voltage_angles": req.calculate_voltage_angles,
        "constraints": req.constraints.to_dict(),
    }
    return ResultEnvelope(data=data)


@app.post("/api/case/validate", summary="仅校验算例，不执行计算")
def validate_case_endpoint(req: PowerFlowRequest) -> ResultEnvelope:
    spec = load_case_dict(req.case)
    return ResultEnvelope(
        data={
            "valid": True,
            "meta": spec.model_dump()["meta"],
            "counts": {
                "buses": len(spec.buses),
                "lines": len(spec.lines),
                "trafos": len(spec.trafos),
                "loads": len(spec.loads),
                "sgens": len(spec.sgens),
            },
        }
    )


# --------------------------------------------------------------------------
# 时序仿真（I2）
# --------------------------------------------------------------------------


class SgenRequest(BaseModel):
    bus: int = Field(description="光伏接入节点")
    capacity_mw: float = Field(ge=0, description="装机容量 MW")
    power_factor: float = Field(default=1.0, gt=0, le=1.0, description="恒定功率因数")
    name: str = ""


class TimeseriesRequest(BaseModel):
    case: dict | None = Field(
        default=None, description="算例 JSON；与 case_ref 二选一"
    )
    case_ref: str | None = Field(
        default=None,
        description="内置算例名，如 ieee33 / ieee33_ampacity / cigre_mv；提供时忽略 case",
    )
    load_profile_path: str | None = Field(default=None, description="负荷曲线 CSV 路径")
    pv_profile_path: str | None = Field(default=None, description="光伏曲线 CSV 路径")
    sgens: list[SgenRequest] = Field(default_factory=list, description="光伏接入点列表")
    load_scale: float = Field(default=1.0, gt=0, le=10, description="负荷整体缩放")
    persist: bool = Field(default=True, description="是否把结果落盘")
    name: str = ""


def _resolve_case(req: TimeseriesRequest) -> dict:
    if req.case_ref:
        path = DEFAULT_CASE_DIR / f"{req.case_ref}.json"
        if not path.exists():
            available = sorted(p.stem for p in DEFAULT_CASE_DIR.glob("*.json"))
            raise timeseries.ProfileError(
                f"内置算例 {req.case_ref!r} 不存在",
                {"field": "case_ref", "available": available},
            )
        return json.loads(path.read_text(encoding="utf-8"))
    if not req.case:
        raise timeseries.ProfileError(
            "必须提供 case 或 case_ref", {"field": "case"}
        )
    return req.case


def _build_scenario_from_request(req: TimeseriesRequest) -> timeseries.Scenario:
    case_dict = _resolve_case(req)
    case_path = _stage_case(case_dict)

    load_path = req.load_profile_path or str(DEFAULT_PROFILE_DIR / "load_typical_day.csv")
    pv_path = req.pv_profile_path
    if pv_path is None and req.sgens:
        pv_path = str(DEFAULT_PROFILE_DIR / "pv_typical_day.csv")

    spec = timeseries.ScenarioSpec(
        case_path=str(case_path),
        load_profile_path=load_path,
        pv_profile_path=pv_path,
        sgens=[
            timeseries.SgenInjection(
                bus=s.bus,
                capacity_mw=s.capacity_mw,
                power_factor=s.power_factor,
                name=s.name,
            )
            for s in req.sgens
        ],
        load_scale=req.load_scale,
        name=req.name,
    )
    return timeseries.assemble_scenario(spec)


def _stage_case(case_dict: dict) -> Path:
    """把请求里的算例临时落盘到结果目录，供 assemble_scenario 统一走文件加载路径。

    这样做的好处：算例的校验逻辑只有一条路径（load_case_file），
    不会出现"请求内算例"和"文件算例"两套校验行为不一致的情况。
    """
    import tempfile

    staged_dir = RESULT_ROOT / "_staged"
    staged_dir.mkdir(parents=True, exist_ok=True)
    # 用内容哈希做文件名，天然去重、不会无限增长（同算例复用同一文件）
    import hashlib

    digest = hashlib.sha256(json.dumps(case_dict, sort_keys=True).encode()).hexdigest()[:16]
    path = staged_dir / f"{digest}.json"
    if not path.exists():
        path.write_text(json.dumps(case_dict, ensure_ascii=False), encoding="utf-8")
    return path


@app.post("/api/timeseries/run", response_model=ResultEnvelope, summary="执行一天时序潮流仿真")
def run_timeseries_endpoint(req: TimeseriesRequest) -> ResultEnvelope:
    """同步执行一次 96 时段仿真（当前实测 < 1 s）。

    I6 之后由 Java 侧接管异步编排；计算服务本身保持同步语义，
    这样"同步调用 + 任务表"两种模式都能支持，不把自己锁死。
    """
    task = registry.create(
        {"case_ref": req.case_ref, "sgens": [s.model_dump() for s in req.sgens], "name": req.name}
    )
    try:
        scenario = _build_scenario_from_request(req)
        result = timeseries.run_timeseries(scenario)

        if result.summary.n_converged < result.summary.n_steps:
            registry.fail(
                task.task_id,
                code="42002",
                message=f"{result.summary.n_steps - result.summary.n_converged} 个时段潮流不收敛",
                detail={"failed_steps": result.summary.failed_steps},
            )
            return ResultEnvelope(
                code=42002,
                message=registry.get(task.task_id).error_message or "",
                data={"task": registry.get(task.task_id).to_dict(), "summary": result.summary.to_dict()},
            )

        result_dir = ""
        if req.persist:
            result_dir = str(
                timeseries.write_result_set(
                    result, scenario, RESULT_ROOT, task_id=task.task_id
                )
            )

        registry.succeed(task.task_id, result_dir=result_dir, summary=result.summary.to_dict())
        record = registry.get(task.task_id)
        return ResultEnvelope(
            data={
                "task": record.to_dict() if record else None,
                "summary": result.summary.to_dict(),
                "scenario": scenario.hourly_summary(),
                "warnings": scenario.warnings,
            }
        )
    except timeseries.ProfileError:
        registry.fail(task.task_id, code="40002", message="场景校验失败")
        raise


@app.get("/api/timeseries/{task_id}", response_model=ResultEnvelope, summary="查询时序任务状态与摘要")
def get_timeseries_task(task_id: str) -> ResultEnvelope:
    record = registry.get(task_id)
    if record is None:
        return ResultEnvelope(code=40404, message=f"任务 {task_id} 不存在", data=None)
    return ResultEnvelope(data=record.to_dict())


@app.get(
    "/api/timeseries/{task_id}/matrix",
    response_model=ResultEnvelope,
    summary="取时序矩阵切片（节点电压 / 线路负载率）",
)
def get_timeseries_matrix(
    task_id: str,
    type: str = "vm_pu",
    bus: int | None = None,
    line: int | None = None,
    trafo: int | None = None,
    step_from: int | None = None,
    step_to: int | None = None,
) -> ResultEnvelope:
    """返回指定矩阵的切片。

    - 不给 element 参数 -> 返回全矩阵（小算例可接受；大算例请指定元素）
    - 给 bus/line/trafo -> 返回该元素的时间序列（96 点）
    """
    record = registry.get(task_id)
    if record is None or record.status.value != "SUCCEEDED" or not record.result_dir:
        return ResultEnvelope(code=40404, message=f"任务 {task_id} 无可用结果", data=None)

    data = timeseries.read_result_set(record.result_dir)
    key_map = {"vm_pu": "vm_pu", "line_loading": "line_loading", "trafo_loading": "trafo_loading"}
    if type not in key_map:
        return ResultEnvelope(
            code=40003,
            message=f"不支持的矩阵类型 {type!r}",
            data={"available": list(key_map)},
        )

    df = data[key_map[type]]
    if step_from is not None or step_to is not None:
        df = df.iloc[step_from:step_to]

    element_col = {"vm_pu": bus, "line_loading": line, "trafo_loading": trafo}[type]
    if element_col is not None:
        col = str(element_col)
        if col not in df.columns:
            return ResultEnvelope(
                code=40003,
                message=f"{type} 中不存在元素 {element_col}",
                data={"available_elements": [int(c) for c in df.columns]},
            )
        series = df[col]
        return ResultEnvelope(
            data={
                "type": type,
                "element": element_col,
                "steps": [int(i) for i in series.index],
                "values": [float(v) for v in series.values],
                "unit": "p.u." if type == "vm_pu" else "%",
            }
        )

    return ResultEnvelope(
        data={
            "type": type,
            "elements": [int(c) for c in df.columns],
            "steps": [int(i) for i in df.index],
            "values": [[None if v != v else float(v) for v in row] for row in df.values],
        }
    )


@app.get("/api/timeseries", response_model=ResultEnvelope, summary="列出最近的时序任务")
def list_timeseries_tasks(limit: int = 20) -> ResultEnvelope:
    return ResultEnvelope(data={"tasks": [r.to_dict() for r in registry.list(limit)]})



# --------------------------------------------------------------------------
# 承载力评估（I3）
# --------------------------------------------------------------------------

DEFAULT_HC_LOAD_PROFILE = "simbench_pv_max_day_20160513_load.csv"
DEFAULT_HC_PV_PROFILE = "simbench_pv_max_day_20160513_pv.csv"

_hc_store: HcStore | None = None


def get_hc_store() -> HcStore:
    """惰性创建，且跟随 RESULT_ROOT（测试里会 monkeypatch 它）。"""
    global _hc_store
    root = RESULT_ROOT / "hc"
    if _hc_store is None or _hc_store.root != root:
        _hc_store = HcStore(root)
    return _hc_store


class ConstraintsRequest(BaseModel):
    preset: str = Field(default="gbt_12325", description=f"约束预设：{', '.join(CONSTRAINT_PRESETS)}")
    vm_min_pu: float | None = Field(default=None, gt=0, lt=2, description="覆盖电压下限")
    vm_max_pu: float | None = Field(default=None, gt=0, lt=2, description="覆盖电压上限")
    line_loading_percent_max: float | None = Field(default=None, gt=0, description="覆盖线路负载率上限 %")
    trafo_loading_percent_max: float | None = Field(default=None, gt=0, description="覆盖变压器负载率上限 %")

    def build(self) -> Constraints:
        try:
            return constraints_from_preset(
                self.preset,
                vm_min_pu=self.vm_min_pu,
                vm_max_pu=self.vm_max_pu,
                line_loading_percent_max=self.line_loading_percent_max,
                trafo_loading_percent_max=self.trafo_loading_percent_max,
            )
        except ValueError as exc:
            raise timeseries.ProfileError(str(exc), {"field": "constraints"}) from exc


class HcOptionsRequest(BaseModel):
    tol_mw: float = Field(default=0.010, gt=0, description="二分精度 MW（默认 10 kW）")
    max_capacity_mw: float = Field(default=50.0, gt=0, le=500, description="搜索上限 MW")
    coarse_points: int = Field(default=10, ge=3, le=50, description="单调性粗扫点数")
    probe_mw: float = Field(default=0.050, gt=0, description="双向探针偏移 MW（默认 50 kW）")

    def build(self) -> hc_solver.HcOptions:
        try:
            return hc_solver.HcOptions(
                tol_mw=self.tol_mw,
                max_capacity_mw=self.max_capacity_mw,
                initial_mw=min(0.5, self.max_capacity_mw),
                coarse_points=self.coarse_points,
                probe_mw=self.probe_mw,
            )
        except ValueError as exc:
            raise timeseries.ProfileError(str(exc), {"field": "options"}) from exc


class HcBaseRequest(BaseModel):
    case: dict | None = Field(default=None, description="算例 JSON；与 case_ref 二选一")
    case_ref: str | None = Field(
        default=None, description="内置算例名；case 与 case_ref 都不给时用 ieee33_ampacity"
    )
    load_profile_path: str | None = Field(
        default=None, description=f"负荷曲线 CSV；默认 SimBench 光伏最大日 {DEFAULT_HC_LOAD_PROFILE}"
    )
    pv_profile_path: str | None = Field(default=None, description=f"光伏曲线 CSV；默认 {DEFAULT_HC_PV_PROFILE}")
    load_scale: float = Field(default=1.0, gt=0, le=10)
    power_factor: float = Field(default=1.0, gt=0, le=1.0, description="光伏恒功率因数（滞后，吸收无功）")
    constraints: ConstraintsRequest = Field(default_factory=ConstraintsRequest)
    options: HcOptionsRequest = Field(default_factory=HcOptionsRequest)
    include_curve: bool = Field(default=True, description="响应中是否包含承载力扫描曲线")
    name: str = ""

    def base_spec(self) -> timeseries.ScenarioSpec:
        if self.case is None and self.case_ref is None:
            self.case_ref = "ieee33_ampacity"
        case_path = _stage_case(_resolve_case(self))  # type: ignore[arg-type]
        return timeseries.ScenarioSpec(
            case_path=str(case_path),
            load_profile_path=self.load_profile_path or str(DEFAULT_PROFILE_DIR / DEFAULT_HC_LOAD_PROFILE),
            pv_profile_path=self.pv_profile_path or str(DEFAULT_PROFILE_DIR / DEFAULT_HC_PV_PROFILE),
            load_scale=self.load_scale,
            name=self.name,
        )


class HcSingleRequest(HcBaseRequest):
    bus: int = Field(description="光伏接入节点")


class HcBatchRequest(HcBaseRequest):
    buses: list[int] = Field(min_length=1, max_length=64, description="节点列表（各自独立评估）")
    workers: int = Field(default=1, ge=1, le=8, description="进程数；默认串行")


def _run_hc(req: HcBaseRequest, buses: list[int], workers: int = 1) -> ResultEnvelope:
    task = registry.create({"kind": "hc", "case_ref": req.case_ref, "buses": buses, "name": req.name})
    try:
        constraints = req.constraints.build()
        options = req.options.build()
        results = hc_solver.solve_batch(
            req.base_spec(), buses, constraints, options, power_factor=req.power_factor, workers=workers
        )
    except timeseries.ProfileError as exc:
        registry.fail(task.task_id, code="40002", message=str(exc), detail=exc.detail)
        raise

    store = get_hc_store()
    for r in results:
        store.save(task.task_id, r, scenario_name=req.name)

    summary = {
        "n_nodes": len(results),
        "results": [
            {k: r.to_dict(include_curve=False)[k] for k in
             ("bus", "max_mw", "limiting_type", "limiting_element_type", "limiting_element_index",
              "limiting_time", "search_method", "iterations", "elapsed_ms")}
            | {"monotonic": r.monotonicity.monotonic, "probe_passed": r.probe.get("passed")}
            for r in results
        ],
    }
    registry.succeed(task.task_id, result_dir=str(store.root / task.task_id), summary=summary)

    # 探针不通过 = 算法给出了错误（偏大）结果，按排期要求视为 bug，必须显式暴露
    failed_probe = [r.bus for r in results if r.probe.get("passed") is False]
    return ResultEnvelope(
        code=0,
        message="ok" if not failed_probe else f"节点 {failed_probe} 的 ±探针验证未通过",
        data={
            "task": registry.get(task.task_id).to_dict(),
            "results": [r.to_dict(include_curve=req.include_curve) for r in results],
        },
    )


@app.post("/api/hc/single", response_model=ResultEnvelope, summary="单节点光伏承载力")
def hc_single(req: HcSingleRequest) -> ResultEnvelope:
    env = _run_hc(req, [req.bus])
    data = env.data or {}
    return ResultEnvelope(
        code=env.code, message=env.message, data={"task": data.get("task"), **(data.get("results") or [{}])[0]}
    )


@app.post("/api/hc/batch", response_model=ResultEnvelope, summary="多节点光伏承载力（各节点独立）")
def hc_batch(req: HcBatchRequest) -> ResultEnvelope:
    if len(set(req.buses)) != len(req.buses):
        raise timeseries.ProfileError("节点列表有重复", {"field": "buses", "buses": req.buses})
    return _run_hc(req, req.buses, workers=req.workers)


@app.get("/api/hc/presets", response_model=ResultEnvelope, summary="可用约束预设")
def hc_presets() -> ResultEnvelope:
    return ResultEnvelope(data={"presets": CONSTRAINT_PRESETS})


@app.get("/api/hc/{task_id}", response_model=ResultEnvelope, summary="查询承载力任务结果（落库摘要）")
def hc_get(task_id: str) -> ResultEnvelope:
    rows = get_hc_store().by_task(task_id)
    if not rows:
        return ResultEnvelope(code=40404, message=f"承载力任务 {task_id} 不存在", data=None)
    return ResultEnvelope(data={"task_id": task_id, "rows": rows})


@app.get("/api/hc/{task_id}/curve", response_model=ResultEnvelope, summary="承载力扫描曲线（接入容量 → 指标）")
def hc_curve(task_id: str, bus: int) -> ResultEnvelope:
    detail = get_hc_store().detail(task_id, bus)
    if detail is None:
        return ResultEnvelope(code=40404, message=f"任务 {task_id} 中无节点 {bus} 的结果", data=None)
    return ResultEnvelope(
        data={
            "task_id": task_id,
            "bus": bus,
            "max_mw": detail["max_mw"],
            "limiting_type": detail["limiting_type"],
            "constraints": detail["constraints"],
            "points": detail["curve"],
        }
    )
