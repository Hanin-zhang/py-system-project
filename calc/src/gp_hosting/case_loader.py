"""算例 JSON 的 schema 与 pandapower 网络构建。

设计原则：
1. **算例只描述物理模型**（bus/line/trafo/load/sgen/ext_grid/switch），不含曲线与场景，
   曲线属于 scenario 层（I2）。
2. **单位固定**：MW / kV / km / ohm / degree。不提供单位字段，避免"同一份数据两种解释"。
3. **引用完整性在构建前校验**，错误以 `lines[3].to_bus` 这种字段级路径返回，
   而不是让 pandapower 在深处抛 KeyError。

算例 JSON 顶层结构见 `CaseSpec`；完整字段说明见 `docs/data-dictionary.md`。
"""

from __future__ import annotations

from typing import Literal

import pandapower as pp
from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------
# 错误类型
# --------------------------------------------------------------------------


class ValidationIssue(BaseModel):
    """一条字段级校验错误，path 形如 `lines[3].to_bus`。"""

    path: str
    message: str


class CaseValidationError(ValueError):
    """算例校验失败。issues 里给出全部问题，便于前端一次性展示。"""

    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        detail = "; ".join(f"{i.path}: {i.message}" for i in issues)
        super().__init__(f"算例校验失败（{len(issues)} 项）：{detail}")


# --------------------------------------------------------------------------
# 元素 schema
# --------------------------------------------------------------------------


class BusSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    name: str = ""
    vn_kv: float = Field(gt=0, description="额定电压 kV")
    type: str = "b"
    zone: str = ""
    in_service: bool = True


class LineSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    name: str = ""
    from_bus: int
    to_bus: int
    length_km: float = Field(gt=0)
    r_ohm_per_km: float = Field(ge=0)
    x_ohm_per_km: float = Field(ge=0)
    c_nf_per_km: float = Field(default=0.0, ge=0)
    max_i_ka: float = Field(gt=0)
    type: str | None = None
    in_service: bool = True


class TrafoSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    name: str = ""
    hv_bus: int
    lv_bus: int
    sn_mva: float = Field(gt=0)
    vn_hv_kv: float = Field(gt=0)
    vn_lv_kv: float = Field(gt=0)
    vk_percent: float = Field(gt=0)
    vkr_percent: float = Field(ge=0)
    pfe_kw: float = Field(default=0.0, ge=0)
    i0_percent: float = Field(default=0.0, ge=0)
    shift_degree: float = 0.0
    in_service: bool = True


class LoadSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    name: str = ""
    bus: int
    p_mw: float
    q_mvar: float = 0.0
    scaling: float = Field(default=1.0, ge=0)
    in_service: bool = True


class SgenSpec(BaseModel):
    """静态发电机。分布式光伏在 pandapower 里用 sgen 表示（不参与电压控制）。"""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    name: str = ""
    bus: int
    p_mw: float = Field(ge=0)
    q_mvar: float = 0.0
    scaling: float = Field(default=1.0, ge=0)
    type: str = "wye"
    in_service: bool = True


class ExtGridSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    name: str = ""
    bus: int
    vm_pu: float = Field(default=1.0, gt=0, le=1.5)
    va_degree: float = 0.0
    slack_weight: float = 1.0
    in_service: bool = True


class SwitchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    name: str = ""
    bus: int
    element: int
    et: Literal["l", "b", "t"]
    closed: bool = True
    type: str = "LS"
    in_service: bool = True


class CaseMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    source: str = ""
    description: str = ""
    vn_kv: float = Field(gt=0, description="主干电压等级，仅用于展示与合理性校验")
    f_hz: float = 50.0


class CaseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = "1.0"
    meta: CaseMeta
    buses: list[BusSpec]
    lines: list[LineSpec] = []
    trafos: list[TrafoSpec] = []
    loads: list[LoadSpec] = []
    sgens: list[SgenSpec] = []
    ext_grids: list[ExtGridSpec] = []
    switches: list[SwitchSpec] = []


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


def _dup_indices(table: str, items: list) -> list[ValidationIssue]:
    seen: dict[int, int] = {}
    issues = []
    for pos, item in enumerate(items):
        if item.index in seen:
            issues.append(
                ValidationIssue(
                    path=f"{table}[{pos}].index",
                    message=f"索引 {item.index} 与 {table}[{seen[item.index]}] 重复",
                )
            )
        else:
            seen[item.index] = pos
    return issues


