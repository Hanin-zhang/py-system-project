"""FastAPI 计算服务：潮流计算接口。

I1 阶段只暴露潮流能力。I2/I3 会新增 timeseries / hc 路由。

约定：
- 所有响应统一为 `{code, message, data}`，`code=0` 表示成功。
  这样 Java 侧 DTO 可以统一处理，无需按接口区分。
- 参数错误返回 **HTTP 200 + code!=0**，只在真正的服务端故障才用 5xx。
  目的是让前端/Java 侧拿到字段级错误信息，而不是一个被网关吞掉的 500。
- 结果字段 snake_case，与算出结构一一对应。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gp_hosting import powerflow
from gp_hosting.case_loader import (
    CaseMeta,
    CaseValidationError,
    CaseSpec,
    build_net,
    load_case_dict,
)
from gp_hosting.constraints import Constraints, check

logger = logging.getLogger("gp_hosting.api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(
    title="配电网光伏承载力平台 · 计算服务",
    version="0.1.0",
    description="基于 pandapower 的潮流、时序仿真与承载力评估。全部算例基于公开标准算例与模拟数据。",
)


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


# 让 CaseMeta 参与类型检查（避免 import 未使用告警，同时保留后续扩展点）
_ = CaseSpec, CaseMeta
