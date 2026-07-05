"""
美股 Layer 2: 双轨数据采集编排

供给侧: Tavily AI 搜索（复用 chain_agent.collectors.tavily_search.TavilySearch）
需求侧: Finnhub 个股新闻（us_chain_agent.collectors.news_finnhub）

输出结构对齐 chain_agent.collectors.orchestrator.collect_all，便于 discovery 层复用：
  {sector, supply, demand, demand_secondary, combined_text}
"""

from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

from chain_agent import config
from chain_agent.collectors.tavily_search import TavilySearch

from us_chain_agent.collectors import news_finnhub


def _dedup(texts: List[str]) -> str:
    seen = set()
    out = []
    for t in texts:
        if not t:
            continue
        for chunk in t.split("\n"):
            fp = chunk[:100].strip()
            if fp and fp not in seen:
                seen.add(fp)
                out.append(chunk)
    return "\n".join(out)


def collect_supply_side(sector: str, days: int = 7, max_results: int = 10) -> Dict:
    """Tavily 深度搜索（美股板块英文查询更丰富）"""
    sector_under = config.to_under(sector)
    try:
        s = TavilySearch()
        data = s.search_industry_news(sector=sector_under, days=days, max_results=max_results)
    except Exception as e:
        return {
            "source": "tavily", "sector": sector_under, "error": str(e),
            "answer": "", "results": [], "content_text": "",
        }

    chunks = []
    if data.get("answer"):
        chunks.append(f"[AI摘要] {data['answer']}")
    for r in data.get("results", []):
        chunks.append(f"[{r.get('title','')}] {r.get('content','')[:500]}")

    return {
        "source": "tavily",
        "sector": sector_under,
        "query": data.get("query", ""),
        "answer": data.get("answer", ""),
        "results": data.get("results", []),
        "content_text": "\n".join(chunks),
        "error": data.get("error"),
    }


def search_extra_query(query: str, max_results: int = 8) -> Dict:
    """任意 query 的 Tavily 搜索（复用 A 股 orchestrator 接口）"""
    try:
        s = TavilySearch()
        r = s.search_with_ai_summary(query, max_results=max_results)
    except Exception as e:
        return {
            "source": "tavily", "query": query, "error": str(e),
            "answer": "", "results": [], "content_text": "",
        }
    if not r:
        return {
            "source": "tavily", "query": query, "error": "no result",
            "answer": "", "results": [], "content_text": "",
        }
    chunks = []
    if r.get("answer"):
        chunks.append(f"[AI摘要] {r['answer']}")
    for item in r.get("results", []):
        chunks.append(f"[{item.get('title','')}] {item.get('content','')[:500]}")
    return {
        "source": "tavily",
        "query": query,
        "answer": r.get("answer", ""),
        "results": r.get("results", []),
        "content_text": "\n".join(chunks),
    }


def collect_all(sector: str, days: int = 7, tavily_results: int = 10,
                leader_codes: List[str] = None) -> Dict:
    """美股双轨并行采集：Tavily 供给侧 + Finnhub 需求侧"""
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_supply = ex.submit(collect_supply_side, sector, days, tavily_results)
        fut_news = ex.submit(
            news_finnhub.collect_stock_news, leader_codes or [], days
        )
        supply = fut_supply.result()
        demand = fut_news.result()

    # 美股只有一条需求轨（Finnhub），secondary 留空保持 schema 对齐
    combined = _dedup([
        supply.get("content_text", ""),
        demand.get("content_text", ""),
    ]).strip()

    return {
        "sector": config.to_under(sector),
        "supply": supply,
        "demand": demand,
        "demand_secondary": {"source": "none", "news_count": 0, "news": [], "content_text": ""},
        "combined_text": combined,
    }
