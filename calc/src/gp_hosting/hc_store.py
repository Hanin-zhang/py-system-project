"""承载力结果落库（排期 3.7：hc_result 表）。

现阶段用标准库 sqlite3，表结构按 MySQL 可直接迁移的方式设计：
- 只用 TEXT / REAL / INTEGER，无 SQLite 特有类型
- 一行 = 一个节点的一次评估摘要；承载力曲线、违规明细等大字段放 JSON 文件，
  表里只存路径 —— 与 I2"MySQL 只存摘要，明细落文件"的原则一致
I6 引入 Java + MySQL 后，由 Java 侧建同构表接管，本模块保留为计算侧本地缓存。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS hc_result (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                TEXT    NOT NULL,
    case_name              TEXT    NOT NULL,
    scenario_name          TEXT    NOT NULL,
    load_profile           TEXT    NOT NULL,
    pv_profile             TEXT    NOT NULL,
    node                   INTEGER NOT NULL,
    max_mw                 REAL    NOT NULL,
    limiting_type          TEXT    NOT NULL,
    limiting_element_type  TEXT,
    limiting_element_index INTEGER,
    limiting_time          TEXT,
    limiting_value         REAL,
    search_method          TEXT    NOT NULL,
    monotonic              INTEGER NOT NULL,
    probe_passed           INTEGER,
    iterations             INTEGER NOT NULL,
    elapsed_ms             REAL    NOT NULL,
    constraints_json       TEXT    NOT NULL,
    detail_path            TEXT,
    created_at             REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hc_result_task ON hc_result(task_id);
CREATE INDEX IF NOT EXISTS idx_hc_result_node ON hc_result(case_name, node);
"""


class HcStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "hc.sqlite3"
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, task_id: str, result, scenario_name: str = "") -> int:
        """写一条节点结果，明细（曲线 + 违规清单）写到 {root}/{task_id}/hc_bus{n}.json。"""
        detail_dir = self.root / task_id
        detail_dir.mkdir(parents=True, exist_ok=True)
        detail_path = detail_dir / f"hc_bus{result.bus}.json"
        detail_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

        sc = result.scenario
        row = (
            task_id,
            sc["case"],
            scenario_name,
            sc["load_profile"],
            sc["pv_profile"] or "",
            result.bus,
            result.max_mw,
            result.limiting_type,
            result.limiting_element_type,
            result.limiting_element_index,
            result.limiting_time,
            result.limiting_value,
            result.search_method,
            int(result.monotonicity.monotonic),
            None if result.probe.get("skipped") else int(result.probe["passed"]),
            result.iterations,
            result.elapsed_ms,
            json.dumps(result.constraints, sort_keys=True),
            str(detail_path),
            time.time(),
        )
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO hc_result (task_id, case_name, scenario_name, load_profile, pv_profile, node,
                   max_mw, limiting_type, limiting_element_type, limiting_element_index, limiting_time,
                   limiting_value, search_method, monotonic, probe_passed, iterations, elapsed_ms,
                   constraints_json, detail_path, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
            return int(cur.lastrowid)

    def by_task(self, task_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM hc_result WHERE task_id=? ORDER BY node", (task_id,)).fetchall()
        return [dict(r) for r in rows]

    def detail(self, task_id: str, node: int) -> dict | None:
        rows = [r for r in self.by_task(task_id) if r["node"] == node]
        if not rows or not rows[0]["detail_path"] or not Path(rows[0]["detail_path"]).exists():
            return None
        return json.loads(Path(rows[0]["detail_path"]).read_text(encoding="utf-8"))