def validate_case(spec: CaseSpec) -> list[ValidationIssue]:
    """返回全部校验问题（空列表表示通过）。不抛异常，便于一次收集所有错误。"""
    issues: list[ValidationIssue] = []

    for table, items in (
        ("buses", spec.buses),
        ("lines", spec.lines),
        ("trafos", spec.trafos),
        ("loads", spec.loads),
        ("sgens", spec.sgens),
        ("ext_grids", spec.ext_grids),
        ("switches", spec.switches),
    ):
        issues.extend(_dup_indices(table, items))

    if not spec.buses:
        issues.append(ValidationIssue(path="buses", message="至少需要一个节点"))
        return issues  # 后续引用校验无意义

    bus_ids = {b.index for b in spec.buses}

    if not spec.ext_grids:
        issues.append(ValidationIssue(path="ext_grids", message="至少需要一个外部电网（平衡节点）"))

    def check_bus_ref(path: str, bus: int) -> None:
        if bus not in bus_ids:
            issues.append(ValidationIssue(path=path, message=f"节点 {bus} 不存在"))

    for i, line in enumerate(spec.lines):
        check_bus_ref(f"lines[{i}].from_bus", line.from_bus)
        check_bus_ref(f"lines[{i}].to_bus", line.to_bus)
        if line.from_bus == line.to_bus:
            issues.append(ValidationIssue(path=f"lines[{i}]", message="首末端节点相同"))

    for i, trafo in enumerate(spec.trafos):
        check_bus_ref(f"trafos[{i}].hv_bus", trafo.hv_bus)
        check_bus_ref(f"trafos[{i}].lv_bus", trafo.lv_bus)
        if trafo.hv_bus == trafo.lv_bus:
            issues.append(ValidationIssue(path=f"trafos[{i}]", message="高低压侧节点相同"))

    for table, items in (("loads", spec.loads), ("sgens", spec.sgens), ("ext_grids", spec.ext_grids)):
        for i, item in enumerate(items):
            check_bus_ref(f"{table}[{i}].bus", item.bus)

    line_ids = {ln.index for ln in spec.lines}
    trafo_ids = {t.index for t in spec.trafos}
    for i, sw in enumerate(spec.switches):
        check_bus_ref(f"switches[{i}].bus", sw.bus)
        target = {"l": line_ids, "t": trafo_ids, "b": bus_ids}[sw.et]
        if sw.element not in target:
            issues.append(
                ValidationIssue(
                    path=f"switches[{i}].element",
                    message=f"et={sw.et} 时元素 {sw.element} 不存在",
                )
            )

    # 物理合理性：防止单位错（MW 写成 kW）在潮流阶段才暴露。
    # 经验阈值：中压馈线负荷密度按 100 MW/kV 封顶已非常宽松（12.66 kV -> 1266 MW），
    # 一旦把 kW 当 MW 写入，量级会直接越界。
    total_load_mw = sum(ld.p_mw for ld in spec.loads if ld.in_service)
    total_sgen_mw = sum(sg.p_mw for sg in spec.sgens if sg.in_service)
    load_limit_mw = 100.0 * spec.meta.vn_kv
    if total_load_mw > load_limit_mw:
        issues.append(
            ValidationIssue(
                path="loads",
                message=(
                    f"总有功负荷 {total_load_mw:.1f} MW 超过 {load_limit_mw:.0f} MW 的合理性上限，"
                    f"疑似单位错误（kW 当作 MW）"
                ),
            )
        )
    if total_sgen_mw > load_limit_mw:
        issues.append(
            ValidationIssue(
                path="sgens",
                message=(
                    f"光伏总容量 {total_sgen_mw:.1f} MW 超过 {load_limit_mw:.0f} MW 的合理性上限，"
                    f"疑似单位错误（kW 当作 MW）"
                ),
            )
        )

    for i, bus in enumerate(spec.buses):
        if not (0.1 <= bus.vn_kv <= 1000):
            issues.append(
                ValidationIssue(path=f"buses[{i}].vn_kv", message=f"电压等级 {bus.vn_kv} kV 不合理")
            )

    return issues


