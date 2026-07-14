"""
Layer 2: Tavily AI 深度搜索（重写）

多 Key 轮询，配额耗尽自动 failover。
算法借鉴 ~/.hermes/scripts/investment-research/tavily_search.py（独立重写）。

进度/诊断日志一律走 stderr（不污染 stdout）——skills 的 --json 输出在 stdout，
若 key 轮询日志也走 stdout 会破坏 JSON 解析（CLI 校验 + 任何 JSON.parse stdout 的消费方）。
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .. import config

# 板块默认搜索词
DEFAULT_QUERIES = {
    "optical_module": "光模块行业 800G 1.6T 订单 英伟达 AI算力 2025",
    "pcb": "PCB行业 AI服务器 印刷电路板 订单 产能 涨价 2025",
    "storage": "存储芯片 HBM DRAM 内存 价格 需求 AI服务器 2025",
    "optical_chip": "光芯片 硅光 CPO 激光器 国产替代 2025",
    "ai_server": "AI服务器 算力 数据中心 液冷 订单 2025",
    "liquid_cooling": "液冷 散热 AI服务器 温控 数据中心 2025",
    "hbm_components": "HBM 颗粒 TSV 封装材料 2025",
    "cooling_components": "冷却液 快接头 CDU 冷板 2025",
    "ccl": "覆铜板 CCL 高速材料 PCB 2025",
    "copper_foil": "铜箔 电子铜箔 PCB 2025",
    "chiplet": "Chiplet 先进封装 CoWoS 2.5D 3D 2025",
    "switch": "交换机 数据中心 800G 1.6T 白盒 2025",
    "datacenter": "数据中心 AIDC 智算中心 PUE 2025",
    "power_supply": "电源 PSU HVDC UPS AI服务器 2025",
    "ocs": "OCS 光交换 MEMS 光路交换 2025",
}


class TavilyKeyManager:
    """Tavily API Key 管理器 - 支持轮询和故障切换"""

    def __init__(self, keys: List[str]):
        self.keys = keys
        self.current_index = 0
        self.failed_keys = set()
        self.usage_stats = {i: {"success": 0, "failed": 0} for i in range(len(keys))}

    def get_current_key(self) -> Optional[str]:
        if not self.keys:
            return None
        return self.keys[self.current_index]

    def get_working_key(self) -> Optional[str]:
        if self.current_index not in self.failed_keys:
            return self.get_current_key()
        return self.switch_to_next_key()

    def switch_to_next_key(self) -> Optional[str]:
        if not self.keys:
            return None
        original = self.current_index
        for _ in range(len(self.keys)):
            self.current_index = (self.current_index + 1) % len(self.keys)
            if self.current_index not in self.failed_keys:
                print(f"🔄 切换到 Key #{self.current_index + 1}", file=sys.stderr)
                return self.keys[self.current_index]
            if self.current_index == original:
                break
        print("❌ 所有 Key 都已耗尽或失败", file=sys.stderr)
        return None

    def mark_failed(self, index: int, reason: str = ""):
        self.failed_keys.add(index)
        self.usage_stats[index]["failed"] += 1
        print(f"⚠️ Key #{index + 1} 标记失败: {reason}", file=sys.stderr)

    def mark_success(self, index: int):
        self.usage_stats[index]["success"] += 1

    def stats(self) -> Dict:
        return {
            "total_keys": len(self.keys),
            "working_keys": len(self.keys) - len(self.failed_keys),
            "failed_keys": list(self.failed_keys),
            "current_key_index": self.current_index,
            "usage_stats": self.usage_stats,
        }


class TavilySearch:
    """Tavily AI 搜索引擎（多 Key 支持）"""

    def __init__(self, keys: List[str] = None):
        keys = keys if keys is not None else config.TAVILY_API_KEYS
        if not keys:
            raise ValueError(
                "未配置 Tavily API Key。请设置环境变量 TAVILY_API_KEYS（逗号分隔多个 Key）"
            )
        self.key_manager = TavilyKeyManager(keys)
        self._client = None
        self._init_client()

    def _init_client(self):
        try:
            from tavily import TavilyClient
        except ImportError as e:
            raise ImportError(
                "tavily-python 未安装，请运行: pip install tavily-python"
            ) from e
        key = self.key_manager.get_working_key()
        if key:
            self._client = TavilyClient(api_key=key)
        else:
            raise RuntimeError("没有可用的 Tavily API Key")

    def _execute_with_retry(self, operation, max_retries: int = None) -> Dict:
        if max_retries is None:
            max_retries = max(2, len(self.key_manager.keys) * 2)

        last_error = None
        attempted = set()

        for attempt in range(max_retries):
            idx = self.key_manager.current_index
            if idx in attempted and idx in self.key_manager.failed_keys:
                if not self.key_manager.switch_to_next_key():
                    break
                idx = self.key_manager.current_index
            attempted.add(idx)

            try:
                from tavily import TavilyClient
                key = self.key_manager.get_current_key()
                self._client = TavilyClient(api_key=key)

                result = operation(self._client)
                self.key_manager.mark_success(idx)
                return result

            except Exception as e:
                err = str(e).lower()
                last_error = e

                is_quota = any(kw in err for kw in [
                    "quota", "limit", "exceeded", "429", "too many requests",
                    "credits", "billing", "usage limit",
                ])
                is_auth = any(kw in err for kw in [
                    "unauthorized", "invalid api key", "authentication",
                    "auth", "forbidden", "401", "403",
                ])

                if is_quota:
                    self.key_manager.mark_failed(idx, "quota exceeded")
                    if self.key_manager.switch_to_next_key():
                        time.sleep(0.5)
                        continue
                    else:
                        break
                elif is_auth:
                    self.key_manager.mark_failed(idx, "auth failed")
                    if self.key_manager.switch_to_next_key():
                        continue
                    else:
                        break
                else:
                    print(f"⚠️ 请求失败 (attempt {attempt + 1}/{max_retries}): {e}",
                          file=sys.stderr)
                    if attempt < max_retries - 1:
                        time.sleep(1)
                        continue
                    else:
                        raise

        raise RuntimeError(f"所有 Tavily API Key 不可用。最后错误: {last_error}")

    def get_key_stats(self) -> Dict:
        return self.key_manager.stats()

    def search_industry_news(self, sector: str, query: str = None,
                             days: int = 7, max_results: int = 10) -> Dict:
        """搜索特定板块的最新新闻"""
        sector = config.to_under(sector)
        search_query = query or DEFAULT_QUERIES.get(sector, f"{sector} 行业 2025")

        print(f"🔍 搜索 [{sector}]: {search_query} (days={days})", file=sys.stderr)
        stats = self.key_manager.stats()
        print(f"📊 Key 统计: {stats['working_keys']}/{stats['total_keys']} 可用", file=sys.stderr)

        try:
            def do_search(client):
                kwargs = dict(
                    query=search_query,
                    search_depth="advanced",
                    max_results=max_results,
                    include_answer=True,
                    include_raw_content=True,
                )
                if days is not None:
                    kwargs["days"] = days
                return client.search(**kwargs)

            response = self._execute_with_retry(do_search)

            # 落盘
            config.TAVILY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            day_dir = config.TAVILY_OUTPUT_DIR / datetime.now().strftime("%Y-%m-%d")
            day_dir.mkdir(exist_ok=True)
            ts = datetime.now().strftime("%H%M%S")
            result_file = day_dir / f"{sector}_{ts}.json"

            data = {
                "sector": sector,
                "query": search_query,
                "search_time": datetime.now().isoformat(),
                "result_count": len(response.get("results", [])),
                "answer": response.get("answer", ""),
                "results": response.get("results", []),
                "key_used": self.key_manager.current_index + 1,
            }
            with open(result_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            print(f"✅ 搜索完成: {len(response.get('results', []))} 条 "
                  f"(Key #{self.key_manager.current_index + 1})", file=sys.stderr)
            print(f"💾 已保存: {result_file}", file=sys.stderr)
            return data

        except Exception as e:
            print(f"❌ 搜索失败: {e}", file=sys.stderr)
            return {
                "sector": sector, "query": search_query, "error": str(e),
                "answer": "", "results": [],
            }

    def search_with_ai_summary(self, query: str, max_results: int = 10,
                                days: Optional[int] = None) -> Optional[Dict]:
        """任意 query 的 AI 摘要搜索

        Args:
            query: 搜索词
            max_results: 返回结果数
            days: 时间窗口（天），None=不限（全量）
        """
        try:
            def do_search(client):
                kwargs = dict(
                    query=query,
                    max_results=max_results,
                    search_depth="advanced",
                    include_answer=True,
                )
                if days is not None:
                    kwargs["days"] = days
                return client.search(**kwargs)
            return self._execute_with_retry(do_search)
        except Exception as e:
            print(f"搜索出错: {e}", file=sys.stderr)
            return None
