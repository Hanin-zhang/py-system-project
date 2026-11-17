# 配电网分布式光伏承载力评估与治理决策平台

基于 pandapower 的配电网光伏接入分析平台。回答三个工程问题：

> **这个节点最多能接多少光伏？什么时段、哪台设备先顶不住？加治理措施后能提升多少？**

> 数据声明：网络参数来自**公开标准算例**（IEEE 33-bus / CIGRE MV，pandapower 内置），
> 负荷与光伏曲线来自 **SimBench** 公开数据集（pandapower 团队发布，2016 年 15 min 实测标准曲线）。
> 不使用任何真实电网运行数据。全部派生数据与假设见 [docs/hosting-capacity.md](docs/hosting-capacity.md)。

---

## 架构

```
Vue 3 + Vite + ECharts  (web/)        ← 展示层
        │ REST/JSON
Spring Boot 3 + MySQL    (server/)    ← 业务层：算例 / 场景 / 任务 / 结果
        │ 异步 HTTP（taskId）
FastAPI + pandapower     (calc/)      ← 计算层：潮流 / 时序 / 承载力 / 治理
        │
   MySQL + 本地文件（Parquet 存时序结果）
```

计算服务内部分层，**上层只调下层**：

```
case_loader → powerflow → timeseries → hc_solver → mitigation
```

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| W0 | 工程骨架 / 算例导出 / 曲线 / 单位校验 | ✅ |
| I1 | 潮流计算内核 + 约束判定 + HTTP 接口 + 测试 | ✅ |
| I2 | 时序仿真引擎 + Parquet 落盘 + 任务注册表 | ✅ |
| I3 | 承载力评估（核心）+ 单/批量接口 + sqlite 落库 | ✅ |
| I4 | 越限归因 + 敏感性 + 瓶颈排序 + 接入点对比 | ✅ |
| I5 | 治理策略与方案对比（核心） | 待开始 |
| I6 | Java 平台 + 前端 v1 | 待开始 |

## 快速开始（计算服务）

```bash
cd calc
uv sync                      # 首次安装依赖（国内建议加 --index-url 镜像）
uv run pytest                # 运行测试（218 项，覆盖率 95%）
uv run python -W ignore scripts/smoke_powerflow.py    # 冒烟：潮流 + 对拍
uv run python scripts/verify_hc.py                    # I3 验收：逐条核对 DoD（约 2 min）
uv run python scripts/verify_analysis.py              # I4 验收（约 1 min，17 项）
uv run uvicorn gp_hosting.api:app --port 8010 --app-dir src
```

打开 http://127.0.0.1:8010/docs 查看接口文档。

## 目录结构

```
calc/                          Python 计算服务
├── src/gp_hosting/
│   ├── case_loader.py         算例 schema、校验、JSON ↔ pandapower 网络
│   ├── powerflow.py           交流潮流封装 + 结构化结果
│   ├── constraints.py         运行约束与越限判定
│   ├── timeseries.py          曲线 / 场景组装 / 逐时段潮流 / Parquet 落盘
│   ├── hc_solver.py           承载力：单调性验证 + 二分/线性搜索 + 探针
│   ├── hc_store.py            承载力结果落库（SQLite，结构对齐 MySQL）
│   ├── analysis.py            越限归因 / 热力图 / 灵敏度 / 瓶颈排序 / 接入点对比
│   ├── tasks.py               任务注册表（进程内）
│   └── api.py                 FastAPI 路由
├── scripts/
│   ├── export_cases.py        从 pandapower 导出公开算例为 JSON
│   ├── gen_profiles.py        生成 96 点模拟负荷/光伏曲线（I2 早期用例）
│   ├── import_simbench_profiles.py  从 SimBench 导出典型日曲线（I3 起默认）
│   ├── smoke_powerflow.py     冒烟：加载 → 潮流 → 打印指标 → 对拍
│   ├── pv_trial.py            单点光伏试算 + 电压沿馈线分布图
│   ├── verify_parquet.py      独立校验落盘结果（形状/数值/电量闭合）
│   ├── verify_hc.py           I3 验收：逐条核对排期 DoD
│   ├── verify_analysis.py     I4 验收
│   └── export_heatmap_demo.py 生成 ECharts 热力图演示页（数据内嵌）
│   └── unit_check.py          单位与量级校验（故意构造错误数据验证拦截）
├── data/
│   ├── cases/                 算例 JSON
│   ├── profiles/              96 点曲线 CSV（SimBench 导出 + 脚本生成）
│   └── results/               时序结果（按 taskId 分目录，gitignore）
└── tests/                     pytest（218 项）
server/                        Java 业务服务（待建）
web/                           前端（待建）
docs/                          数据字典、承载力 / 归因设计文档、热力图演示页
experiments/                   实验配置与结果（I7）
```

