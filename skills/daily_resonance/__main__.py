"""
主入口：每日板块共振自进化系统

每天早晨8点运行，处理昨日全天新闻，输出今日共振预判。

用法:
    # 运行今日共振（处理昨日新闻）
    .venv/bin/python -m skills.daily_resonance

    # 指定日期（回测用）
    .venv/bin/python -m skills.daily_resonance --date 2026-07-13

    # 跳过自进化
    .venv/bin/python -m skills.daily_resonance --no-evolve

    # 输出JSON
    .venv/bin/python -m skills.daily_resonance --json

    # 不使用LLM（模板报告）
    .venv/bin/python -m skills.daily_resonance --no-llm
"""
import argparse
import json
import sys
from datetime import datetime

from .config import OUTPUT_DIR
from .data import (
    get_date_for_run,
    load_yesterday_news,
    load_ecosystem,
    get_sector_name,
)
from .agent1_classify import classify_events
from .agent2_resonance import compute_resonance, get_top_k, get_top3_sectors
from .agent3_report import generate_report
from .evolution import load_state, save_state, process_feedback


def main():
    parser = argparse.ArgumentParser(
        description="每日板块共振自进化系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m skills.daily_resonance                    # 运行今日
  python -m skills.daily_resonance --date 2026-07-13  # 指定日期（回测）
  python -m skills.daily_resonance --json              # JSON输出
  python -m skills.daily_resonance --no-evolve         # 跳过自进化
  python -m skills.daily_resonance --no-llm            # 模板报告
        """,
    )
    parser.add_argument("--date", help="指定日期 YYYY-MM-DD")
    parser.add_argument("--json", action="store_true", help="输出JSON格式")
    parser.add_argument("--no-evolve", action="store_true", help="跳过自进化")
    parser.add_argument("--no-llm", action="store_true", help="不使用LLM，生成模板报告")

    args = parser.parse_args()

    # ── 确定运行日期 ──
    target_date = get_date_for_run(args.date)
    print(f"[系统] 运行日期: {target_date}", file=sys.stderr)

    # ── 加载新闻 ──
    if args.date:
        # 指定日期：加载当天的新闻
        from .data import load_news_for_date
        news_list = load_news_for_date(target_date)
    else:
        # 默认：加载昨天的新闻（早上8点运行）
        news_list = load_yesterday_news()

    if not news_list:
        print(f"[系统] ❌ 未找到 {target_date} 的新闻数据，退出", file=sys.stderr)
        sys.exit(1)

    # ── 加载进化状态 ──
    state = load_state()
    weights = state.get("weights")

    if state.get("converged"):
        print(f"[系统] ✅ 系统已收敛（第{state['days_run']}天），"
              f"权重=[{', '.join(f'{w:.3f}' for w in weights)}]",
              file=sys.stderr)

    # ── Agent 1: 事件分类与板块映射 ──
    print(f"[系统] Agent 1: 分类 {len(news_list)} 条新闻...", file=sys.stderr)
    sector_events = classify_events(news_list)

    if not sector_events:
        print("[系统] ⚠️ 没有新闻映射到任何板块，输出空结果", file=sys.stderr)
        _output_empty(target_date, args.json)
        sys.exit(0)

    # ── Agent 2: 共振计算 ──
    print(f"[系统] Agent 2: 计算 {len(sector_events)} 个板块的共振...", file=sys.stderr)
    history = state.get("sector_daily_counts", {})
    resonance_results = compute_resonance(sector_events, weights, history)

    if not resonance_results:
        print("[系统] ⚠️ 共振计算无结果", file=sys.stderr)
        _output_empty(target_date, args.json)
        sys.exit(0)

    # ── 自进化: 反馈学习（非跳过时） ──
    top3 = get_top3_sectors(resonance_results)

    if not args.no_evolve:
        print(f"[系统] 自进化: 处理反馈...", file=sys.stderr)
        state = process_feedback(state, target_date, top3, resonance_results)
        save_state(state)

    # ── Agent 3: 报告生成 ──
    print(f"[系统] Agent 3: 生成报告...", file=sys.stderr)
    use_llm = not args.no_llm
    report = generate_report(
        resonance_results, sector_events, target_date, use_llm=use_llm
    )

    # ── 保存输出 ──
    top10 = get_top_k(resonance_results, 10)

    # JSON输出
    output_json = {
        "date": target_date,
        "total_news": len(news_list),
        "mapped_sectors": len(sector_events),
        "converged": state.get("converged", False),
        "days_run": state.get("days_run", 0),
        "weights": state.get("weights"),
        "top10": [
            {
                "rank": i + 1,
                "sector": r["sector"],
                "name": r["name"],
                "score": r["score"],
                "dimensions": r["dimensions"],
                "stats": r["stats"],
            }
            for i, r in enumerate(top10)
        ],
    }

    json_path = OUTPUT_DIR / f"resonance_{target_date}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output_json, f, ensure_ascii=False, indent=2)
    print(f"[系统] JSON已保存: {json_path}", file=sys.stderr)

    # Markdown报告
    md_path = OUTPUT_DIR / f"resonance_{target_date}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"[系统] 报告已保存: {md_path}", file=sys.stderr)

    # ── 输出到stdout ──
    if args.json:
        print(json.dumps(output_json, ensure_ascii=False, indent=2))
    else:
        # 打印简要摘要
        print(f"\n{'='*60}")
        print(f"  每日板块共振简报 — {target_date}")
        print(f"{'='*60}")
        print(f"  新闻总数: {len(news_list)}")
        print(f"  映射板块: {len(sector_events)}")
        print(f"  系统状态: {'已收敛' if state.get('converged') else '学习期'}"
              f" (第{state['days_run']}天)")
        print(f"  权重: [{', '.join(f'{w:.3f}' for w in weights)}]")
        print(f"\n  TOP10 共振板块:")
        for i, r in enumerate(top10):
            print(f"  {i+1:2d}. {r['name']:<12s} {r['score']:5.1f}分  "
                  f"(事件{r['stats']['total_events']}条)")
        print(f"{'='*60}")
        print(f"\n  完整报告: {md_path}")
        print(f"  JSON数据: {json_path}")
        print(f"\n  报告预览（前30行）:")
        print(f"{'-'*60}")
        for line in report.split("\n")[:30]:
            print(f"  {line}")
        if report.count("\n") > 30:
            print(f"  ... (共{report.count(chr(10))+1}行)")
        print(f"{'-'*60}")


def _output_empty(date: str, as_json: bool):
    """输出空结果"""
    empty = {"date": date, "total_news": 0, "mapped_sectors": 0, "top10": []}
    if as_json:
        print(json.dumps(empty, ensure_ascii=False, indent=2))
    else:
        print(f"[系统] {date}: 无数据")


if __name__ == "__main__":
    main()