def _humanize_pydantic_error(err: dict) -> str:
    """把 pydantic 的英文错误转成可读中文（只覆盖本项目会遇到的几类）。"""
    etype = err.get("type", "")
    ctx = err.get("ctx") or {}
    msg = err.get("msg", "")
    if etype == "greater_than":
        return f"必须大于 {ctx.get('gt')}"
    if etype == "greater_than_equal":
        return f"必须大于等于 {ctx.get('ge')}"
    if etype == "less_than_equal":
        return f"必须小于等于 {ctx.get('le')}"
    if etype == "missing":
        return "缺少必填字段"
    if etype == "extra_forbidden":
        return "不允许的字段"
    if etype == "literal_error":
        return f"取值必须为 {ctx.get('expected')}"
    if etype == "float_parsing" or etype == "int_parsing":
        return "必须为数值"
    if etype == "model_type":
        return "类型不正确"
    return msg


def load_case_dict(data: dict) -> CaseSpec:
    """从 dict 解析并做完整校验，失败抛 CaseValidationError（字段级 issues）。"""
    try:
        spec = CaseSpec.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError
        issues = []
        for err in getattr(exc, "errors", lambda: [])():
            loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
            issues.append(ValidationIssue(path=loc, message=_humanize_pydantic_error(err)))
        if not issues:
            issues.append(ValidationIssue(path="<root>", message=str(exc)))
        raise CaseValidationError(issues) from exc

    issues = validate_case(spec)
    if issues:
        raise CaseValidationError(issues)
    return spec


def load_case_file(path: str) -> CaseSpec:
    import json
    from pathlib import Path

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return load_case_dict(data)


# --------------------------------------------------------------------------
# 构建 pandapower 网络
# --------------------------------------------------------------------------


def build_net(spec: CaseSpec) -> pp.pandapowerNet:
    """CaseSpec -> pandapowerNet。调用前自动执行完整校验。"""
    issues = validate_case(spec)
    if issues:
        raise CaseValidationError(issues)

    net = pp.create_empty_network(name=spec.meta.name, f_hz=spec.meta.f_hz, sn_mva=1.0)

    for b in spec.buses:
        pp.create_bus(
            net,
            vn_kv=b.vn_kv,
            index=b.index,
            name=b.name,
            type=b.type,
            zone=b.zone,
            in_service=b.in_service,
        )

    for ln in spec.lines:
        kwargs = {}
        if ln.type is not None:
            kwargs["type"] = ln.type
        pp.create_line_from_parameters(
            net,
            from_bus=ln.from_bus,
            to_bus=ln.to_bus,
            length_km=ln.length_km,
            r_ohm_per_km=ln.r_ohm_per_km,
            x_ohm_per_km=ln.x_ohm_per_km,
            c_nf_per_km=ln.c_nf_per_km,
            max_i_ka=ln.max_i_ka,
            name=ln.name,
            index=ln.index,
            in_service=ln.in_service,
            **kwargs,
        )

    for t in spec.trafos:
        pp.create_transformer_from_parameters(
            net,
            hv_bus=t.hv_bus,
            lv_bus=t.lv_bus,
            sn_mva=t.sn_mva,
            vn_hv_kv=t.vn_hv_kv,
            vn_lv_kv=t.vn_lv_kv,
            vk_percent=t.vk_percent,
            vkr_percent=t.vkr_percent,
            pfe_kw=t.pfe_kw,
            i0_percent=t.i0_percent,
            shift_degree=t.shift_degree,
            name=t.name,
            index=t.index,
            in_service=t.in_service,
        )

    for ld in spec.loads:
        pp.create_load(
            net,
            bus=ld.bus,
            p_mw=ld.p_mw,
            q_mvar=ld.q_mvar,
            name=ld.name,
            index=ld.index,
            scaling=ld.scaling,
            in_service=ld.in_service,
        )

    for sg in spec.sgens:
        pp.create_sgen(
            net,
            bus=sg.bus,
            p_mw=sg.p_mw,
            q_mvar=sg.q_mvar,
            name=sg.name,
            index=sg.index,
            scaling=sg.scaling,
            type=sg.type,
            in_service=sg.in_service,
        )

    for eg in spec.ext_grids:
        pp.create_ext_grid(
            net,
            bus=eg.bus,
            vm_pu=eg.vm_pu,
            va_degree=eg.va_degree,
            name=eg.name,
            index=eg.index,
            slack_weight=eg.slack_weight,
            in_service=eg.in_service,
        )

    for sw in spec.switches:
        pp.create_switch(
            net,
            bus=sw.bus,
            element=sw.element,
            et=sw.et,
            closed=sw.closed,
            type=sw.type,
            name=sw.name,
            index=sw.index,
            in_service=sw.in_service,
        )

    return net


