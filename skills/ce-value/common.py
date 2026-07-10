"""ce-value 共享工具：LLM 调用 + web 搜索（缓存 + Tavily/Zhipu 兜底）。

复用 chain_agent 的 LLM client / parse / search_cache / TavilySearch / ZhipuSearch，
接口与 skills/deep-analyze/analyzer.py 的 _llm_call / _do_search 保持一致。
"""

import sys
from typing import Optional

from chain_agent import config
from chain_agent.llm.client import get_llm_client
from chain_agent.llm.parse import json_from_llm
from chain_agent.collectors import search_cache
from chain_agent.collectors.tavily_search import TavilySearch
from chain_agent.collectors.zhipu_search import ZhipuSearch


def _llm_call(system: str, user: str) -> Optional[str]:
    """单轮 LLM 调用，失败返回 None。"""
    client = get_llm_client()
    if client is None:
        print("[ce-value] LLM 不可用", file=sys.stderr)
        return None
    try:
        return client.synthesize(system, user)
    except Exception as e:
        print(f"[ce-value] LLM 调用失败: {e}", file=sys.stderr)
        return None


def _llm_call_json(system: str, user: str) -> Optional[dict]:
    """LLM 调用 + json_from_llm 解析，失败返回 None。"""
    text = _llm_call(system, user)
    if not text:
        return None
    data = json_from_llm(text)
    if not data:
        print(f"[ce-value] JSON 解析失败: {text[:200]}", file=sys.stderr)
    return data


def web_search(query: str, max_results: int = 5) -> str:
    """web 搜索 -> 拼成文本块（标题 + snippet/answer）。缓存优先，Tavily 失败切智谱。

    返回拼接文本（可能为空串）。不抛异常。
    """
    cached = search_cache.get_cached(query)
    if cached:
        return _format_search(cached)
    try:
        provider = TavilySearch()
        r = provider.search_with_ai_summary(query, max_results=max_results)
    except Exception as e:
        print(f"[ce-value] 搜索失败 ({query[:40]}): {e}", file=sys.stderr)
        r = None
    if (not r or not (r.get("results") or r.get("answer"))) and config.ZHIPU_API_KEY:
        try:
            r = ZhipuSearch().search_with_ai_summary(query, max_results=max_results)
        except Exception as e:
            print(f"[ce-value] 智谱兜底失败 ({query[:40]}): {e}", file=sys.stderr)
            r = None
    if r and (r.get("results") or r.get("answer")):
        search_cache.set_cached(query, r)
    return _format_search(r or {})


def _format_search(r: dict) -> str:
    """把 search_with_ai_summary 结果拼成文本块。"""
    if not r:
        return ""
    parts = []
    ans = r.get("answer")
    if ans:
        parts.append(f"[摘要] {ans}")
    for i, item in enumerate(r.get("results", []) or [], 1):
        title = item.get("title", "")
        content = item.get("content") or item.get("snippet") or ""
        parts.append(f"[{i}] {title} | {content[:300]}")
    return "\n".join(parts)
