"""生成 I4 热力图的最小 HTML 演示页（I6 前的前端替身）。

用法：
    uv run python scripts/export_heatmap_demo.py

做的事：
    1. 跑一个确定越限的场景（bus17 接 4 MW）
    2. 调 analysis.build_attribution() 拿到 ECharts 数据
    3. 把数据**内嵌**进单个 HTML 文件，双击即可打开，无需起服务

为什么内嵌而不是 fetch：
    演示页要能直接发给别人看，不应该依赖后端在跑。数据量很小
    （只有越限单元，实测 40 个格子），内嵌完全够用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gp_hosting import analysis, timeseries  # noqa: E402
from gp_hosting.constraints import Constraints  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
P = ROOT / "data" / "profiles"
OUT = ROOT.parent / "docs" / "figures" / "heatmap_demo.html"

TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>越限热力图 · 配电网光伏承载力平台</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
  body {{ font-family: -apple-system, "PingFang SC", sans-serif; margin: 24px; color: #1f2328; }}
  h1 {{ font-size: 20px; margin-bottom: 4px; }}
  .meta {{ color: #57606a; font-size: 13px; margin-bottom: 16px; line-height: 1.7; }}
  #chart {{ width: 100%; height: 620px; }}
  .legend-note {{ font-size: 12px; color: #57606a; margin-top: 8px; }}
  code {{ background: #f6f8fa; padding: 1px 5px; border-radius: 4px; }}
</style>
</head>
<body>
<h1>越限热力图 · {title}</h1>
<div class="meta">
  算例 <code>{case}</code> ｜ 曲线 <code>{load_profile}</code> / <code>{pv_profile}</code><br>
  约束：电压 {vm_min}–{vm_max} p.u.，线路 {line_max}%，变压器 {trafo_max}%<br>
  <b>{n_events}</b> 条越限事件，涉及 <b>{n_elements}</b> 台设备，首次越限 <b>{first_time}</b>
  （{first_constraint} 设备 {first_element}，持续 {first_steps} 个时段）
</div>
<div id="chart"></div>
<div class="legend-note">
  横轴 = 时刻（96 点，15 min）；纵轴 = 节点编号；颜色 = 归一化裕度（负值 = 越限，越深越严重）。
  仅绘制越限单元，无越限的格子为空白。
</div>
<script>
const D = {payload};

function render(elId, cfg) {{
  const chart = echarts.init(document.getElementById(elId));
  chart.setOption({{
    tooltip: {{
      position: 'top',
      formatter: p => `${{cfg.rowLabel}} ${{cfg.elements[p.value[1]]}}<br>` +
                      `${{cfg.stepTimes[p.value[0]]}}<br>` +
                      `裕度 ${{(p.value[2] * 100).toFixed(2)}}%`
    }},
    grid: {{ left: 56, right: 24, top: 24, bottom: 96 }},
    xAxis: {{
      type: 'category',
      data: cfg.stepTimes,
      name: '时刻',
      nameLocation: 'middle', nameGap: 28,
      axisLabel: {{ interval: 7, rotate: 0, fontSize: 10 }},
      splitArea: {{ show: false }}
    }},
    yAxis: {{
      type: 'category',
      data: cfg.elements.map(String),
      name: cfg.rowLabel,
      nameLocation: 'middle', nameGap: 40,
      axisLabel: {{ fontSize: 10 }}
    }},
    visualMap: {{
      // 值域按数据自适应：固定 [-1, 0] 会让 2~3% 的越限全部挤在接近 0 的浅色区，
      // 肉眼看不出"哪里更严重"。这里下界取实际最小值（向上取整到 1%）。
      min: cfg.vmin, max: 0,
      calculable: true,
      orient: 'horizontal',
      left: 'center', bottom: 8,
      // ECharts：text[0] 显示在**最大值**端，text[1] 显示在最小值端
      text: ['轻微（0）', `越限重（${{(cfg.vmin * 100).toFixed(0)}}%）`],
      textStyle: {{ fontSize: 11 }},
      formatter: v => `${{(v * 100).toFixed(2)}}%`,
      inRange: {{ color: ['#08306b', '#2171b5', '#6baed6', '#c6dbef'] }}
    }},
    series: [{{
      type: 'heatmap',
      data: cfg.data,
      emphasis: {{ itemStyle: {{ borderColor: '#333', borderWidth: 1 }} }},
      progressive: 0
    }}]
  }});
  window.addEventListener('resize', () => chart.resize());
}}

const vm = D.heatmap.vm;
const vmin = vm.data.length
  ? Math.max(-1, Math.ceil(Math.min(...vm.data.map(d => d[2])) * 100) / 100)
  : -1;
render('chart', {{
  rowLabel: '节点',
  elements: vm.elements,
  stepTimes: vm.step_times,
  data: vm.data,
  vmin: vmin
}});
</script>
</body>
</html>
"""


def main() -> None:
    scenario = timeseries.assemble_scenario(
        timeseries.ScenarioSpec(
            case_path=str(ROOT / "data" / "cases" / "ieee33_ampacity.json"),
            load_profile_path=str(P / "simbench_pv_max_day_20160513_load.csv"),
            pv_profile_path=str(P / "simbench_pv_max_day_20160513_pv.csv"),
            sgens=[timeseries.SgenInjection(bus=17, capacity_mw=4.0)],
        )
    )
    result = timeseries.run_timeseries(scenario)
    c = Constraints()
    report = analysis.build_attribution(result, c)
    payload = report.to_dict()
    # 控制文件体积：演示页只画电压热力图
    payload["heatmap"] = {"vm": payload["heatmap"]["vm"]}
    payload.pop("events", None)
    payload.pop("top_by_peak_severity", None)

    first = report.first_violation or {}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        TEMPLATE.format(
            title="bus17 接入 4.0 MW 光伏",
            case=scenario.case_spec.meta.name,
            load_profile=scenario.load_profile.name,
            pv_profile=scenario.pv_profile.name,
            vm_min=c.vm_min_pu, vm_max=c.vm_max_pu,
            line_max=c.line_loading_percent_max, trafo_max=c.trafo_loading_percent_max,
            n_events=report.n_events,
            n_elements=report.n_affected_elements,
            first_time=report.first_time,
            first_constraint=first.get("constraint", "-"),
            first_element=first.get("element_index", "-"),
            first_steps=first.get("duration_steps", "-"),
            payload=json.dumps(payload, ensure_ascii=False),
        ),
        encoding="utf-8",
    )
    print(f"已生成 {OUT}  （{OUT.stat().st_size / 1024:.1f} KB，"
          f"{report.heatmap['vm']['n_violating_cells']} 个越限单元）")


if __name__ == "__main__":
    main()
