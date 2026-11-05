"""读取固定遥测并生成两种取证模式共用的服务证据。"""

import json
from pathlib import Path

import duckdb
from fetch_pilot import fetch_case


def load_evidence(directory: Path) -> dict[str, dict[str, object]]:
    with duckdb.connect() as db:
        metrics = db.read_parquet(str(directory / "metrics.parquet"))
        columns = [name for name in metrics.columns if name != "time"]
        start, end = metrics.aggregate("min(time), max(time)").fetchone()
        width = min(180, (end - start) // 2)
        if width < 1:
            raise ValueError("指标观察窗口不足")
        evidence: dict[str, dict[str, object]] = {}
        for at, limit in ((start, start + width), (end - width + 1, end + 1)):
            window = metrics.filter(f"time >= {at} AND time < {limit}")
            values = window.aggregate(", ".join(f'avg("{c}")' for c in columns)).fetchone()
            for column, value in zip(columns, values, strict=True):
                service, kind = column.split("_", 1)
                evidence.setdefault(service, {}).setdefault(kind, []).append(
                    round(value, 4) if value is not None else None
                )
        rows = db.execute(
            """
            WITH counts AS (
              SELECT container_name, left(message, 160) AS snippet, count(*) AS n
              FROM read_parquet(?)
              WHERE timestamp >= ? AND timestamp <= ?
              GROUP BY ALL
            )
            SELECT container_name, snippet, n FROM counts
            QUALIFY row_number() OVER (PARTITION BY container_name ORDER BY n DESC, snippet) <= 2
            ORDER BY container_name, n DESC, snippet
            """,
            [str(directory / "logs.parquet"), end - width + 1, end],
        ).fetchall()
        for service, snippet, count in rows:
            if service in evidence:
                evidence[service].setdefault("late_log_samples", []).append([count, snippet])
        return evidence


def load_batch(manifest: Path):
    batch = json.loads(manifest.read_text(encoding="utf-8-sig"))
    for case in batch["cases"]:
        target = fetch_case(batch["dataset"], batch["revision"], case["case_id"], case["files"])
        yield case["case_id"], case["expected_service"], load_evidence(target)
