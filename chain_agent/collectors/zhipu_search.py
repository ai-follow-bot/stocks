"""
Layer 2 兜底搜索：智谱 BigModel web_search_pro

Tavily 不可用（key 配额耗尽 / 网络异常 / 未配置）时的兜底搜索引擎。
接口对齐 TavilySearch，方便 _get_search_provider 无缝切换。

API 文档：https://docs.bigmodel.cn/api-reference/工具-api/网络搜索
- 端点: POST https://open.bigmodel.cn/api/paas/v4/tools
- SDK: zhipuai >= 2.0
- 计费: 按搜索次数（search_pro 单价高于 search_std）

注意：智谱 web_search_pro 不返回 AI 摘要，answer 字段留空，
下游 _segment_search 已兼容空 answer。
"""

from datetime import datetime
import sys
from typing import Dict, Optional

from .. import config


class ZhipuSearch:
    """智谱 web_search_pro 兜底搜索，接口对齐 TavilySearch。"""

    def __init__(self, api_key: str = None, engine: str = None):
        try:
            from zhipuai import ZhipuAI
        except ImportError as e:
            raise ImportError(
                "zhipuai 未安装，请运行: pip install zhipuai>=2.0.0"
            ) from e

        self.api_key = api_key or config.ZHIPU_API_KEY
        if not self.api_key:
            raise ValueError(
                "未配置 ZHIPU_API_KEY。请设置环境变量 ZHIPU_API_KEY"
            )
        self.engine = engine or config.ZHIPU_SEARCH_ENGINE
        # 智谱支持的搜索引擎：search_std / search_pro / search_pro_sogou（搜狗增强版）
        valid_engines = ("search_std", "search_pro", "search_pro_sogou")
        if self.engine not in valid_engines:
            print(f"[zhipu] 未知 engine {self.engine!r}，回退 search_pro")
            self.engine = "search_pro"
        self._default_key = config._DEFAULT_ZHIPU_API_KEY
        self.client = ZhipuAI(api_key=self.api_key)

    def _is_auth_error(self, err: Exception) -> bool:
        text = str(err).lower()
        return any(k in text for k in ["身份验证失败", "401", "unauthorized", "invalid api key", "auth"])

    def _call_search(self, query: str, max_results: int):
        """实际调用 SDK 搜索，失败时返回异常。"""
        return self.client.web_search.web_search(
            search_engine=self.engine,
            search_query=query,
            count=max_results,
        )

    def search_with_ai_summary(self, query: str, max_results: int = 10) -> Optional[Dict]:
        """对应 TavilySearch.search_with_ai_summary。

        返回 {"results": [{title, content, url, media}], "answer": ""}
        智谱 web_search 不返回 AI 摘要，answer 固定为空字符串。

        若传入/环境 key 认证失败，自动回退到项目默认 key 重试一次。

        SDK 2.1+ API：client.web_search.web_search(search_engine, search_query, count, ...)
        响应 resp.search_result 是 list[SearchResultResp]，每项含 title/link/content/media。
        """
        resp = None
        try:
            resp = self._call_search(query, max_results)
        except Exception as e:
            if self._is_auth_error(e) and self.api_key != self._default_key and self._default_key:
                print(f"[zhipu] 当前 key 认证失败，回退默认 key 重试", file=sys.stderr)
                self.api_key = self._default_key
                self.client = __import__('zhipuai').ZhipuAI(api_key=self.api_key)
                try:
                    resp = self._call_search(query, max_results)
                except Exception as e2:
                    print(f"[zhipu] 默认 key 也失败 ({query}): {e2}")
                    return None
            else:
                print(f"[zhipu] 搜索失败 ({query}): {e}")
                return None

        raw_items = []
        if hasattr(resp, "search_result"):
            raw_items = resp.search_result or []
        elif isinstance(resp, dict):
            raw_items = resp.get("search_result", []) or []

        results = []
        for item in raw_items[:max_results]:
            # SDK 返回 pydantic 对象，也兼容 dict
            if hasattr(item, "model_dump"):
                d = item.model_dump()
            elif isinstance(item, dict):
                d = item
            else:
                d = {
                    "title": getattr(item, "title", "") or "",
                    "content": getattr(item, "content", "") or "",
                    "link": getattr(item, "link", "") or getattr(item, "url", "") or "",
                    "media": getattr(item, "media", "") or "",
                }
            results.append({
                "title": d.get("title", "") or "",
                "content": d.get("content", "") or "",
                "url": d.get("link", "") or d.get("url", "") or "",
                "media": d.get("media", "") or "",
            })

        return {
            "results": results,
            "answer": "",  # 智谱无 AI 摘要
            "provider": "zhipu",
            "query": query,
        }

    def search_industry_news(self, sector: str, query: str = None,
                             days: int = 7, max_results: int = 10) -> Dict:
        """接口对齐 TavilySearch.search_industry_news。

        智谱不区分 sector/days，直接拿 query 搜索；无 query 时按 sector 构造。
        """
        from ..collectors.tavily_search import DEFAULT_QUERIES
        sector = config.to_under(sector)
        search_query = query or DEFAULT_QUERIES.get(
            sector, f"{sector} 行业 {datetime.now().year}"
        )
        print(f"🔍 [zhipu] 搜索 [{sector}]: {search_query}")

        r = self.search_with_ai_summary(search_query, max_results=max_results)
        if r is None:
            return {
                "sector": sector, "query": search_query, "error": "zhipu search failed",
                "answer": "", "results": [],
            }

        return {
            "sector": sector,
            "query": search_query,
            "search_time": datetime.now().isoformat(),
            "result_count": len(r.get("results", [])),
            "answer": r.get("answer", ""),
            "results": r.get("results", []),
            "provider": "zhipu",
        }


if __name__ == "__main__":
    import sys
    # 清代理（akshare/zhipuai 直连需要）
    import os
    for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "all_proxy", "ALL_PROXY", "NO_PROXY", "no_proxy"]:
        os.environ.pop(k, None)
    q = sys.argv[1] if len(sys.argv) > 1 else "MLCC 国产替代 三环集团"
    zs = ZhipuSearch()
    r = zs.search_with_ai_summary(q, max_results=5)
    if r is None:
        print("搜索失败")
    else:
        print(f"智谱返回 {len(r['results'])} 条:")
        for i, item in enumerate(r["results"], 1):
            print(f"  {i}. {item['title'][:60]}")
            print(f"     {item['content'][:100]}")
            print(f"     {item['url']}")
