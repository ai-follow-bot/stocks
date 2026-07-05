"""
Layer 2: 三轨数据采集编排

供给侧（深度）: Tavily AI 搜索 — 行业研报/供需数据/AI 摘要
需求侧（实时）: 财联社（复用 hermes latest_news.json，cron 实时更新）
需求侧（补充）: akshare 新闻 — 个股新闻 + 宏观新闻关键词过滤（财联社不可用时降级）

三轨独立失败，统一为 content_text 供 discovery 层提取标的。
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

from .. import config
from .tavily_search import TavilySearch
from .zhipu_search import ZhipuSearch
from . import news_cailianshe
from . import news_akshare


def _get_search_provider():
    """优先 Tavily，失败后尝试智谱兜底。返回 (provider, name) 或 (None, None)。"""
    try:
        return TavilySearch(), "tavily"
    except Exception as e:
        print(f"[orchestrator] Tavily 不可用: {e}，尝试智谱兜底", file=sys.stderr)
    if config.ZHIPU_API_KEY:
        try:
            return ZhipuSearch(), "zhipu"
        except Exception as e:
            print(f"[orchestrator] 智谱不可用: {e}", file=sys.stderr)
    return None, None


def _search_failed(data: Dict) -> bool:
    """判断一次搜索是否无有效结果。"""
    if not data:
        return True
    if data.get("error"):
        return True
    if not (data.get("results") or data.get("answer")):
        return True
    return False


def _format_search_response(data: Dict, provider_name: str) -> Dict:
    """统一格式化搜索返回，供下游生成 content_text。"""
    chunks = []
    if data.get("answer"):
        chunks.append(f"[AI摘要] {data['answer']}")
    for r in data.get("results", []):
        chunks.append(f"[{r.get('title','')}] {r.get('content','')[:500]}")
    return {
        "source": provider_name,
        "query": data.get("query", ""),
        "answer": data.get("answer", ""),
        "results": data.get("results", []),
        "content_text": "\n".join(chunks),
        "error": data.get("error"),
    }


def collect_supply_side(sector: str, days: int = 7, max_results: int = 10) -> Dict:
    """Tavily 深度搜索（Tavily 失败则切智谱兜底）"""
    sector = config.to_under(sector)
    provider, provider_name = _get_search_provider()
    if provider is None:
        return {
            "source": "none", "sector": sector, "error": "无可用搜索引擎（Tavily/智谱均失败）",
            "answer": "", "results": [], "content_text": "",
        }

    data = None
    try:
        data = provider.search_industry_news(sector=sector, days=days, max_results=max_results)
    except Exception as e:
        data = {"error": str(e), "results": [], "answer": "", "query": ""}

    # Tavily 失败或无结果 → 切智谱
    if _search_failed(data) and provider_name == "tavily":
        try:
            z = ZhipuSearch()
            q = data.get("query") if data else None
            data = z.search_industry_news(sector=sector, query=q, max_results=max_results)
            provider_name = "zhipu"
        except Exception as e:
            err = f"Tavily 失败; 智谱兜底失败: {e}"
            return {
                "source": "tavily+zhipu", "sector": sector, "error": err,
                "answer": "", "results": [], "content_text": "",
            }

    if _search_failed(data):
        return {
            "source": provider_name, "sector": sector,
            "error": data.get("error") if data else "no result",
            "answer": "", "results": [], "content_text": "",
        }

    return {
        **_format_search_response(data, provider_name),
        "sector": sector,
    }


def search_extra_query(query: str, max_results: int = 8) -> Dict:
    """任意 query 的搜索（Tavily 失败切智谱）"""
    provider, provider_name = _get_search_provider()
    if provider is None:
        return {
            "source": "none", "query": query, "error": "无可用搜索引擎",
            "answer": "", "results": [], "content_text": "",
        }

    data = None
    try:
        data = provider.search_with_ai_summary(query, max_results=max_results)
    except Exception as e:
        data = None
        err = e

    if (data is None or _search_failed(data)) and provider_name == "tavily":
        try:
            z = ZhipuSearch()
            data = z.search_with_ai_summary(query, max_results=max_results)
            provider_name = "zhipu"
        except Exception as e:
            return {
                "source": "tavily+zhipu", "query": query,
                "error": f"Tavily 失败; 智谱兜底失败: {e}",
                "answer": "", "results": [], "content_text": "",
            }

    if not data or _search_failed(data):
        return {
            "source": provider_name, "query": query,
            "error": data.get("error") if data else "no result",
            "answer": "", "results": [], "content_text": "",
        }

    return _format_search_response(data, provider_name)


def _dedup(texts: List[str]) -> str:
    """跨轨去重：用每行前 100 字符做指纹，避免同新闻在财联社+akshare 重复计 news_hits。"""
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


def collect_all(sector: str, days: int = 7, tavily_results: int = 10,
                leader_codes: List[str] = None) -> Dict:
    """
    三轨并行采集。
    leader_codes 同时传给财联社（按 stock_codes 字段过滤）和 akshare（拉个股新闻）。
    财联社是主需求轨（实时）；akshare 是补充轨（兜底 + 个股新闻深度）。
    """
    with ThreadPoolExecutor(max_workers=3) as ex:
        fut_supply = ex.submit(collect_supply_side, sector, days, tavily_results)
        fut_cls = ex.submit(news_cailianshe.collect_demand_side, sector, days, leader_codes)
        fut_ak = ex.submit(news_akshare.collect_demand_side, sector, days, leader_codes)
        supply = fut_supply.result()
        demand_cls = fut_cls.result()
        demand_ak = fut_ak.result()

    # 主需求轨：财联社（实时）；akshare 作为补充
    # 若财联社不可用（hermes 不在或数据陈旧），主轨降级为 akshare
    if demand_cls.get("news_count", 0) > 0:
        primary_demand = demand_cls
        secondary_demand = demand_ak
    else:
        primary_demand = demand_ak
        secondary_demand = demand_cls

    combined = _dedup([
        supply.get("content_text", ""),
        primary_demand.get("content_text", ""),
        secondary_demand.get("content_text", ""),
    ]).strip()

    return {
        "sector": config.to_under(sector),
        "supply": supply,
        "demand": primary_demand,             # 主需求轨（discovery 层消费）
        "demand_secondary": secondary_demand,  # 补充轨（保底）
        "combined_text": combined,
    }
