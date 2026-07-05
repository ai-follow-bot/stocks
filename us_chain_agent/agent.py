#!/usr/bin/env python3
"""
美股产业链投资挖掘 Agent - 主入口

5 层 pipeline（与 chain_agent.agent 对齐）:
  us_chain_graph.expand_chain → orchestrator_us.collect_all
    → candidates_us.discover_candidates → integrator_us.score_candidates → llm.synthesize

用法:
  python -m us_chain_agent.agent semiconductors                       # 纯 Python 报告
  python -m us_chain_agent.agent semiconductors --llm                 # 加 LLM 综合
  python -m us_chain_agent.agent --sectors semiconductors,ai_cloud    # 多板块批量
  python -m us_chain_agent.agent semiconductors --json --out x.json
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from us_chain_agent import config
from chain_agent import config as cn_cfg  # to_under / to_hyphen
from us_chain_agent import chain_graph as us_chain_graph
from us_chain_agent.collectors import orchestrator_us as collectors_us
from chain_agent.collectors.tavily_search import TavilySearch  # 复用 A 股 Tavily
from us_chain_agent.discovery import candidates_us as discovery_us
from us_chain_agent.scoring import integrator_us as scoring_us
from us_chain_agent.scoring.quotes_us import get_quote_provider
from us_chain_agent.llm.prompts_us import SYNTHESIS_SYSTEM, SYNTHESIS_USER_TEMPLATE

from chain_agent.llm.client import get_llm_client
from chain_agent import chain_graph  # 用 render_chain_text
from chain_agent.scoring.integrator import render_scored_text


def _get_sector_leaders(sector: str):
    """从 us_sector_overflow_config.json 取该板块龙头代码列表（供 Finnhub 新闻层用）"""
    import json as _json
    if not config.US_OVERFLOW_CONFIG_JSON.exists():
        return None
    with open(config.US_OVERFLOW_CONFIG_JSON, "r", encoding="utf-8") as f:
        all_cfg = _json.load(f)
    sec_cfg = all_cfg.get(sector.replace("_", "-"), {})
    codes = [s["code"] for s in sec_cfg.get("leaders", []) if s.get("code")]
    # 加二线，让新闻覆盖更广
    codes += [s["code"] for s in sec_cfg.get("second_tier", []) if s.get("code")][:4]
    return codes or None


def run_pipeline(sector: str, days: int = 7, tavily_results: int = 10) -> dict:
    """运行完整的 4 层 pipeline（不含 LLM）"""
    print(f"[1/4] 展开美股产业链: {sector}", file=sys.stderr)
    chain = us_chain_graph.expand_chain(sector)
    if chain.get("error"):
        raise RuntimeError(f"产业链展开失败: {chain['error']}")

    leaders = _get_sector_leaders(sector)
    print(f"[2/4] 双轨采集 (Tavily + Finnhub, days={days}, leaders={leaders})", file=sys.stderr)
    collected = collectors_us.collect_all(
        sector, days=days, tavily_results=tavily_results, leader_codes=leaders
    )

    print(f"[3/4] 动态发现候选标的", file=sys.stderr)
    discovered = discovery_us.discover_candidates(sector, collected, chain)

    print(f"[4/4] 可投资性评分 (溢出 + 技术期权 + 启发式)", file=sys.stderr)
    tavily_for_scoring = None
    try:
        tavily_for_scoring = TavilySearch()
    except Exception:
        pass
    tavily_results_pool = collected.get("supply", {}).get("results", []) or []
    scored = scoring_us.score_candidates(
        sector, discovered,
        quote_provider=get_quote_provider(),
        tavily_search=tavily_for_scoring,
        tavily_results_pool=tavily_results_pool,
    )

    return {
        "sector": cn_cfg.to_under(sector),
        "market": "us",
        "run_time": datetime.now().isoformat(),
        "days": days,
        "chain": chain,
        "collected": collected,
        "discovered_stats": discovered["stats"],
        "scored": scored,
    }


def run_batch(sectors: list, days: int = 7, tavily_results: int = 10,
              top_n: int = 15, use_llm: bool = False, max_workers: int = 3) -> dict:
    """多板块并行 pipeline"""
    print(f"[batch] 并行跑 {len(sectors)} 个美股板块: {sectors} (max_workers={max_workers})", file=sys.stderr)
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(run_pipeline, s, days, tavily_results): s for s in sectors}
        for fut in as_completed(futs):
            sec = futs[fut]
            try:
                results[sec] = fut.result()
                print(f"[batch] ✓ {sec} 完成", file=sys.stderr)
            except Exception as e:
                print(f"[batch] ✗ {sec} 失败: {e}", file=sys.stderr)
                results[sec] = {"sector": sec, "error": str(e)}
    return {
        "sectors": sectors,
        "market": "us",
        "run_time": datetime.now().isoformat(),
        "days": days,
        "results": results,
    }


def llm_synthesize(result: dict, top_n: int = 15) -> str:
    """调用 LLM 生成最终投资分析报告"""
    client = get_llm_client()
    if client is None:
        return _fallback_report(result, top_n) + \
            "\n\n<!-- LLM 未启用（ANTHROPIC_API_KEY/OPENAI_API_KEY 均未设置），使用模板报告 -->\n"

    chain = result["chain"]
    coll = result["collected"]
    tavily_content = (coll["supply"].get("content_text") or "")[:20000]
    news_content = "\n\n".join(filter(None, [
        coll["demand"].get("content_text", ""),
        coll.get("demand_secondary", {}).get("content_text", ""),
    ]))[:20000]
    tavily_answer = coll["supply"].get("answer", "") or "(无)"
    tavily_count = len(coll["supply"].get("results", []))
    news_count = coll["demand"].get("news_count", 0)

    user_prompt = SYNTHESIS_USER_TEMPLATE.format(
        sector_name=chain.get("focus_name", result["sector"]),
        chain_text=chain_graph.render_chain_text(chain)[:3000],
        tavily_answer=tavily_answer,
        tavily_count=tavily_count,
        tavily_content=tavily_content or "(无)",
        days=result["days"],
        news_count=news_count,
        news_content=news_content or "(无)",
        top_n=top_n,
        scored_text=render_scored_text(result["scored"], top_n=top_n),
    )

    print(f"[LLM input] {len(user_prompt)} chars", file=sys.stderr)

    try:
        return client.synthesize(SYNTHESIS_SYSTEM, user_prompt) + "\n\n---\n\n> **免责声明：** " + config.DISCLAIMER_TEXT
    except Exception as e:
        return _fallback_report(result, top_n) + \
            f"\n\n<!-- LLM 调用失败: {e}，使用模板报告 -->\n"


def _fallback_report(result: dict, top_n: int = 15) -> str:
    """LLM 不可用时的模板报告"""
    chain = result["chain"]
    scored = result["scored"]
    coll = result["collected"]
    tavily_count = len(coll["supply"].get("results", []))
    tavily_err = coll["supply"].get("error")
    news_count = coll["demand"].get("news_count", 0)
    news_err = coll["demand"].get("error")

    lines = [
        f"# {chain.get('focus_name', result['sector'])} 美股产业链投资分析",
        f"\n*生成时间: {result['run_time']} | 数据窗口: 近 {result['days']} 天 | 市场: 美股*\n",
        "## 1. 产业链结构\n",
        chain_graph.render_chain_text(chain),
        "\n## 2. 数据采集概览\n",
        f"- Tavily 深度搜索: {tavily_count} 条结果" +
        (f" (错误: {tavily_err})" if tavily_err else ""),
        f"- Finnhub 个股新闻: {news_count} 条匹配" +
        (f" (错误: {news_err})" if news_err else ""),
        "\n## 3. 候选标的评分\n",
        render_scored_text(scored, top_n=top_n),
        "\n## 4. 数据缺口\n",
    ]
    gaps = []
    if tavily_err:
        gaps.append("- Tavily 不可用，缺少行业深度搜索数据")
    if news_err:
        gaps.append("- Finnhub 新闻读取异常")
    if not any(s["extras"] for s in scored["scored"][:5]):
        gaps.append("- 行情源未启用，缺少实时 PE/市值，无法精确计算龙头饱和度")
    if not gaps:
        gaps.append("- 暂无明显缺口")
    lines.extend(gaps)
    lines.extend([
        "\n## 5. 后续行动建议\n",
        "- 对 Top 5 标的做技术面确认（趋势/支撑/量能）",
        "- 核查新发现标的的板块归属是否准确",
        "- 跟踪近 7 天新闻中出现 ≥2 次的标的，等待回调买点",
        "",
        "---",
        "",
        f"> **免责声明：** {config.DISCLAIMER_TEXT}",
    ])
    return "\n".join(lines)


def render_batch_report(batch: dict, top_n: int = 10, use_llm: bool = False) -> str:
    """多板块汇总报告"""
    lines = [
        "# 美股产业链投资分析 - 多板块汇总",
        f"\n*生成时间: {batch['run_time']} | 数据窗口: 近 {batch['days']} 天 | 市场: 美股*",
        f"*板块: {', '.join(batch['sectors'])}*\n",
        "---\n",
    ]

    all_scored = []
    for sec, res in batch["results"].items():
        if res.get("error"):
            continue
        for s in res["scored"]["scored"][:top_n]:
            s = dict(s)
            s["from_sector"] = sec
            all_scored.append(s)
    all_scored.sort(key=lambda x: x["score"], reverse=True)

    lines.append("## 跨板块 Top 15 候选标的\n")
    lines.append("| 排名 | 代码 | 名称 | 来源板块 | 角色 | 分数 |")
    lines.append("|------|------|------|---------|------|------|")
    for i, s in enumerate(all_scored[:15], 1):
        lines.append(
            f"| {i} | {s['code']} | {s['name']} | {s['from_sector']} | "
            f"{s['role']} | {s['score']} |"
        )

    lines.extend(["\n---\n", "## 各板块详细分析\n"])
    for sec in batch["sectors"]:
        res = batch["results"].get(sec, {})
        if res.get("error"):
            lines.append(f"### {sec}\n\n❌ 失败: {res['error']}\n")
            continue
        lines.append(f"### {res['chain'].get('focus_name', sec)} ({sec})\n")
        if use_llm:
            lines.append(llm_synthesize(res, top_n=top_n))
        else:
            lines.append(_fallback_report(res, top_n=top_n))
        lines.append("\n---\n")

    lines.append(
        "\n---\n\n> **免责声明：** " + config.DISCLAIMER_TEXT
    )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="美股产业链投资挖掘 Agent")
    parser.add_argument("sector", nargs="?",
                        help="美股板块代码，如 semiconductors / ai_cloud / consumer_electronics（与 --sectors 互斥）")
    parser.add_argument("--sectors", type=str,
                        help="多板块批量，逗号分隔，如 semiconductors,ai_cloud")
    parser.add_argument("--days", type=int, default=7, help="数据回看窗口（默认 7 天）")
    parser.add_argument("--tavily-results", type=int, default=10, help="Tavily 搜索结果数")
    parser.add_argument("--top-n", type=int, default=15, help="评分展示前 N 名")
    parser.add_argument("--max-workers", type=int, default=3, help="多板块并行 worker 数")
    parser.add_argument("--llm", action="store_true",
                        help="启用 LLM 综合分析（需要 ANTHROPIC_API_KEY 或 OPENAI_API_KEY）")
    parser.add_argument("--out", type=str, help="输出到文件（默认仅打印）")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON 结果")
    args = parser.parse_args()

    if not args.sector and not args.sectors:
        parser.error("必须提供 sector 或 --sectors")

    # ===== 多板块批量 =====
    if args.sectors:
        sector_list = [s.strip() for s in args.sectors.split(",") if s.strip()]
        batch = run_batch(sector_list, days=args.days, tavily_results=args.tavily_results,
                          top_n=args.top_n, use_llm=args.llm, max_workers=args.max_workers)

        if args.json:
            slim = {
                "market": "us",
                "run_time": batch["run_time"],
                "days": batch["days"],
                "sectors": batch["sectors"],
                "per_sector": {},
            }
            for sec, res in batch["results"].items():
                if res.get("error"):
                    slim["per_sector"][sec] = {"error": res["error"]}
                else:
                    slim["per_sector"][sec] = {
                        "overflow_strength": res["scored"]["overflow_strength"],
                        "leader_saturation": res["scored"]["leader_saturation"],
                        "discovered_stats": res["discovered_stats"],
                        "top_candidates": res["scored"]["scored"][:args.top_n],
                    }
            output = json.dumps(slim, ensure_ascii=False, indent=2)
        else:
            print("[batch] 渲染汇总报告...", file=sys.stderr)
            output = render_batch_report(batch, top_n=args.top_n, use_llm=args.llm)

        print(output)

        if args.out:
            out_path = Path(args.out) if Path(args.out).is_absolute() else config.OUTPUT_DIR / args.out
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(output, encoding="utf-8")
            print(f"\n[已保存到 {out_path}]", file=sys.stderr)
        return

    # ===== 单板块 =====
    result = run_pipeline(args.sector, days=args.days, tavily_results=args.tavily_results)

    if args.json:
        slim = {
            "market": "us",
            "sector": result["sector"],
            "run_time": result["run_time"],
            "days": result["days"],
            "chain": {k: v for k, v in result["chain"].items() if k != "stocks_by_sector"},
            "discovered_stats": result["discovered_stats"],
            "scored_top": result["scored"]["scored"][:args.top_n],
            "overflow_strength": result["scored"]["overflow_strength"],
            "leader_saturation": result["scored"]["leader_saturation"],
        }
        output = json.dumps(slim, ensure_ascii=False, indent=2)
    else:
        if args.llm:
            print("[5/5] LLM 综合分析...", file=sys.stderr)
            output = llm_synthesize(result, top_n=args.top_n)
        else:
            output = _fallback_report(result, top_n=args.top_n)

    print(output)

    if args.out:
        out_path = Path(args.out) if Path(args.out).is_absolute() else config.OUTPUT_DIR / args.out
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
        print(f"\n[已保存到 {out_path}]", file=sys.stderr)


if __name__ == "__main__":
    main()
