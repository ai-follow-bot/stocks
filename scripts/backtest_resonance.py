"""
板块共振系统回测脚本

按 SPEC §9 设计：利用历史财联社数据（2026-04-19 至今）回测。

用法:
    .venv/bin/python scripts/backtest_resonance.py                          # 全量回测
    .venv/bin/python scripts/backtest_resonance.py --start 2026-06-01        # 指定起始日期
    .venv/bin/python scripts/backtest_resonance.py --days 30                 # 回测最近30天
    .venv/bin/python scripts/backtest_resonance.py --no-evolve               # 不模拟自进化（只运行Agent1+2）
    .venv/bin/python scripts/backtest_resonance.py --json                    # JSON输出
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skills.daily_resonance.config import (
    INITIAL_WEIGHTS,
    CONVERGENCE_DAYS,
    CONVERGENCE_DELTA,
    CONVERGENCE_MIN_ACCURACY,
    REGIME_CHANGE_DAYS,
    REGIME_CHANGE_THRESHOLD,
    OUTPUT_DIR,
)
from skills.daily_resonance.data import (
    load_news_for_date,
    load_ecosystem,
    get_sector_name,
)
from skills.daily_resonance.agent1_classify import classify_events
from skills.daily_resonance.agent2_resonance import (
    compute_resonance,
    get_top_k,
    get_top3_sectors,
)
from skills.daily_resonance.evolution import (
    process_feedback,
    _init_state,
    _compute_accuracy_from_market,
)


def get_available_dates(start_str: str = None, max_days: int = None) -> list[str]:
    """获取有新闻数据的日期列表（按时间排序）"""
    hermes_dir = Path("/root/.hermes/data/investment-research/news")
    if not hermes_dir.exists():
        print(f"[回测] ❌ Hermes目录不存在: {hermes_dir}", file=sys.stderr)
        return []

    dates = []
    for d in sorted(hermes_dir.iterdir()):
        if not d.is_dir():
            continue
        date_str = d.name
        # 只处理 YYYY-MM-DD 格式的目录
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        # 检查是否有新闻文件
        news_file = d / f"news_{date_str}.json"
        if news_file.exists():
            dates.append(date_str)

    # 过滤起始日期
    if start_str:
        dates = [d for d in dates if d >= start_str]

    # 限制天数
    if max_days and len(dates) > max_days:
        dates = dates[:max_days]

    return dates


def compute_accuracy_simple(top3_sectors: list[str], date: str) -> float:
    """
    简化版准确率计算。
    如果akshare数据不可用，返回None。
    """
    try:
        return _compute_accuracy_from_market(top3_sectors, date, date)
    except Exception as e:
        return None


def run_backtest(dates: list[str], no_evolve: bool = False, verbose: bool = True):
    """运行回测"""
    results = []
    state = _init_state()
    accuracy_available = False

    print(f"[回测] 开始回测 {len(dates)} 天: {dates[0]} ~ {dates[-1]}", file=sys.stderr)
    print(f"[回测] 初始权重: {[f'{w:.3f}' for w in INITIAL_WEIGHTS]}", file=sys.stderr)
    print(file=sys.stderr)

    for idx, date in enumerate(dates):
        if verbose:
            print(f"  [{idx+1}/{len(dates)}] {date}...", end=" ", file=sys.stderr)

        # ── 加载新闻 ──
        news_list = load_news_for_date(date)
        if not news_list:
            if verbose:
                print(f"无新闻", file=sys.stderr)
            continue

        # ── Agent 1: 分类 ──
        sector_events = classify_events(news_list)
        if not sector_events:
            if verbose:
                print(f"无板块映射", file=sys.stderr)
            continue

        # ── Agent 2: 共振计算 ──
        history = state.get("sector_daily_counts", {})
        resonance_results = compute_resonance(sector_events, state["weights"], history)

        if not resonance_results:
            if verbose:
                print(f"共振无结果", file=sys.stderr)
            continue

        top3 = get_top3_sectors(resonance_results)
        top5 = get_top_k(resonance_results, 5)

        # ── 自进化: 反馈学习 ──
        if not no_evolve:
            state = process_feedback(state, date, top3, resonance_results)

        # ── 准确率计算 ──
        acc = None
        if state.get("accuracy_history"):
            acc = state["accuracy_history"][-1]
            if acc is not None:
                accuracy_available = True

        # ── 记录结果 ──
        result_entry = {
            "date": date,
            "top3": top3,
            "top5": [r["sector"] for r in top5],
            "top5_scores": {r["sector"]: r["score"] for r in top5},
            "accuracy": acc,
            "weights": list(state["weights"]),
            "days_run": state["days_run"],
            "converged": state.get("converged", False),
            "total_news": len(news_list),
            "mapped_sectors": len(sector_events),
        }
        results.append(result_entry)

        if verbose:
            top3_names = [
                get_sector_name(s, load_ecosystem()) for s in top3
            ]
            acc_str = f"acc={acc:.2f}" if acc is not None else "acc=N/A"
            conv_str = "✅" if state.get("converged") else " "
            print(f"{top3_names} {acc_str} {conv_str}w[{state['days_run']}]",
                  file=sys.stderr)

    return results, state


def summarize_results(results: list[dict], state: dict):
    """汇总回测结果"""
    if not results:
        return {
            "status": "empty",
            "total_dates_run": 0,
            "message": "没有可回测的数据",
        }

    # 准确率统计
    accuracies = [r["accuracy"] for r in results if r["accuracy"] is not None]
    avg_accuracy = sum(accuracies) / len(accuracies) if accuracies else None

    # 收敛信息
    converged = state.get("converged", False)
    converged_at = state.get("converged_at", None)
    days_run = state.get("days_run", 0)

    # 权重变化
    weight_history = state.get("weight_history", [])
    weight_stability = None
    if len(weight_history) >= 5:
        recent = weight_history[-5:]
        variances = []
        for i in range(len(recent[0])):
            vals = [w[i] for w in recent]
            mean = sum(vals) / len(vals)
            var = sum((v - mean) ** 2 for v in vals) / len(vals)
            variances.append(var)
        weight_stability = sum(variances) / len(variances)

    # 收敛统计
    if converged:
        convergence_days = None
        for r in results:
            if r.get("converged"):
                convergence_days = r["days_run"]
                break
    else:
        convergence_days = None

    # 准确率趋势
    accuracy_trend = None
    if len(accuracies) >= 10:
        first_half = sum(accuracies[:len(accuracies)//2]) / (len(accuracies)//2)
        second_half = sum(accuracies[len(accuracies)//2:]) / (len(accuracies) - len(accuracies)//2)
        accuracy_trend = {
            "first_half_avg": round(first_half, 3),
            "second_half_avg": round(second_half, 3),
            "improvement": round(second_half - first_half, 3),
        }

    return {
        "status": "completed",
        "total_dates_run": len(results),
        "date_range": {
            "start": results[0]["date"],
            "end": results[-1]["date"],
        },
        "accuracy": {
            "mean": round(avg_accuracy, 3) if avg_accuracy is not None else None,
            "available_count": len(accuracies),
            "total_count": len(results),
            "values": [round(a, 3) for a in accuracies[:50]] if accuracies else [],
            "trend": accuracy_trend,
        },
        "convergence": {
            "converged": converged,
            "converged_at": converged_at,
            "days_run": days_run,
            "convergence_days": convergence_days,
        },
        "weights": {
            "final": [round(w, 4) for w in state.get("weights", [])],
            "initial": [round(w, 4) for w in INITIAL_WEIGHTS],
            "stability": round(weight_stability, 6) if weight_stability is not None else None,
            "history": [[round(w, 4) for w in wh] for wh in weight_history],
        },
        "top3_frequency": _compute_top3_frequency(results),
    }


def _compute_top3_frequency(results: list[dict]) -> dict:
    """统计TOP3板块出现频率"""
    from collections import Counter
    counter = Counter()
    for r in results:
        for s in r.get("top3", []):
            counter[s] += 1
    total = len(results) * 3 if results else 1
    freq = {}
    for sector, count in counter.most_common(20):
        ecosystem = load_ecosystem()
        name = get_sector_name(sector, ecosystem)
        freq[sector] = {
            "name": name,
            "count": count,
            "frequency": round(count / total, 3),
        }
    return freq


def main():
    parser = argparse.ArgumentParser(
        description="板块共振系统回测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--start", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--days", type=int, help="回测天数")
    parser.add_argument("--no-evolve", action="store_true", help="不模拟自进化")
    parser.add_argument("--json", action="store_true", help="JSON输出")
    parser.add_argument("--verbose", action="store_true", help="详细输出")
    args = parser.parse_args()

    # ── 获取日期列表 ──
    dates = get_available_dates(args.start, args.days)
    if not dates:
        print("[回测] ❌ 没有可回测的数据", file=sys.stderr)
        sys.exit(1)

    print(f"[回测] 找到 {len(dates)} 个有新闻数据的日期", file=sys.stderr)
    print(f"[回测] 日期范围: {dates[0]} ~ {dates[-1]}", file=sys.stderr)
    print(file=sys.stderr)

    # ── 运行回测 ──
    start_time = time.time()
    results, state = run_backtest(dates, args.no_evolve, args.verbose)
    elapsed = time.time() - start_time

    # ── 汇总 ──
    summary = summarize_results(results, state)

    # ── 输出 ──
    if args.json:
        output = {
            "meta": {
                "total_dates_available": len(dates),
                "elapsed_seconds": round(elapsed, 1),
                "no_evolve": args.no_evolve,
            },
            **summary,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"  板块共振系统回测报告")
        print(f"{'='*60}")
        print(f"  回测期间: {summary['date_range']['start']} ~ {summary['date_range']['end']}")
        print(f"  运行天数: {summary['total_dates_run']} / {len(dates)} 可用")
        print(f"  耗时: {elapsed:.1f}s")
        print()

        acc = summary["accuracy"]
        print(f"  📊 准确率")
        print(f"     均值: {acc['mean']}" if acc['mean'] else "     均值: N/A")
        print(f"     有数据的样本: {acc['available_count']}/{acc['total_count']}")
        if acc.get("trend"):
            t = acc["trend"]
            arrow = "↑" if t["improvement"] > 0 else "↓"
            print(f"     趋势: 前半{round(t['first_half_avg'],3)} → 后半{round(t['second_half_avg'],3)} {arrow}")

        conv = summary["convergence"]
        print(f"\n  🎯 收敛状态")
        print(f"     {'✅ 已收敛' if conv['converged'] else '⏳ 未收敛'} (运行{conv['days_run']}天)")
        if conv["converged"]:
            print(f"     收敛于: {conv['converged_at']} (第{conv['convergence_days']}天)")

        w = summary["weights"]
        print(f"\n  ⚖️ 权重")
        print(f"     初始: {w['initial']}")
        print(f"     最终: {w['final']}")
        if w["stability"] is not None:
            print(f"     稳定性(近5天方差): {w['stability']}")

        print(f"\n  🔝 TOP3板块频率 (前10)")
        freq = summary["top3_frequency"]
        for i, (sector, info) in enumerate(list(freq.items())[:10]):
            print(f"     {i+1:2d}. {info['name']:<14s} {info['count']:3d}次 ({info['frequency']:.1%})")

        print(f"{'='*60}")

    # 保存回测结果
    output_dir = OUTPUT_DIR
    summary_path = output_dir / "backtest_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n[回测] 结果已保存: {summary_path}", file=sys.stderr)

    # 持久化进化状态（供单日运行接力）
    from skills.daily_resonance.evolution import save_state
    save_state(state)


if __name__ == "__main__":
    main()
