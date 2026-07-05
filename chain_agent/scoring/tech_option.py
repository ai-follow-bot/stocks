"""
Layer 4: 技术突破期权评估（重写）

算法借鉴 ~/.hermes/scripts/investment-research/active_opportunity_integrator.py 的
TechBreakthroughAnalyzer（独立重写）。

期望值 = success_prob × (success_pe - base_pe) / base_pe

可选 Tavily 验证搜索：对每只技术期权股做一次「公司 + 技术 + 量产 + 客户」搜索。
"""

import json
from typing import Dict, List, Optional

from .. import config
from ..collectors.zhipu_search import ZhipuSearch


def _load_tech_option_stocks(sector_hyphen: str) -> List[Dict]:
    """从 sector_overflow_config.json 取技术期权股清单"""
    if not config.OVERFLOW_CONFIG_JSON.exists():
        return []
    with open(config.OVERFLOW_CONFIG_JSON, "r", encoding="utf-8") as f:
        all_cfg = json.load(f)
    sec_cfg = all_cfg.get(sector_hyphen, {})
    # 优先用 tech_option_stocks，其次从 second_tier 中带 tech_option=True 的
    stocks = list(sec_cfg.get("tech_option_stocks", []))
    for s in sec_cfg.get("second_tier", []):
        if s.get("tech_option") and not any(
            x.get("code") == s.get("code") for x in stocks
        ):
            stocks.append(s)
    return stocks


def _expected_value(stock: Dict) -> float:
    """技术期权期望值 = p × (success_pe - base_pe) / base_pe"""
    p = stock.get("success_prob", 0)
    base = stock.get("base_pe", 0)
    success = stock.get("success_pe", 0)
    if not (base and success and p):
        return 0.0
    return p * (success - base) / base


def _match_snippet_from_pool(stock: Dict, tavily_results: List[Dict]) -> str:
    """从主调 Tavily results 池里按股票名/代码/技术做 substring 匹配，取首条命中的 content[:500]。

    未命中返回空串，调用方再决定是否 fallback 到单独调 Tavily。
    """
    name = str(stock.get("name", ""))
    code = str(stock.get("code", ""))
    tech = str(stock.get("tech", ""))
    needles = [n for n in (name, code, tech) if n and len(n) >= 2]
    if not needles:
        return ""
    for r in tavily_results or []:
        title = str(r.get("title", ""))
        content = str(r.get("content", ""))
        haystack = title + " " + content
        if any(n in haystack for n in needles):
            snippet = (title + " | " + content).strip()
            return snippet[:500]
    return ""


def analyze_tech_options(sector: str, tavily_search=None,
                         tavily_results_pool: List[Dict] = None) -> Dict:
    """
    评估板块的技术期权。

    Args:
        sector: 板块代码
        tavily_search: 可选 TavilySearch 实例，仅在 results 池未命中时做 fallback 搜索
        tavily_results_pool: 主 pipeline 采集的 Tavily results 列表，优先从中做 substring 匹配

    Returns:
        {sector, opportunities: [{code, name, tech, stage, base_pe, success_pe,
                                   success_prob, expected_value, tavily_snippet}]}
    """
    sector_under = config.to_under(sector)
    sector_hyphen = config.to_hyphen(sector)
    stocks = _load_tech_option_stocks(sector_hyphen)

    if not stocks:
        return {
            "sector": sector_under,
            "opportunities": [],
            "summary": f"板块 {sector_under} 无技术期权股配置",
        }

    pool = tavily_results_pool or []
    opportunities = []
    fallback_calls = 0
    for s in stocks:
        ev = _expected_value(s)
        op = {
            "code": s.get("code", ""),
            "name": s.get("name", ""),
            "tech": s.get("tech", ""),
            "stage": s.get("stage", ""),
            "base_pe": s.get("base_pe", 0),
            "success_pe": s.get("success_pe", 0),
            "success_prob": s.get("success_prob", 0),
            "tam_cn": s.get("tam_cn", ""),
            "expected_value": round(ev, 3),
            "tavily_snippet": "",
        }

        # P1-2: 优先从主调 Tavily results 池子做 substring 匹配（0 次 API 调用）
        op["tavily_snippet"] = _match_snippet_from_pool(s, pool)

        # 池子未命中才 fallback 到单独调 Tavily；Tavily 不可用再切智谱
        if not op["tavily_snippet"] and op["code"]:
            fallback_calls += 1
            query = f"{op['name']} {op['tech']} 最新进展 量产 客户 认证 2025 2026"
            if tavily_search is not None:
                try:
                    data = tavily_search.search_industry_news(
                        sector=sector_hyphen, query=query, max_results=5
                    )
                    if data.get("answer"):
                        op["tavily_snippet"] = data["answer"][:500]
                    elif data.get("results"):
                        op["tavily_snippet"] = data["results"][0].get("content", "")[:500]
                except Exception as e:
                    op["tavily_snippet"] = f"(Tavily 搜索失败: {e})"

            if not op["tavily_snippet"] or op["tavily_snippet"].startswith("(Tavily"):
                try:
                    z = ZhipuSearch()
                    zr = z.search_with_ai_summary(query, max_results=5)
                    if zr and zr.get("results"):
                        op["tavily_snippet"] = zr["results"][0].get("content", "")[:500]
                except Exception as e:
                    if not op["tavily_snippet"]:
                        op["tavily_snippet"] = f"(搜索失败: {e})"

        opportunities.append(op)

    print(
        f"[tech_option] Tavily fallback 调用 {fallback_calls}/{len(stocks)} 次 "
        f"(池子命中 {len(stocks) - fallback_calls})",
        file=__import__('sys').stderr,
    )

    opportunities.sort(key=lambda x: x["expected_value"], reverse=True)

    if opportunities:
        top = opportunities[0]
        summary = (
            f"共 {len(opportunities)} 只技术期权股，"
            f"最高期望值: {top['name']}({top['expected_value']:.2f}, "
            f"{top['tech']}, {top['stage']})"
        )
    else:
        summary = "无技术期权股"

    return {
        "sector": sector_under,
        "opportunities": opportunities,
        "summary": summary,
    }
