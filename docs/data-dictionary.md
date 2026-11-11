# 数据字典

本平台所有计算输入的单位约定：**MW / kV / km / ohm / degree / p.u.**
算例 JSON 不携带单位字段——单位是约定，不是数据。这是刻意的：让同一份数据只有一种解释。

> 数据来源声明：本项目全部算例基于公开标准算例（IEEE 33-bus、CIGRE MV）与**模拟**负荷/光伏曲线，
> 不使用真实电网运行数据。

---

## 算例 JSON 顶层结构

```json
{
  "version": "1.0",
  "meta":   { "name": "...", "source": "...", "description": "...", "vn_kv": 12.66, "f_hz": 50 },
  "buses": [], "lines": [], "trafos": [], "loads": [],
  "sgens": [], "ext_grids": [], "switches": []
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `version` | string | schema 版本，当前 `1.0` |
| `meta.name` | string | 算例名，展示用 |
| `meta.source` | string | 数据来源（必填，如 `IEEE 33-bus (pandapower.networks.case33bw)`） |
| `meta.vn_kv` | float | 主干电压等级，用于合理性校验 |
| `meta.f_hz` | float | 系统频率，默认 50 |

每个数组元素的 `index` 是**主键**，线路/负荷用 `from_bus`、`to_bus`、`bus` 引用它。
索引必须唯一、非负；不要求连续。

---

## buses · 节点

| 字段 | 类型 | 单位 | 必填 | 说明 |
|---|---|---|---|---|
| `index` | int | — | ✓ | 节点编号，全表唯一 |
| `name` | string | — | | 展示名 |
| `vn_kv` | float | kV | ✓ | 额定电压，> 0 |
| `type` | string | — | | `b` 母线 / `n` 中性点 |
| `zone` | string | — | | 分区标记，本平台留空 |
| `in_service` | bool | — | | 默认 `true` |

IEEE 33-bus 全部节点 `vn_kv = 12.66`。

---

## lines · 线路

| 字段 | 类型 | 单位 | 必填 | 说明 |
|---|---|---|---|---|
| `index` | int | — | ✓ | 线路编号 |
| `name` | string | — | | |
| `from_bus` | int | — | ✓ | 首端节点，必须存在于 `buses` |
| `to_bus` | int | — | ✓ | 末端节点，必须存在于 `buses`，且 ≠ `from_bus` |
| `length_km` | float | km | ✓ | > 0 |
| `r_ohm_per_km` | float | Ω/km | ✓ | 单位长度电阻 |
| `x_ohm_per_km` | float | Ω/km | ✓ | 单位长度电抗 |
| `c_nf_per_km` | float | nF/km | | 对地电容，配网常为 0 |
| `max_i_ka` | float | kA | ✓ | 载流量，决定负载率 |
| `type` | string/null | — | | 标准导线类型名，按参数建模时为 `null` |
| `in_service` | bool | — | | |

负载率定义：`loading_percent = max(|I_from|, |I_to|) / max_i_ka × 100%`。

IEEE 33-bus 线路总长 ~37 km，`r` 约 0.2–0.6 Ω/km，`x` 约 0.1–0.3 Ω/km，`max_i_ka` 0.2–0.9 kA。

---

## trafos · 变压器

| 字段 | 类型 | 单位 | 必填 | 说明 |
|---|---|---|---|---|
| `index` | int | — | ✓ | |
| `hv_bus` / `lv_bus` | int | — | ✓ | 高压侧 / 低压侧节点 |
| `sn_mva` | float | MVA | ✓ | 额定容量 |
| `vn_hv_kv` / `vn_lv_kv` | float | kV | ✓ | 两侧额定电压 |
| `vk_percent` | float | % | ✓ | 短路电压百分比 |
| `vkr_percent` | float | % | ✓ | 短路电压电阻分量 |
| `pfe_kw` | float | kW | | 铁损 |
| `i0_percent` | float | % | | 空载电流 |
| `shift_degree` | float | degree | | 移相角，配网通常 0 |
| `in_service` | bool | — | | |

IEEE 33-bus 是**单一电压等级**算例，没有变压器；CIGRE MV 含 20/0.4 kV 变压器。

---

## loads · 负荷

| 字段 | 类型 | 单位 | 必填 | 说明 |
|---|---|---|---|---|
| `index` | int | — | ✓ | |
| `bus` | int | — | ✓ | 挂接节点 |
| `p_mw` | float | MW | ✓ | 有功负荷（正=消耗） |
| `q_mvar` | float | Mvar | | 无功负荷 |
| `scaling` | float | — | | 缩放系数，默认 1 |
| `in_service` | bool | — | | |

IEEE 33-bus 总负荷 3.715 MW + 2.30 Mvar。

---

## sgens · 静态发电机（分布式光伏）

| 字段 | 类型 | 单位 | 必填 | 说明 |
|---|---|---|---|---|
| `index` | int | — | ✓ | |
| `bus` | int | — | ✓ | 并网点 |
| `p_mw` | float | MW | ✓ | 有功出力（正=注入电网） |
| `q_mvar` | float | Mvar | | 无功，`+` 发出 / `-` 吸收 |
| `scaling` | float | — | | 曲线时段缩放用 |
| `type` | string | — | | 默认 `wye` |
| `in_service` | bool | — | | |

为什么用 `sgen` 而不是 `gen`：光伏是电流源型并网，不参与电压/频率调节。
`gen` 是电压控制节点（PV 节点），用它会把并网点电压钉死在给定值，承载力评估失真。

---

## ext_grids · 外部电网（平衡节点）

| 字段 | 类型 | 单位 | 必填 | 说明 |
|---|---|---|---|---|
| `index` | int | — | ✓ | |
| `bus` | int | — | ✓ | 通常挂在 0 号节点 |
| `vm_pu` | float | p.u. | | 参考电压，默认 1.0 |
| `va_degree` | float | degree | | 参考相角，默认 0 |
| `slack_weight` | float | — | | 多平衡节点时的分摊权重 |

至少一个，否则算例校验直接失败。

---

## switches · 开关

| 字段 | 类型 | 单位 | 必填 | 说明 |
|---|---|---|---|---|
| `index` | int | — | ✓ | |
| `bus` | int | — | ✓ | 开关的母线侧节点 |
| `element` | int | — | ✓ | 被连接元素编号 |
| `et` | string | — | ✓ | `l` 线路 / `t` 变压器 / `b` 母线间 |
| `closed` | bool | — | | 默认 `true` |
| `type` | string | — | | `LS` 负荷开关 / `CB` 断路器 |

IEEE 33-bus 无开关；环网算例会用到（`closed=false` 表示常开联络点）。

---

## 曲线 CSV（I2 起使用）

`calc/data/profiles/` 下，96 点（15 min 分辨率）典型日曲线。

| 列 | 说明 |
|---|---|
| `time_index` | 0–95 |
| `factor` | 归一化系数，相对基准值 |

- 负荷曲线 `load_typical_day.csv`：双峰（09–11、19–21），峰谷比 0.55–1.0
- 光伏曲线 `pv_typical_day.csv`：单峰（正午），06:00 起、18:00 止

曲线是**模拟生成**的，生成参数见 `calc/scripts/gen_profiles.py` 文件头。
