"""
美股 Layer 1: 产业链图展开

与 chain_agent.chain_graph 算法一致，仅切换数据源为 us_sector_ecosystem.json。
render_chain_text 直接复用 chain_agent 的（市场无关）。
"""

import json
from typing import Dict, List

from chain_agent import config as _cn_cfg  # 用 to_under / to_hyphen
from us_chain_agent import config

from chain_agent.chain_graph import render_chain_text  # 市场无关，直接复用

_ECOSYSTEM_CACHE: Dict | None = None


def load_ecosystem() -> Dict:
    """加载美股产业链生态配置（带缓存）"""
    global _ECOSYSTEM_CACHE
    if _ECOSYSTEM_CACHE is not None:
        return _ECOSYSTEM_CACHE
    if not config.US_ECOSYSTEM_JSON.exists():
        _ECOSYSTEM_CACHE = {}
        return _ECOSYSTEM_CACHE
    with open(config.US_ECOSYSTEM_JSON, "r", encoding="utf-8") as f:
        _ECOSYSTEM_CACHE = json.load(f)
    return _ECOSYSTEM_CACHE


def get_sector_config(sector: str) -> Dict:
    ecosystem = load_ecosystem()
    return ecosystem.get(_cn_cfg.to_under(sector), {})


def get_sector_name(sector: str) -> str:
    return get_sector_config(sector).get("name", sector)


def get_sector_tier(sector: str) -> int:
    return get_sector_config(sector).get("tier", 2)


def get_include_sectors(sector: str) -> List[str]:
    cfg = get_sector_config(sector)
    return cfg.get("include_sectors", [_cn_cfg.to_under(sector)])


def expand_chain(sector: str) -> Dict:
    """
    展开美股板块的完整产业链结构。

    Returns: 与 chain_agent.chain_graph.expand_chain 同结构
    """
    sector = _cn_cfg.to_under(sector)
    cfg = get_sector_config(sector)
    if not cfg:
        return {
            "focus_sector": sector,
            "error": f"未知美股板块: {sector}",
            "upstream": [], "downstream": [], "all_sectors": [sector],
            "nodes": {}, "stocks_by_sector": {},
        }

    upstream = cfg.get("upstream", [])
    downstream = cfg.get("downstream", [])
    related = cfg.get("related", [])
    all_sectors = list(dict.fromkeys(
        cfg.get("include_sectors", [sector]) + upstream + downstream
    ))

    nodes = {}
    for s in all_sectors:
        sc = get_sector_config(s)
        if sc:
            nodes[s] = {
                "name": sc.get("name", s),
                "tier": sc.get("tier", 2),
                "key_products": sc.get("key_products", [])[:5],
                "technologies": sc.get("technologies", [])[:5],
            }

    return {
        "focus_sector": sector,
        "focus_name": cfg.get("name", sector),
        "tier": cfg.get("tier", 2),
        "upstream": upstream,
        "downstream": downstream,
        "related": related,
        "all_sectors": all_sectors,
        "include_sectors": cfg.get("include_sectors", [sector]),
        "key_products": cfg.get("key_products", []),
        "technologies": cfg.get("technologies", []),
        "cost_structure": cfg.get("cost_structure", ""),
        "description": cfg.get("description", ""),
        "nodes": nodes,
        "stocks_by_sector": {},  # 由 discovery 层填充
    }
