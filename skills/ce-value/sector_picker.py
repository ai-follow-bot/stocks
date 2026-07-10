"""431 行业层（无 --chain 时）：LLM 从宏观/市场简报 + ecosystem 板块清单选 1-3 个顺势板块。

板块清单取自 sector_ecosystem.json 的 {key, name(中文), description}。
LLM 选中的是中文名（deep-analyze/valuation-lens/chain_agent 均接受中文链名）。
"""

import json
import sys

from chain_agent import config
from . import common, prompts


def _sector_list() -> list:
    """返回 [{key, name, description}] 从 ecosystem（排除 metadata）。"""
    try:
        ec = json.loads(config.ECOSYSTEM_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[ce-value] ecosystem 读取失败: {e}", file=sys.stderr)
        return []
    out = []
    for k, v in ec.items():
        if k == "metadata" or not isinstance(v, dict):
            continue
        out.append({"key": k, "name": v.get("name", k), "description": v.get("description", "")})
    return out


def pick_sectors(macro_briefing: dict, market_briefing: dict, max_pick: int = 3) -> dict:
    """返回 {picked: [中文名...], reason, all_sectors}。picked 至少 1 个（兜底取清单首项）。"""
    sectors = _sector_list()
    if not sectors:
        return {"picked": [], "reason": "ecosystem 不可用", "all_sectors": []}

    sector_list_text = "\n".join(
        f"- {s['name']}（{s['description'][:50]}）" for s in sectors
    )
    user = prompts.SECTOR_PICK_USER_TEMPLATE.format(
        macro_summary=json.dumps(macro_briefing, ensure_ascii=False)[:800],
        market_summary=json.dumps(market_briefing, ensure_ascii=False)[:800],
        sector_list=sector_list_text,
    )
    data = common._llm_call_json(prompts.SECTOR_PICK_SYSTEM, user) or {}
    picked_raw = data.get("picked", []) or []
    if isinstance(picked_raw, str):
        picked_raw = [picked_raw]

    # reason 容错：LLM 可能返回 {板块:理由} dict 或字符串，统一转成可读字符串
    reason = data.get("reason", "")
    if isinstance(reason, dict):
        reason = "; ".join(f"{k}: {v}" for k, v in reason.items())
    elif not isinstance(reason, str):
        reason = str(reason)

    valid_names = {s["name"] for s in sectors}
    picked = [p for p in picked_raw if p in valid_names][:max_pick]
    # 模糊兜底：LLM 可能写成英文 key
    if not picked:
        key_to_name = {s["key"]: s["name"] for s in sectors}
        picked = [key_to_name[p] for p in picked_raw if p in key_to_name][:max_pick]
    # 仍空 -> 清单首项兜底，保证下游有板块可跑
    if not picked:
        picked = [sectors[0]["name"]]
        print(f"[ce-value] 板块选择未命中清单，兜底取 {picked[0]}", file=sys.stderr)

    print(f"[ce-value] 选定板块: {picked}", file=sys.stderr)
    return {"picked": picked, "reason": reason, "all_sectors": sectors}
