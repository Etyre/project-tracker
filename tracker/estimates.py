"""Learn how long tasks really take, so the scheduler's estimates get better over time."""
from __future__ import annotations

import statistics

from . import config, db


def actuals() -> list[dict]:
    """Completed tasks that had an estimate and some tracked time."""
    return db.rows("""
        SELECT t.id, t.title, t.project_id, t.estimate_minutes AS est,
               SUM(COALESCE(e.stopped_at, strftime('%s','now')) - e.started_at)/60.0 AS actual
        FROM tasks t JOIN time_entries e ON e.task_id=t.id
        WHERE t.completed_at IS NOT NULL AND t.estimate_minutes IS NOT NULL AND t.estimate_minutes > 0
        GROUP BY t.id HAVING actual >= 2
        ORDER BY t.completed_at DESC LIMIT 200""")


def calibration() -> dict:
    """Median actual/estimate ratio, overall and per top-level project (min 5 samples each)."""
    rows = actuals()
    ratios = [r["actual"] / r["est"] for r in rows]
    overall = statistics.median(ratios) if len(ratios) >= 5 else 1.0
    by_project: dict[int, float] = {}
    buckets: dict[int, list[float]] = {}
    for r in rows:
        root = db.project_path(r["project_id"])
        key = db.find_project_by_path(root[:1]) if root else None
        if key is not None:
            buckets.setdefault(key, []).append(r["actual"] / r["est"])
    for k, v in buckets.items():
        if len(v) >= 5:
            by_project[k] = statistics.median(v)
    return {"overall": round(overall, 2), "by_root_project": by_project, "samples": len(ratios),
            "median_actual_minutes": round(statistics.median([r["actual"] for r in rows]), 1) if rows else None}


def expected_minutes(task: dict, cal: dict | None = None) -> int:
    cal = cal or calibration()
    est = task.get("estimate_minutes") or config.load()["general"]["default_estimate_minutes"]
    factor = cal["overall"]
    root = db.project_path(task["project_id"])
    key = db.find_project_by_path(root[:1]) if root else None
    if key in cal["by_root_project"]:
        factor = cal["by_root_project"][key]
    factor = min(max(factor, 0.5), 3.0)
    done = db.task_seconds(task["id"]) / 60.0
    return max(5, int(round(est * factor - done)))


def summary() -> dict:
    cal = calibration()
    recent = actuals()[:30]
    return {**cal, "recent": [{"id": r["id"], "title": r["title"], "estimate": r["est"], "actual": round(r["actual"])}
                              for r in recent]}
