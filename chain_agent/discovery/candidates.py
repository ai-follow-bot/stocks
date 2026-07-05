"""
Layer 3: 候选标的构建

合并「文本动态发现」+「池中已有」+「overflow_config 龙头清单」，去重排序。
"""

import json
import sys
from collections import defaultdict
from typing import Dict, List

from .. import config
from .stock_detector import StockDetector


def _load_overflow_leaders() -> Dict:
    """从 sector_overflow_config.json 取各板块龙头/二线/技术期权股清单"""
    if not config.OVERFLOW_CONFIG_JSON.exists():
        return {}
    with open(config.OVERFLOW_CONFIG_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def _scan_news_for_hits(news_items: List[Dict], detector: StockDetector) -> Dict[str, Dict]:
    """对每条新闻单独跑 detector，累加 per-code 命中次数与 importance_sum。

    importance 取新闻条目的 `importance` 字段（财联新闻有，akshare/tavily 默认 1）。
    返回 {code: {"hits": int, "importance_sum": float}}
    """
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
    从双轨采集文本 + 配置文件龙头清单中构建候选池。

    Args:
        sector: 焦点板块
        collected: collectors.orchestrator.collect_all() 的返回
        chain: chain_graph.expand_chain() 的返回

    Returns:
        {
            "sector": str,
            "candidates": [{code, name, sector, match_type, matched_keyword,
                            source, news_hits, in_pool, tier}],
            "stats": {total, from_news, from_pool, new_discoveries}
        }
    """
    sector = config.to_under(sector)
    text = collected.get("combined_text", "")

    # 1. 文本发现：combined_text 扫描拿候选元数据（name/sector/match_type）
    detector = StockDetector()
    news_hits = detector.detect_stocks_from_text(text)

    # 1b. P1-1: 逐条新闻扫描，累加 hit_counts + news_importance_sum
    # （财联新闻 importance 字段生效；akshare/tavily 默认 1）
    # 注意 detect_stocks_from_text 单次调用按 code 去重，combined_text 扫描只能给出 0/1，
    # 真正的"命中次数"必须逐条新闻累加。
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

    # combined_text 扫到的但逐条新闻没命中的（如 supply content_text 拼接片段），
    # 至少给 1 次命中、importance 1，避免漏掉候选
    for h in news_hits:
        code = h["code"]
        if code not in hit_counts:
            hit_counts[code] = 1
            importance_sums[code] = 1.0

    # 2. overflow_config 中的龙头/二线/技术期权股作为「池中已有」
    overflow_cfg = _load_overflow_leaders()
    pool_stocks = {}
    sector_hyphen = config.to_hyphen(sector)
    sec_cfg = overflow_cfg.get(sector_hyphen, {})
    for role_key in ("leaders", "second_tier", "tech_option_stocks"):
        for s in sec_cfg.get(role_key, []):
            code = str(s.get("code", ""))
            if code and code not in pool_stocks:
                pool_stocks[code] = {
                    "code": code,
                    "name": s.get("name", ""),
                    "sector": sector,
                    "tier": (chain.get("tier", 2) if chain else 2),
                    "in_pool": True,
                    "role_hint": role_key,
                    "catalyst": s.get("catalyst", ""),
                }

    # 3. 合并去重
    candidates = {}
    for h in news_hits:
        code = h["code"]
        pool_info = pool_stocks.get(code, {})
        candidates[code] = {
            "code": code,
            "name": h.get("name") or pool_info.get("name", ""),
            "sector": h.get("sector") or pool_info.get("sector", sector),
            "match_type": h.get("match_type", ""),
            "matched_keyword": h.get("matched_keyword", ""),
            "source": "both" if code in pool_stocks else "news",
            "news_hits": hit_counts[code],
            "news_importance_sum": importance_sums.get(code, 0.0),
            "in_pool": code in pool_stocks,
            "tier": pool_info.get("tier", chain.get("tier", 2) if chain else 2),
            "catalyst": pool_info.get("catalyst", ""),
        }

    # 4. 池中有但文本没命中的也纳入
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
        "sector": sector,
        "candidates": cand_list,
        "stats": stats,
    }


def filter_by_lockup(candidates: List[Dict], days: int = 90) -> List[Dict]:
    """剔除未来 N 天内有解禁的候选股，但保留 pool 中的龙头/二线（仅打 warning）。

    Args:
        candidates: 已 enrich 过的 candidate list（含 `extras.lockup` 字段）
        days: 解禁窗口（天），默认 90

    Returns:
        过滤后的 list。被剔除的候选股不进 scoring；保留的 pool 股票打上
        `extras.lockup_warning=True`，让 LLM 在报告里提示风险。
    """
    kept = []
    dropped = 0
    for c in candidates:
        lockup = (c.get("extras") or {}).get("lockup") or {}
        days_until = lockup.get("days_until")
        # 无解禁数据 / 无未来解禁 → 保留
        if days_until is None:
            kept.append(c)
            continue
        # 在解禁窗口内
        if days_until <= days:
            # pool 中的龙头/二线不剔除，仅打 warning
            if c.get("in_pool"):
                c.setdefault("extras", {})["lockup_warning"] = True
                kept.append(c)
            else:
                dropped += 1
                print(f"[lockup-filter] 剔除 {c.get('code')} {c.get('name')} "
                      f"(解禁日 {lockup['upcoming'][0]['date']}, 距今 {days_until} 天)",
                      file=sys.stderr)
            continue
        kept.append(c)
    if dropped:
        print(f"[lockup-filter] 共剔除 {dropped} 只近 {days} 天解禁候选", file=sys.stderr)
    return kept


def render_candidates_text(discovered: Dict) -> str:
    lines = [f"# 候选标的清单 (共 {discovered['stats']['total']} 只)"]
    lines.append(
        f"动态发现: {discovered['stats']['from_news']} | "
        f"配置池: {discovered['stats']['from_pool']} | "
        f"新发现: {discovered['stats']['new_discoveries']}"
    )
    lines.append("")
    lines.append("| 代码 | 名称 | 板块 | Tier | 新闻命中 | 来源 |")
    lines.append("|------|------|------|------|---------|------|")
    for c in discovered["candidates"][:30]:
        lines.append(
            f"| {c['code']} | {c['name']} | {c['sector']} | T{c['tier']} | "
            f"{c['news_hits']} | {c['source']} |"
        )
    return "\n".join(lines)