## 算例

| 文件 | 说明 |
|---|---|
| `ieee33.json` | IEEE 33-bus，**原样导出**自 `pandapower.networks.case33bw()`。线路 `max_i_ka=99999`（原算例不约束载流量），5 条联络线停运，纯辐射状。 |
| `ieee33_ampacity.json` | 同上，仅把全部线路载流量改为 **0.29 kA**（pandapower 标准线型 `70-AL1/11-ST1A 10.0`），使"线路过载"约束可被观测。基线负载率 72.5%。 |
| `cigre_mv.json` | CIGRE MV 20 kV 算例，用于证明算法不依赖单一算例。 |

## 承载力（I3）

```bash
# 单节点：默认算例 ieee33_ampacity + SimBench 光伏最大日 + GB/T 12325
curl -s -X POST localhost:8010/api/hc/single -H 'Content-Type: application/json' \
  -d '{"bus": 17}' | python -m json.tool

# 多节点（各节点独立评估）
curl -s -X POST localhost:8010/api/hc/batch -H 'Content-Type: application/json' \
  -d '{"buses": [1, 5, 8, 12, 17]}' | python -m json.tool
```

实测（96 时段，单节点 9–12 s）：bus1 13.95 MW（线路受限）→ bus17 3.07 MW（电压受限），
主干严格递减。**注意**：IEEE 33 在满负荷日基线电压即 0.9131 p.u. < 0.93，
此时 GB/T 口径会返回 `max_mw=0, limiting_type=baseline_infeasible` —— 这是算例属性，不是错误。
详见 [docs/hosting-capacity.md](docs/hosting-capacity.md)。

## 归因与敏感性（I4）

```bash
# 归因报告（读 I2 已落盘的结果，不重算潮流）
curl -s "localhost:8010/api/analysis/attribution/{taskId}?merge_gap=4" | python -m json.tool

# 灵敏度 + 瓶颈排序
curl -s -X POST localhost:8010/api/analysis/sensitivity -H 'Content-Type: application/json' \
  -d '{"bus": 17, "base_mw": 3.0}' | python -m json.tool

# 等容量下比较接入点
curl -s -X POST localhost:8010/api/analysis/compare -H 'Content-Type: application/json' \
  -d '{"buses": [1, 8, 17, 32], "capacity_mw": 5.0}' | python -m json.tool
```

热力图演示页：`docs/figures/heatmap_demo.html`（双击打开，数据内嵌，无需起服务）。

**注意**：排期要求求 `∂V_min/∂P`，本平台实测该值**恒为 0**——全天最低电压出现在
夜间光伏零出力时刻，与装机容量无关。决策依据是 `∂V_max/∂P` 与注入节点在峰值时刻的
`∂V/∂P`。详见 [docs/attribution.md](docs/attribution.md)。

## 关键约定

- **单位**：MW / kV / km / Ω / degree / p.u.，全链路不做隐式换算。
- **接口响应**：`{code, message, data}`，`code=0` 成功；参数错误返回 HTTP 200 + `code=40001`（字段级错误），不吞成 500。
- **潮流参数**：`algorithm=nr`、`init=auto`、`calculate_voltage_angles=False`、`trafo_model=t`。
- **依赖锁**：pandapower 2.14 依赖 `numpy.Inf`（NumPy 2 已移除），因此 numpy 锁在 1.26.x。

## 环境要求

- Python 3.12（`uv` 管理）
- 后续：JDK 21、Node 20+、MySQL 8