def net_to_case_dict(net: pp.pandapowerNet) -> dict:
    """pandapowerNet -> 算例 dict（用于导出内置算例）。

    只导出本平台 schema 里的列，不导出 res_* 结果表与元数据。
    """
    buses = [
        BusSpec(
            index=int(idx),
            name=str(row.get("name") or ""),
            vn_kv=float(row["vn_kv"]),
            type=str(row.get("type") or "b"),
            zone=str(row.get("zone") or ""),
            in_service=bool(row.get("in_service", True)),
        ).model_dump()
        for idx, row in net.bus.iterrows()
    ]

    lines = []
    for idx, row in net.line.iterrows():
        lines.append(
            LineSpec(
                index=int(idx),
                name=str(row.get("name") or ""),
                from_bus=int(row["from_bus"]),
                to_bus=int(row["to_bus"]),
                length_km=float(row["length_km"]),
                r_ohm_per_km=float(row["r_ohm_per_km"]),
                x_ohm_per_km=float(row["x_ohm_per_km"]),
                c_nf_per_km=float(row.get("c_nf_per_km") or 0.0),
                max_i_ka=float(row["max_i_ka"]),
                type=(None if row.get("type") is None else str(row["type"])),
                in_service=bool(row.get("in_service", True)),
            ).model_dump()
        )

    trafos = []
    for idx, row in net.trafo.iterrows():
        trafos.append(
            TrafoSpec(
                index=int(idx),
                name=str(row.get("name") or ""),
                hv_bus=int(row["hv_bus"]),
                lv_bus=int(row["lv_bus"]),
                sn_mva=float(row["sn_mva"]),
                vn_hv_kv=float(row["vn_hv_kv"]),
                vn_lv_kv=float(row["vn_lv_kv"]),
                vk_percent=float(row["vk_percent"]),
                vkr_percent=float(row["vkr_percent"]),
                pfe_kw=float(row.get("pfe_kw") or 0.0),
                i0_percent=float(row.get("i0_percent") or 0.0),
                shift_degree=float(row.get("shift_degree") or 0.0),
                in_service=bool(row.get("in_service", True)),
            ).model_dump()
        )

    loads = [
        LoadSpec(
            index=int(idx),
            name=str(row.get("name") or ""),
            bus=int(row["bus"]),
            p_mw=float(row["p_mw"]),
            q_mvar=float(row.get("q_mvar") or 0.0),
            scaling=float(row.get("scaling", 1.0)),
            in_service=bool(row.get("in_service", True)),
        ).model_dump()
        for idx, row in net.load.iterrows()
    ]

    sgens = [
        SgenSpec(
            index=int(idx),
            name=str(row.get("name") or ""),
            bus=int(row["bus"]),
            p_mw=float(row["p_mw"]),
            q_mvar=float(row.get("q_mvar") or 0.0),
            scaling=float(row.get("scaling", 1.0)),
            type=str(row.get("type") or "wye"),
            in_service=bool(row.get("in_service", True)),
        ).model_dump()
        for idx, row in net.sgen.iterrows()
    ]

    ext_grids = [
        ExtGridSpec(
            index=int(idx),
            name=str(row.get("name") or ""),
            bus=int(row["bus"]),
            vm_pu=float(row["vm_pu"]),
            va_degree=float(row.get("va_degree") or 0.0),
            slack_weight=float(row.get("slack_weight", 1.0)),
            in_service=bool(row.get("in_service", True)),
        ).model_dump()
        for idx, row in net.ext_grid.iterrows()
    ]

    switches = [
        SwitchSpec(
            index=int(idx),
            name=str(row.get("name") or ""),
            bus=int(row["bus"]),
            element=int(row["element"]),
            et=str(row["et"]),
            closed=bool(row.get("closed", True)),
            type=str(row.get("type") or "LS"),
            in_service=bool(row.get("in_service", True)),
        ).model_dump()
        for idx, row in net.switch.iterrows()
    ]

    return {
        "version": "1.0",
        "meta": {
            "name": str(net.name or "unnamed"),
            "source": "",
            "description": "",
            "vn_kv": float(net.bus.vn_kv.max()),
            "f_hz": float(net.f_hz),
        },
        "buses": buses,
        "lines": lines,
        "trafos": trafos,
        "loads": loads,
        "sgens": sgens,
        "ext_grids": ext_grids,
        "switches": switches,
    }
