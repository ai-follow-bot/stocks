"""
Agent 2: 共振计算

纯确定性Python公式，计算每个板块的共振分数。
完全不用LLM，可审计、可回测。
"""
import sys
from typing import Optional

from .config import DENSITY_CAP, DIVERSITY_MAX, IMPORTANCE_CAP
from .data import load_ecosystem, get_upstream_downstream, get_sector_name


def compute_resonance(
    sector_events: dict,
    weights: list[float],
    history: Optional[dict] = None,
) -> list[dict]:
    """
    计算所有板块的共振分数。

    参数:
        sector_events: Agent 1的输出 {sector_key: {events, stats}}
        weights: [w_density, w_sentiment, w_chain, w_diversity, w_importance]
        history: 历史统计数据 {sector_key: {avg_30d_count: float, ...}}

    返回:
        [{
            "sector": str,       # 板块key
            "name": str,         # 中文名称
            "score": float,      # 共振分数 0-100
            "dimensions": {      # 各维度分数
                "density": float,
                "sentiment": float,
                "chain_resonance": float,
                "diversity": float,
                "importance": float,
            },
            "stats": {...},      # 原始统计数据
            "events": [...],     # 关联事件
        }, ...]
    """
    ecosystem = load_ecosystem()
    if not ecosystem:
        print("[Agent 2] ⚠️ 未加载到板块生态系统", file=sys.stderr)
        return []

    w_density, w_sentiment, w_chain, w_diversity, w_importance = weights

    # 获取所有板块的key列表（用于产业链共振计算）
    all_sector_keys = set(sector_events.keys())

    results = []
    for sector_key, data in sector_events.items():
        stats = data["stats"]
        total = stats["total"]
        if total == 0:
            continue

        # ── 维度1: 事件密度 ──
        avg_30d = (history or {}).get(sector_key, {}).get("avg_30d_count", max(total, 1))
        density_raw = total / max(avg_30d, 1)
        density = min(density_raw, DENSITY_CAP) / DENSITY_CAP  # 归一化到 [0, 1]

        # ── 维度2: 情绪强度 ──
        pos = stats.get("positive", 0)
        neg = stats.get("negative", 0)
        sentiment_raw = (pos - neg) / total
        sentiment = (sentiment_raw + 1) / 2  # 从 [-1, 1] 映射到 [0, 1]

        # ── 维度3: 产业链共振 ──
        chain_resonance = _compute_chain_resonance(
            sector_key, all_sector_keys, ecosystem
        )

        # ── 维度4: 事件多样性 ──
        event_types = stats.get("event_types", [])
        diversity = min(len(event_types), DIVERSITY_MAX) / DIVERSITY_MAX

        # ── 维度5: 重要性加权 ──
        importance_sum = stats.get("importance_sum", 0)
        importance = min(importance_sum / 5.0, 1.0)  # 归一化

        # ── 加权总分 ──
        score = (
            w_density * density +
            w_sentiment * sentiment +
            w_chain * chain_resonance +
            w_diversity * diversity +
            w_importance * importance
        ) * 100

        results.append({
            "sector": sector_key,
            "name": get_sector_name(sector_key, ecosystem),
            "score": round(score, 2),
            "dimensions": {
                "density": round(density, 4),
                "sentiment": round(sentiment, 4),
                "chain_resonance": round(chain_resonance, 4),
                "diversity": round(diversity, 4),
                "importance": round(importance, 4),
            },
            "stats": {
                "total_events": total,
                "positive": pos,
                "negative": neg,
                "event_types": event_types,
                "importance_sum": round(importance_sum, 2),
            },
            "events": data["events"],
        })

    # 按分数降序排列
    results.sort(key=lambda x: x["score"], reverse=True)

    print(f"[Agent 2] 计算了 {len(results)} 个板块的共振分数", file=sys.stderr)
    for i, r in enumerate(results[:5]):
        print(f"  TOP{i+1}: {r['name']} ({r['sector']}) = {r['score']}", file=sys.stderr)

    return results


def _compute_chain_resonance(
    sector_key: str,
    all_sectors: set,
    ecosystem: dict,
) -> float:
    """
    计算产业链上下游共振强度。
    检查上下游板块是否同时有事件发生。
    """
    upstream, downstream, related = get_upstream_downstream(sector_key, ecosystem)

    # 上下游命中数
    chain_hits = 0
    chain_total = 0

    for us in upstream:
        chain_total += 1
        if us in all_sectors:
            chain_hits += 1

    for ds in downstream:
        chain_total += 1
        if ds in all_sectors:
            chain_hits += 1

    for rl in related:
        chain_total += 1
        if rl in all_sectors:
            chain_hits += 0.5  # 相关板块权重减半

    if chain_total == 0:
        return 0.0

    return chain_hits / chain_total


def get_top_k(results: list[dict], k: int = 10) -> list[dict]:
    """取前K个板块"""
    return results[:k]


def get_top3_sectors(results: list[dict]) -> list[str]:
    """取TOP3板块的key列表（用于反馈学习）"""
    return [r["sector"] for r in results[:3]]
