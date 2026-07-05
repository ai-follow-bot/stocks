"""
美股 Layer 3: 候选标的构建

基于 chain_agent/discovery/candidates.py 改写：
- StockDetector → StockDetectorUS
- overflow_config → us_sector_overflow_config
- 候选池 leaders/second_tier/tech_option_stocks 全部来自 us_sector_overflow_config.json
"""

import json
from collections import defaultdict
from typing import Dict, List

from us_chain_agent import config
from us_chain_agent.discovery.stock_detector_us import StockDetectorUS, CORE_SECTOR_STOCKS


def _load_us_overflow_leaders() -> Dict:
    """从 us_sector_overflow_config.json 取各板块龙头/二线/技术期权清单"""
    if not config.US_OVERFLOW_CONFIG_JSON.exists():
        return {}
    with open(config.US_OVERFLOW_CONFIG_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def _scan_news_for_hits(news_items: List[Dict], detector: StockDetectorUS) -> Dict[str, Dict]:
    agg: Dict[str, Dict] = defaultdict(lambda: {"hits": 0, "importance_sum": 0.0})
    for n in news_items or []:
        title = str(n.get("title", ""))
        content = str(n.get("content", ""))
        text = (title + " " + content).strip()
        if not text:
            continue
        try:
            importance = float(n.get("importance") or 1)
        except (TypeError, ValueError):
            importance = 1.0
        for h in detector.detect_stocks_from_text(text):
            code = h["code"]
            agg[code]["hits"] += 1
            agg[code]["importance_sum"] += importance
    return agg


def discover_candidates(sector: str, collected: Dict, chain: Dict = None) -> Dict:
    """
    从双轨采集文本 + us_overflow_config 龙头清单中构建候选池。

    Returns: 与 A 股 discover_candidates 同结构
    """
    sector_under = config.to_under(sector) if hasattr(config, "to_under") else sector.replace("-", "_")
    text = collected.get("combined_text", "")

    detector = StockDetectorUS()
    news_hits = detector.detect_stocks_from_text(text)

    # 逐条新闻扫描累加命中
    hit_counts: Dict[str, int] = defaultdict(int)
    importance_sums: Dict[str, float] = defaultdict(float)
    for news_items in (
        (collected.get("demand") or {}).get("news", []),
        (collected.get("demand_secondary") or {}).get("news", []),
        (collected.get("supply") or {}).get("results", []),
    ):
        per_code = _scan_news_for_hits(news_items, detector)
        for code, v in per_code.items():
            hit_counts[code] += v["hits"]
            importance_sums[code] += v["importance_sum"]

    for h in news_hits:
        code = h["code"]
        if code not in hit_counts:
            hit_counts[code] = 1
            importance_sums[code] = 1.0

    # us_overflow_config 中的龙头/二线/技术期权股作为「池中已有」
    overflow_cfg = _load_us_overflow_leaders()
    pool_stocks = {}
    sector_hyphen = sector_under.replace("_", "-")
    sec_cfg = overflow_cfg.get(sector_hyphen, {})
    for role_key in ("leaders", "second_tier", "tech_option_stocks"):
        for s in sec_cfg.get(role_key, []):
            code = str(s.get("code", ""))
            if code and code not in pool_stocks:
                pool_stocks[code] = {
                    "code": code,
                    "name": s.get("name", ""),
                    "sector": sector_under,
                    "tier": (chain.get("tier", 2) if chain else 2),
                    "in_pool": True,
                    "role_hint": role_key,
                    "catalyst": s.get("catalyst", ""),
                }

    # 合并去重
    candidates = {}
    for h in news_hits:
        code = h["code"]
        pool_info = pool_stocks.get(code, {})
        candidates[code] = {
            "code": code,
            "name": h.get("name") or pool_info.get("name", ""),
            "sector": h.get("sector") or pool_info.get("sector", sector_under),
            "match_type": h.get("match_type", ""),
            "matched_keyword": h.get("matched_keyword", ""),
            "source": "both" if code in pool_stocks else "news",
            "news_hits": hit_counts[code],
            "news_importance_sum": importance_sums.get(code, 0.0),
            "in_pool": code in pool_stocks,
            "tier": pool_info.get("tier", chain.get("tier", 2) if chain else 2),
            "catalyst": pool_info.get("catalyst", ""),
        }

    for code, info in pool_stocks.items():
        if code not in candidates:
            candidates[code] = {
                "code": code,
                "name": info["name"],
                "sector": info["sector"],
                "match_type": "pool",
                "matched_keyword": "",
                "source": "pool",
                "news_hits": 0,
                "news_importance_sum": 0.0,
                "in_pool": True,
                "tier": info["tier"],
                "catalyst": info.get("catalyst", ""),
            }

    cand_list = sorted(
        candidates.values(),
        key=lambda x: (x["news_hits"], x["in_pool"], -x["tier"]),
        reverse=True,
    )

    stats = {
        "total": len(cand_list),
        "from_news": sum(1 for c in cand_list if c["source"] in ("news", "both")),
        "from_pool": sum(1 for c in cand_list if c["source"] in ("pool", "both")),
        "new_discoveries": sum(1 for c in cand_list if c["source"] == "news"),
    }

    return {
        "sector": sector_under,
        "candidates": cand_list,
        "stats": stats,
    }
