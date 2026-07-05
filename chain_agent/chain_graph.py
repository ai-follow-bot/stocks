"""
Layer 1: 产业链图展开

加载 data/sector_ecosystem.json，输入板块代码，输出包含上下游的完整产业链结构。
算法借鉴 ~/.hermes/scripts/investment-research/supply_chain_integration.py（独立重写）。
"""

import json
from typing import Dict, List

from . import config


_ECOSYSTEM_CACHE = None


def load_ecosystem() -> Dict:
    """加载产业链生态配置（带缓存）"""
    global _ECOSYSTEM_CACHE
    if _ECOSYSTEM_CACHE is not None:
        return _ECOSYSTEM_CACHE
    if not config.ECOSYSTEM_JSON.exists():
        _ECOSYSTEM_CACHE = {}
        return _ECOSYSTEM_CACHE
    with open(config.ECOSYSTEM_JSON, "r", encoding="utf-8") as f:
        _ECOSYSTEM_CACHE = json.load(f)
    return _ECOSYSTEM_CACHE


def get_sector_config(sector: str) -> Dict:
    """取单个板块的配置"""
    ecosystem = load_ecosystem()
    return ecosystem.get(config.to_under(sector), {})


def get_sector_name(sector: str) -> str:
    return get_sector_config(sector).get("name", sector)


def get_sector_tier(sector: str) -> int:
    return get_sector_config(sector).get("tier", 2)


def get_include_sectors(sector: str) -> List[str]:
    """取分析某板块时应包含的所有板块（含上游）"""
    cfg = get_sector_config(sector)
    return cfg.get("include_sectors", [config.to_under(sector)])


def expand_chain(sector: str) -> Dict:
    """
    展开板块的完整产业链结构。

    Returns:
        {
            "focus_sector", "focus_name", "tier",
            "upstream", "downstream", "related",
            "all_sectors", "include_sectors",
            "key_products", "technologies", "cost_structure", "description",
            "nodes": {sector: {name, tier, key_products, technologies}},
            "stocks_by_sector": {},  # 本层不再加载股票池，由 discovery 层负责
        }
    """
    sector = config.to_under(sector)
    cfg = get_sector_config(sector)
    if not cfg:
        return {
            "focus_sector": sector,
            "error": f"未知板块: {sector}",
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


def render_chain_text(chain: Dict) -> str:
    """把产业链结构渲染成可读文本"""
    if chain.get("error"):
        return f"[链展开失败] {chain['error']}"

    lines = [
        f"# 产业链结构: {chain['focus_name']} (Tier {chain['tier']})",
        f"说明: {chain['description']}",
        "",
    ]
    if chain["upstream"]:
        up_str = " → ".join(chain["nodes"].get(s, {}).get("name", s) for s in chain["upstream"])
        lines.append(f"上游: {up_str}")
    lines.append(f"本环节: {chain['focus_name']}")
    if chain["downstream"]:
        down_str = " → ".join(chain["nodes"].get(s, {}).get("name", s) for s in chain["downstream"])
        lines.append(f"下游: {down_str}")

    lines.append("")
    lines.append(f"核心产品: {', '.join(chain.get('key_products', [])[:6])}")
    if chain.get("technologies"):
        lines.append(f"关键技术: {', '.join(chain['technologies'][:6])}")
    if chain.get("cost_structure"):
        lines.append(f"成本结构: {chain['cost_structure']}")

    return "\n".join(lines)
