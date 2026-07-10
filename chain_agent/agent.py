#!/usr/bin/env python3
"""
产业链投资挖掘 Agent - 主入口

5 层 pipeline:
  chain_graph.expand_chain → collectors.collect_all
    → discovery.discover_candidates → scoring.score_candidates → llm.synthesize

用法:
  python -m chain_agent.agent optical_module                  # 纯 Python 报告
  python -m chain_agent.agent optical_module --llm            # 加 LLM 综合
  python -m chain_agent.agent --sectors pcb,storage           # 多板块批量
  python -m chain_agent.agent optical_module --json --out x.json
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from . import config
from . import chain_graph
from .collectors import orchestrator as collectors
from .collectors.orchestrator import _get_search_provider
from .discovery import candidates as discovery
from .scoring import integrator as scoring
from .scoring.quotes import get_quote_provider
from .llm.client import get_llm_client
from .llm.prompts import SYNTHESIS_SYSTEM, SYNTHESIS_USER_TEMPLATE


def _get_sector_leaders(sector: str):
    """从 overflow_config 取该板块龙头股代码列表（供 akshare 新闻层用）"""
    import json as _json
    if not config.OVERFLOW_CONFIG_JSON.exists():
        return None
    with open(config.OVERFLOW_CONFIG_JSON, "r", encoding="utf-8") as f:
        all_cfg = _json.load(f)
    sec_cfg = all_cfg.get(config.to_hyphen(sector), {})
    codes = [s["code"] for s in sec_cfg.get("leaders", []) if s.get("code")]
    return codes or None


def run_pipeline(sector: str, days: int = 7, tavily_results: int = 10) -> dict:
    """运行完整的 6 层 pipeline（不含 LLM）"""
    print(f"[1/6] 展开产业链: {sector}", file=sys.stderr)
    chain = chain_graph.expand_chain(sector)
    if chain.get("error"):
        raise RuntimeError(f"产业链展开失败: {chain['error']}")

    leaders = _get_sector_leaders(sector)
    print(f"[2/6] 双轨采集 (Tavily + akshare, days={days}, leaders={leaders})", file=sys.stderr)
    collected = collectors.collect_all(
        sector, days=days, tavily_results=tavily_results, leader_codes=leaders
    )

    print(f"[3/6] 动态发现候选标的", file=sys.stderr)
    discovered = discovery.discover_candidates(sector, collected, chain)

    print(f"[4/6] 候选股深度数据 enrich (资金面/龙虎榜/研报/解禁/热点题材)", file=sys.stderr)
    from .collectors import stock_data
    from .discovery.candidates import filter_by_lockup
    # 限制 enrich 前 30 只候选股，避免候选池过大触发东财风控
    stock_data.enrich_candidates(discovered["candidates"], sector=sector, limit=30)
    discovered["candidates"] = filter_by_lockup(discovered["candidates"], days=90)
    enriched_extras = {str(c["code"]): c.get("extras", {}) for c in discovered["candidates"]}

    print(f"[5/6] 可投资性评分 (溢出 + 技术期权 + 启发式 + enrich 4 维)", file=sys.stderr)
    # 共享一个搜索实例给评分层做验证搜索（Tavily 不可用则自动切智谱）
    search_for_scoring = None
    try:
        search_for_scoring, _ = _get_search_provider()
    except Exception:
        pass
    # P1-2: 复用主调 Tavily results 池给 tech_option，避免每股二次调用
    tavily_results_pool = collected.get("supply", {}).get("results", []) or []
    scored = scoring.score_candidates(
        sector, discovered,
        tavily_search=search_for_scoring,
        tavily_results_pool=tavily_results_pool,
        enriched_extras=enriched_extras,
    )

    return {
        "sector": config.to_under(sector),
        "run_time": datetime.now().isoformat(),
        "days": days,
        "chain": chain,
        "collected": collected,
        "discovered_stats": discovered["stats"],
        "scored": scored,
        "enriched_extras": enriched_extras,
    }


def run_batch(sectors: list, days: int = 7, tavily_results: int = 10,
              top_n: int = 15, use_llm: bool = False, max_workers: int = 3) -> dict:
    """多板块并行 pipeline"""
    print(f"[batch] 并行跑 {len(sectors)} 个板块: {sectors} (max_workers={max_workers})", file=sys.stderr)
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
        "run_time": datetime.now().isoformat(),
        "days": days,
        "results": results,
    }


def _render_dragon_tiger_text(enriched_extras: dict, top_n: int = 10, name_map: dict = None) -> str:
    """渲染龙虎榜机构净买 TOP，供 LLM 综合。"""
    name_map = name_map or {}
    rows = []
    for code, ex in enriched_extras.items():
        dt = ex.get("dragon_tiger") or {}
        inst = dt.get("institution") or {}
        net = inst.get("net_amt")
        if net and net > 0:
            records = dt.get("records") or []
            latest = records[0] if records else {}
            rows.append({
                "code": code,
                "name": name_map.get(code, code),
                "net_wan": net,
                "date": latest.get("date", ""),
                "reason": latest.get("reason", ""),
            })
    rows.sort(key=lambda x: x["net_wan"], reverse=True)
    if not rows:
        return "(近 30 天无机构净买入上榜记录)"
    lines = ["| 名称 | 机构净买(万) | 上榜日期 | 上榜原因 |",
             "|------|-------------|---------|---------|"]
    for r in rows[:top_n]:
        lines.append(f"| {r['name']} | {r['net_wan']:.0f} | {r['date']} | {r['reason'][:30]} |")
    return "\n".join(lines)


def _build_change_60d_map(enriched_extras: dict) -> dict:
    """各股近60日涨幅：直接用 AkshareQuoteProvider 拉 stock_zh_a_spot_em（批量行情自带
    「60日涨跌幅」列）。独立于 QUOTE_PROVIDER（默认 easyquotation 无此字段）；类级缓存，
    scoring 已用 akshare 时复用、零额外网络。失败返回 {}（涨幅列显示 "-"）。"""
    if not enriched_extras:
        return {}
    try:
        from .scoring.quotes import AkshareQuoteProvider
        qmap = AkshareQuoteProvider().get_quotes(list(enriched_extras.keys())) or {}
        return {c: (q or {}).get("change_60d") for c, q in qmap.items()}
    except Exception as e:
        print(f"[agent] 近60日涨幅拉取失败: {str(e)[:80]}", file=sys.stderr)
        return {}


def _build_price_changes_map(enriched_extras: dict) -> dict:
    """各股近 7/30 日涨幅（akshare stock_zh_a_hist 日K 算）。push2his 风控时全 {}（列显示 "-"）。
    60日另有批量行情可靠来源，故此处只算 7/30 日。"""
    if not enriched_extras:
        return {}
    try:
        from .collectors import stock_data
        return stock_data.price_changes_batch(list(enriched_extras.keys()))
    except Exception as e:
        print(f"[agent] 近7/30日涨幅拉取失败: {str(e)[:80]}", file=sys.stderr)
        return {}


def _render_fund_flow_text(enriched_extras: dict, name_map: dict = None,
                           change_60d_map: dict = None,
                           price_changes_map: dict = None) -> str:
    """渲染 60/30/7 日涨幅 + 融资融券余额变化。

    60日取自批量行情「60日涨跌幅」（可靠）；7/30 日取自 stock_zh_a_hist 日K 算
    （push2his 易风控，缺失写 "-"）；融资融券来自 enrich。全缺的标的跳过。
    """
    name_map = name_map or {}
    change_60d_map = change_60d_map or {}
    price_changes_map = price_changes_map or {}
    rows = []
    for code, ex in enriched_extras.items():
        chg60 = change_60d_map.get(code)
        pc = price_changes_map.get(code) or {}
        chg30 = pc.get(30)
        chg7 = pc.get(7)
        mg = ex.get("margin") or {}
        chg = mg.get("margin_balance_change")  # None = 无融资融券数据
        if chg60 is None and chg30 is None and chg7 is None and chg is None:
            continue
        rows.append({
            "code": code,
            "name": name_map.get(code, code),
            "change_60d": chg60,
            "change_30d": chg30,
            "change_7d": chg7,
            "margin_chg_pct": None if chg is None else chg * 100,
        })
    if not rows:
        return "(无资金面数据)"
    # None 沉底，其余按涨幅降序（None 作 -inf，reverse 后自然沉底）
    rows.sort(key=lambda x: x["change_60d"] if x["change_60d"] is not None else float('-inf'),
              reverse=True)
    lines = ["| 名称 | 60日涨幅% | 30日涨幅% | 7日涨幅% | 融资余额变化% |",
             "|------|---------|---------|--------|--------------|"]
    for r in rows[:15]:
        c60 = "-" if r["change_60d"] is None else f"{r['change_60d']:+.1f}"
        c30 = "-" if r["change_30d"] is None else f"{r['change_30d']:+.1f}"
        c7 = "-" if r["change_7d"] is None else f"{r['change_7d']:+.1f}"
        chg_str = "-" if r["margin_chg_pct"] is None else f"{r['margin_chg_pct']:+.1f}"
        lines.append(f"| {r['name']} | {c60} | {c30} | {c7} | {chg_str} |")
    return "\n".join(lines)


def _render_research_text(enriched_extras: dict, top_n: int = 10, name_map: dict = None) -> str:
    """渲染研报评级 + 一致预期 EPS，供 LLM 综合。"""
    name_map = name_map or {}
    rows = []
    for code, ex in enriched_extras.items():
        rpts = ex.get("research_reports") or []
        if not rpts:
            continue
        top = rpts[0]
        rows.append({
            "code": code,
            "name": name_map.get(code, code),
            "rating": top.get("rating", ""),
            "eps": top.get("predict_this_year_eps"),
            "org": top.get("org", ""),
            "title": top.get("title", "")[:40],
            "date": top.get("publish_date", ""),
        })
    if not rows:
        return "(无研报数据)"
    lines = ["| 名称 | 评级 | 今年一致预期EPS | 机构 | 标题 | 日期 |",
             "|------|------|----------------|------|------|------|"]
    for r in rows[:top_n]:
        try:
            eps = f"{float(r['eps']):.2f}" if r['eps'] not in (None, "") else "-"
        except (TypeError, ValueError):
            eps = "-"
        lines.append(
            f"| {r['name']} | {r['rating']} | {eps} | {r['org']} | {r['title']} | {r['date']} |"
        )
    return "\n".join(lines)


def llm_synthesize(result: dict, top_n: int = 15) -> str:
    """调用 LLM 生成最终投资分析报告"""
    client = get_llm_client()
    if client is None:
        return _fallback_report(result, top_n) + \
            "\n\n<!-- LLM 未启用（ANTHROPIC_API_KEY/OPENAI_API_KEY 均未设置），使用模板报告 -->\n"

    chain = result["chain"]
    coll = result["collected"]
    enriched_extras = result.get("enriched_extras") or {}
    # P0-2: 200K 上下文窗口下 4000 截断毫无必要，放宽到 20000
    tavily_content = (coll["supply"].get("content_text") or "")[:20000]
    # P0-1: 把补充轨（akshare）也拼进 LLM 输入，避免 0% 进 LLM
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
        dragon_tiger_text=_render_dragon_tiger_text(enriched_extras)[:3000],
        fund_flow_text=_render_fund_flow_text(
            enriched_extras,
            name_map={s["code"]: s.get("name", s["code"]) for s in result["scored"].get("scored", [])},
            change_60d_map=_build_change_60d_map(enriched_extras),
            price_changes_map=_build_price_changes_map(enriched_extras),
        )[:3000],
        research_text=_render_research_text(enriched_extras)[:3000],
        top_n=top_n,
        scored_text=scoring.render_scored_text(result["scored"], top_n=top_n),
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
        f"# {chain.get('focus_name', result['sector'])} 产业链投资分析",
        f"\n*生成时间: {result['run_time']} | 数据窗口: 近 {result['days']} 天*\n",
        "## 1. 产业链结构\n",
        chain_graph.render_chain_text(chain),
        "\n## 2. 数据采集概览\n",
        f"- Tavily 深度搜索: {tavily_count} 条结果" +
        (f" (错误: {tavily_err})" if tavily_err else ""),
        f"- akshare 新闻: {news_count} 条匹配" +
        (f" (错误: {news_err})" if news_err else ""),
        "\n## 3. 候选标的评分\n",
        scoring.render_scored_text(scored, top_n=top_n),
    ]

    # a-stock-data skill 集成：资金面 + 研报章节
    enriched_extras = result.get("enriched_extras") or {}
    # code -> 名称映射，资金面/研报表用名称替代代码
    name_map = {s["code"]: s.get("name", s["code"]) for s in scored.get("scored", [])}
    if enriched_extras:
        lines.append("\n## 4. 资金面信号\n")
        lines.append("### 龙虎榜（近 30 天机构净买）\n")
        lines.append(_render_dragon_tiger_text(enriched_extras, name_map=name_map))
        lines.append("\n### 近60日涨幅 + 融资融券\n")
        lines.append(_render_fund_flow_text(enriched_extras, name_map=name_map,
                                            change_60d_map=_build_change_60d_map(enriched_extras),
                                            price_changes_map=_build_price_changes_map(enriched_extras)))
        lines.append("\n## 5. 研报评级\n")
        lines.append(_render_research_text(enriched_extras, name_map=name_map))

    lines.append("\n## 6. 数据缺口\n")
    gaps = []
    if tavily_err:
        gaps.append("- Tavily 不可用，缺少行业深度搜索数据")
    if news_err:
        gaps.append("- akshare 新闻读取异常")
    if not any(s["extras"] for s in scored["scored"][:5]):
        gaps.append("- 行情源未启用，缺少实时 PE/市值，无法精确计算龙头饱和度")
    if not enriched_extras:
        gaps.append("- a-stock-data enrich 未运行，缺资金面/研报/龙虎榜数据")
    if not gaps:
        gaps.append("- 暂无明显缺口")
    lines.extend(gaps)
    lines.extend([
        "\n## 7. 后续行动建议\n",
        "- 对 Top 5 标的做技术面确认（趋势/支撑/量能）",
        "- 核查新发现标的的板块归属是否准确（防 6 位数字误匹配）",
        "- 跟踪近 7 天新闻中出现 ≥2 次的标的，等待回调买点",
        "- 关注龙虎榜机构净买的标的，结合 120 日主力净流入确认",
        "",
        "---",
        "",
        f"> **免责声明：** {config.DISCLAIMER_TEXT}",
    ])
    return "\n".join(lines)


def render_batch_report(batch: dict, top_n: int = 10, use_llm: bool = False) -> str:
    """多板块汇总报告"""
    lines = [
        "# 产业链投资分析 - 多板块汇总",
        f"\n*生成时间: {batch['run_time']} | 数据窗口: 近 {batch['days']} 天*",
        f"*板块: {', '.join(batch['sectors'])}*\n",
        "---\n",
    ]

    # 跨板块 Top
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
    lines.append("| 排名 | 名称 | 来源板块 | 角色 | 分数 |")
    lines.append("|------|------|---------|------|------|")
    for i, s in enumerate(all_scored[:15], 1):
        lines.append(
            f"| {i} | {s['name']} | {s['from_sector']} | "
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
    parser = argparse.ArgumentParser(description="产业链投资挖掘 Agent")
    parser.add_argument("sector", nargs="?",
                        help="板块代码，如 optical_module / pcb / storage（与 --sectors 互斥）")
    parser.add_argument("--sectors", type=str,
                        help="多板块批量，逗号分隔，如 optical_module,pcb,storage")
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
