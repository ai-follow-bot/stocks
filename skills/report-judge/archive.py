"""report-judge 独立 archive（SPEC §9）：评判结果存档 + 聚合统计。

output/report_judge_archive.json:
  {<filename>: {quality_score, total_score, dimensions, cross_path_conflicts,
                suggestions, judged_at, llm_provider, task_type, sector, task_id,
                data_quality, report_mtime}}

key 用 filename（basename，不含目录），与前端报告列表的 filename 对齐。
不碰 valuation_stock_archive / cycle_archive / deep archive。
"""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from chain_agent import config

ARCHIVE_PATH = Path(os.environ.get("REPORT_JUDGE_ARCHIVE")) if os.environ.get("REPORT_JUDGE_ARCHIVE") else config.OUTPUT_DIR / "report_judge_archive.json"


def _load() -> dict:
    try:
        if ARCHIVE_PATH.exists():
            return json.loads(ARCHIVE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[report-judge] archive 读取失败: {e}", file=sys.stderr)
    return {}


def _save(arc: dict):
    try:
        ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ARCHIVE_PATH.write_text(json.dumps(arc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as e:
        print(f"[report-judge] archive 写入失败: {e}", file=sys.stderr)


def load_judgment(filename: str) -> dict:
    """读单份评判结果，无则返回 {}。filename 是 basename。"""
    return _load().get(filename, {})


def load_all() -> dict:
    """读全部 {filename: entry}。"""
    return _load()


def upsert_judgment(filename: str, judgment: dict, task_meta: dict) -> dict:
    """存/更新一份评判结果。返回写入的 entry。

    filename: basename（与前端列表对齐）。
    judgment: judge_report 的输出。
    task_meta: extract_task_meta 的输出（task_type/sector/data_quality/llm_model/task_id）。

    稳定性：一篇文章保持一份评判。若本次重判失败（quality_score=None）但已有
    旧的好评判，保留旧的，只记 last_error/last_attempted_at；失败且无旧评判才
    写入失败态（前端可知「已尝试但失败」而非「未评判」）。
    """
    arc = _load()
    existing = arc.get(filename, {})
    failed = judgment.get("quality_score") is None

    if failed and existing.get("quality_score") is not None:
        # 有旧的好评判 -> 保留，仅记本次失败
        entry = dict(existing)
        entry["last_error"] = judgment.get("error")
        entry["last_attempted_at"] = judgment.get("judged_at")
    else:
        # 成功（覆盖旧的）或失败且无旧评判（记录失败态）
        entry = {
            "quality_score": judgment.get("quality_score"),
            "total_score": judgment.get("total_score"),
            "dimensions": judgment.get("dimensions") or [],
            "cross_path_conflicts": judgment.get("cross_path_conflicts") or [],
            "suggestions": judgment.get("suggestions") or [],
            "judged_at": judgment.get("judged_at"),
            "llm_provider": judgment.get("llm_provider"),
            "error": judgment.get("error"),
            "task_type": (task_meta or {}).get("task_type"),
            "sector": (task_meta or {}).get("sector"),
            "task_id": (task_meta or {}).get("task_id"),
            "data_quality": (task_meta or {}).get("data_quality"),
            "llm_model": (task_meta or {}).get("llm_model"),
            "days": (task_meta or {}).get("days"),
            "market": (task_meta or {}).get("market"),
        }
    arc[filename] = entry
    _save(arc)
    return entry


def aggregate_stats(limit: int = 50) -> dict:
    """聚合最近 limit 份评判（按 judged_at 倒序）。

    返回 {
        count, avg_score, score_dist: {A,B,C,D},
        top_issues: [{type, count, examples}],
        by_task_type: {task_type: {avg, count}},
        by_sector: {sector: {avg, count}},
        trend: [{date, avg}],  # 按天均分（ judged_at 的日期）
    }
    """
    arc = _load()
    entries = [v for v in arc.values() if v.get("total_score") is not None]
    # 按 judged_at 倒序取最近 limit 份
    entries.sort(key=lambda e: e.get("judged_at") or "", reverse=True)
    entries = entries[:limit] if limit and limit > 0 else entries

    if not entries:
        return {
            "count": 0,
            "avg_score": 0,
            "score_dist": {"A": 0, "B": 0, "C": 0, "D": 0},
            "top_issues": [],
            "by_task_type": {},
            "by_sector": {},
            "trend": [],
        }

    scores = [e["total_score"] for e in entries]
    avg = round(sum(scores) / len(scores), 1)

    dist = {"A": 0, "B": 0, "C": 0, "D": 0}
    for e in entries:
        g = e.get("quality_score")
        if g in dist:
            dist[g] += 1

    # issues 按维度 key 聚合（type = 维度 key）
    issue_map = defaultdict(lambda: {"count": 0, "examples": []})
    for e in entries:
        for d in e.get("dimensions") or []:
            key = d.get("key", "unknown")
            for issue in d.get("issues") or []:
                if not issue:
                    continue
                m = issue_map[key]
                m["count"] += 1
                if len(m["examples"]) < 3:
                    m["examples"].append(issue)
    top_issues = sorted(
        [{"type": k, "count": v["count"], "examples": v["examples"]} for k, v in issue_map.items()],
        key=lambda x: x["count"],
        reverse=True,
    )

    # 按 task_type / sector 聚合
    by_task = defaultdict(list)
    by_sector = defaultdict(list)
    for e in entries:
        if e.get("task_type"):
            by_task[e["task_type"]].append(e["total_score"])
        if e.get("sector"):
            by_sector[e["sector"]].append(e["total_score"])
    by_task_type = {
        k: {"avg": round(sum(v) / len(v), 1), "count": len(v)}
        for k, v in by_task.items()
    }
    by_sector = {
        k: {"avg": round(sum(v) / len(v), 1), "count": len(v)}
        for k, v in by_sector.items()
    }

    # 按天趋势
    day_map = defaultdict(list)
    for e in entries:
        ts = e.get("judged_at") or ""
        day = ts[:10]  # YYYY-MM-DD
        if day:
            day_map[day].append(e["total_score"])
    trend = sorted(
        [{"date": k, "avg": round(sum(v) / len(v), 1), "count": len(v)} for k, v in day_map.items()],
        key=lambda x: x["date"],
    )

    return {
        "count": len(entries),
        "avg_score": avg,
        "score_dist": dist,
        "top_issues": top_issues,
        "by_task_type": by_task_type,
        "by_sector": by_sector,
        "trend": trend,
    }
