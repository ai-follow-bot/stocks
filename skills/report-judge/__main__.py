"""report-judge CLI（SPEC §10）。

用法:
  # 评判单份
  python -m skills.report-judge --file output/ban-dao-ti-cai-liao_20260712-100218_92.md --json

  # 批量评判最近 N 份未评判的
  python -m skills.report-judge --batch --limit 20

  # 聚合统计
  python -m skills.report-judge --stats
"""

import argparse
import json
import re
import sys
from pathlib import Path

from chain_agent import config

from . import judge
from . import archive

# 报告文件名模式：<prefix>_<YYYYMMDD-HHMMSS>_<task_id>.<md|json>
# 用它区分真报告与 archive/tavily 等 JSON，避免误判
_REPORT_NAME_RE = re.compile(r"_\d{8}-\d{6}_\d+\.(?:md|json)$")


def _print_summary(judgment: dict, meta: dict):
    """人类可读的评判摘要（stdout）。"""
    grade = judgment.get("quality_score")
    total = judgment.get("total_score")
    if grade is None:
        print(f"❌ 评判失败: {judgment.get('error')}")
        return
    color = {"A": "🟢", "B": "🔵", "C": "🟠", "D": "🔴"}.get(grade, "⚪")
    print(f"{color} 质量等级 {grade}（总分 {total}/100）"
          f"  provider={judgment.get('llm_provider')}  judged={judgment.get('judged_at','')[:19]}")
    if meta:
        print(f"   元数据: task_type={meta.get('task_type')} sector={meta.get('sector')} "
              f"data_quality={meta.get('data_quality')} llm_model={meta.get('llm_model')}")
    print("   维度:")
    for d in judgment.get("dimensions") or []:
        issues = d.get("issues") or []
        flag = " ⚠" + "; ".join(issues) if issues else ""
        print(f"     - {d.get('name')}({d.get('key')}): {d.get('score')}  {d.get('reason','')}{flag}")
    if judgment.get("cross_path_conflicts"):
        print("   跨视角冲突:")
        for c in judgment["cross_path_conflicts"]:
            print(f"     - {c}")
    if judgment.get("suggestions"):
        print("   改进建议:")
        for s in judgment["suggestions"]:
            print(f"     - {s}")


def _print_stats(stats: dict):
    """人类可读的聚合统计（stdout）。"""
    print(f"=== 评判统计（最近 {stats.get('count',0)} 份）===")
    print(f"平均分: {stats.get('avg_score',0)}")
    dist = stats.get("score_dist", {})
    print(f"等级分布: A={dist.get('A',0)} B={dist.get('B',0)} C={dist.get('C',0)} D={dist.get('D',0)}")
    by_task = stats.get("by_task_type", {})
    if by_task:
        print("按任务类型:")
        for k, v in sorted(by_task.items(), key=lambda x: x[1].get("avg", 0), reverse=True):
            print(f"  - {k}: 均分 {v.get('avg')} ({v.get('count')} 份)")
    by_sector = stats.get("by_sector", {})
    if by_sector:
        print("按板块:")
        for k, v in sorted(by_sector.items(), key=lambda x: x[1].get("avg", 0), reverse=True):
            print(f"  - {k}: 均分 {v.get('avg')} ({v.get('count')} 份)")
    top_issues = stats.get("top_issues", [])
    if top_issues:
        print("高频问题:")
        for it in top_issues:
            print(f"  - [{it.get('type')}] x{it.get('count')}: {it.get('examples',[''])[0]}")
    trend = stats.get("trend", [])
    if trend:
        print("按天趋势:")
        for t in trend:
            print(f"  - {t.get('date')}: 均分 {t.get('avg')} ({t.get('count')} 份)")


def run_single(filepath: str, as_json: bool) -> int:
    meta = judge.extract_task_meta(filepath)
    judgment = judge.judge_report(filepath, task_meta=meta)
    # 始终存档（失败也存 error，便于统计/排查）
    try:
        archive.upsert_judgment(Path(filepath).name, judgment, meta)
    except Exception as e:
        print(f"[report-judge] 存档失败: {e}", file=sys.stderr)
    if as_json:
        print(json.dumps(judgment, ensure_ascii=False, indent=2))
    else:
        _print_summary(judgment, meta)
    return 1 if judgment.get("quality_score") is None and not judgment.get("error") is None else 0


def run_batch(limit: int, as_json: bool) -> int:
    out_dir = config.OUTPUT_DIR
    judged = archive.load_all()
    files = []
    for f in out_dir.iterdir():
        if not f.is_file():
            continue
        # 只挑符合报告命名模式的文件，跳过 archive/tavily 等 JSON
        if not _REPORT_NAME_RE.search(f.name):
            continue
        # 跳过已评判的（幂等；如需重判用 --file 单份）
        if f.name in judged:
            continue
        files.append(f)
    # 按 mtime 倒序取最近 limit 份
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if limit and limit > 0:
        files = files[:limit]
    if not files:
        print("[report-judge] 无未评判报告", file=sys.stderr)
        if as_json:
            print("[]")
        return 0
    print(f"[report-judge] 待评判 {len(files)} 份", file=sys.stderr)
    results = []
    fail = 0
    for i, fp in enumerate(files, 1):
        print(f"[report-judge] ({i}/{len(files)}) {fp.name}", file=sys.stderr)
        meta = judge.extract_task_meta(fp)
        judgment = judge.judge_report(str(fp), task_meta=meta)
        try:
            archive.upsert_judgment(fp.name, judgment, meta)
        except Exception as e:
            print(f"[report-judge] 存档失败 {fp.name}: {e}", file=sys.stderr)
        results.append({"filename": fp.name, "judgment": judgment})
        if judgment.get("quality_score") is None:
            fail += 1
        else:
            print(f"   -> {judgment.get('quality_score')} ({judgment.get('total_score')})", file=sys.stderr)
    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"\n=== 批量完成: {len(results)} 份，失败 {fail} ===")
        for r in results:
            j = r["judgment"]
            g = j.get("quality_score") or "✗"
            print(f"  {g}  {r['filename']}")
    return 0


def run_stats(as_json: bool, limit: int) -> int:
    stats = archive.aggregate_stats(limit=limit)
    if as_json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        _print_stats(stats)
    return 0


def main():
    ap = argparse.ArgumentParser(prog="skills.report-judge", description="报告质量评判")
    ap.add_argument("--file", help="评判单份报告（.md 或 .json）")
    ap.add_argument("--batch", action="store_true", help="批量评判最近未评判的报告")
    ap.add_argument("--limit", type=int, default=20, help="批量/统计的份数上限（默认 20）")
    ap.add_argument("--stats", action="store_true", help="输出聚合统计")
    ap.add_argument("--json", action="store_true", help="输出 JSON 到 stdout")
    args = ap.parse_args()

    if args.stats:
        sys.exit(run_stats(args.json, args.limit))
    if args.batch:
        sys.exit(run_batch(args.limit, args.json))
    if args.file:
        sys.exit(run_single(args.file, args.json))
    ap.error("必须指定 --file / --batch / --stats 之一")


if __name__ == "__main__":
    main()
