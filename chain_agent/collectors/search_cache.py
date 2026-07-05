"""Tavily / 智谱 web 搜索结果缓存（query-level，带 TTL）。

缓存目的：
- 同板块一天内重跑（如调试、对比 LLM 模型）不重复花钱调 Tavily / 智谱
- 跨板块复用相同子查询的结果

缓存策略：
- key = sha1(query) （不含 max_results，因为 max_results=5 命中 max_results=10 的缓存也安全）
- 文件：output/search_cache/<sha1>.json
- 内容：{"query": str, "search_time": ISO, "data": dict}
- 命中条件：文件存在 且 search_time 在 TTL_HOURS 内
- 失效：超 TTL 或文件损坏或 data 没有 results/answer

环境变量：
- SEARCH_CACHE_DISABLED：任意非空值 → 关闭缓存（不读不写）
- SEARCH_CACHE_TTL_HOURS：缓存有效期（小时），默认 12

调用方：skills/deep-analyze/analyzer.py 的 _do_search 闭包
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .. import config


def _cache_dir() -> Path:
    d = config.OUTPUT_DIR / "search_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_enabled() -> bool:
    """是否启用缓存（读 + 写）。"""
    return not os.environ.get("SEARCH_CACHE_DISABLED", "").strip()


def cache_ttl_hours() -> int:
    try:
        return max(1, int(os.environ.get("SEARCH_CACHE_TTL_HOURS", "12")))
    except ValueError:
        return 12


def _cache_key(query: str) -> str:
    return hashlib.sha1(query.encode("utf-8")).hexdigest()


def _cache_path(query: str) -> Path:
    return _cache_dir() / f"{_cache_key(query)}.json"


def get_cached(query: str) -> Optional[dict]:
    """命中返回缓存的 data（原 provider 返回结构），未命中或失效返回 None。"""
    if not cache_enabled():
        return None
    p = _cache_path(query)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    search_time_str = raw.get("search_time")
    if not search_time_str:
        return None
    try:
        st = datetime.fromisoformat(search_time_str)
    except Exception:
        return None
    if st.tzinfo is None:
        st = st.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - st).total_seconds() / 3600
    if age_hours > cache_ttl_hours():
        return None
    data = raw.get("data")
    if not isinstance(data, dict):
        return None
    # 必须有实质内容才认缓存
    if not (data.get("results") or data.get("answer")):
        return None
    return data


def set_cached(query: str, data: dict) -> None:
    """写入缓存。data 必须有 results 或 answer 才写。"""
    if not cache_enabled():
        return
    if not isinstance(data, dict):
        return
    if not (data.get("results") or data.get("answer")):
        return
    p = _cache_path(query)
    payload = {
        "query": query,
        "search_time": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    try:
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        # 缓存写盘失败不影响主流程
        pass
