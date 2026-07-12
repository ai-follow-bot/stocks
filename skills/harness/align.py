"""三路径输出对齐 by code + 一致性/分歧标注 + 文本渲染（供 LLM 输入与报告）"""

from typing import List


def _extract_chain(raw: dict) -> dict:
    """chain_agent --json: scored_top[] 或 scored.scored[] → {code: {name, score, role}}"""
    if "error" in raw:
        return {}
    scored = raw.get("scored_top") or (raw.get("scored") or {}).get("scored") or []
    out = {}
    for s in scored:
        code = s.get("code")
        if code:
            out[code] = {"name": s.get("name", ""), "score": s.get("score"), "role": s.get("role", "")}
    return out


def _extract_deep(raw: dict) -> dict:
    """deep-analyze: scoring 可能在 top（chain）或 chain_analysis.scoring（stock）；
    三维分数在 candidate.scores 里，total_score 在 top-level。"""
    if "error" in raw:
        return {}
    scoring = raw.get("scoring") or (raw.get("chain_analysis") or {}).get("scoring")
    if isinstance(scoring, dict) and scoring.get("candidates"):
        cands = scoring["candidates"]
    elif isinstance(scoring, dict) and (scoring.get("stock_code") or scoring.get("total_score") is not None):
        cands = [scoring]  # stock 模式 single
    else:
        cands = raw.get("candidates") or []
    out = {}
    for c in cands:
        code = c.get("stock_code") or c.get("code")
        if not code:
            continue
        ts = c.get("total_score")
        if ts is None:
            ts = (c.get("scores") or {}).get("total_score")
        scores = c.get("scores") or {}
        out[code] = {
            "name": c.get("name") or c.get("company", ""),
            "total": ts,
            "dims": {
                "supply_demand": scores.get("supply_demand"),
                "domestic_substitution": scores.get("domestic_substitution"),
                "earnings_realization": scores.get("earnings_realization"),
            },
        }
    return out


def _extract_val(raw: dict) -> dict:
    """valuation-lens --json: chain 模式 scoring.candidates[]；stock 模式 scoring 是 single dict"""
    if "error" in raw:
        return {}
    scoring = raw.get("scoring")
    if isinstance(scoring, list):
        cands = scoring
    elif isinstance(scoring, dict) and scoring.get("candidates"):
        cands = scoring["candidates"]
    elif isinstance(scoring, dict) and (scoring.get("stock_code") or scoring.get("valuation_score") is not None):
        cands = [scoring]  # stock 模式 single
    else:
        cands = raw.get("candidates") or []
    out = {}
    for c in cands:
        code = c.get("stock_code") or c.get("code")
        if not code:
            continue
        dims = {}
        for k in ("scarcity", "forward", "supply_demand"):
            d = c.get(k) or {}
            if isinstance(d, dict):
                dims[k] = d.get("score")
        out[code] = {
            "name": c.get("company") or c.get("name", ""),
            "score": c.get("valuation_score"),
            "dims": dims,
            "role": c.get("role", ""),
        }
    return out


def _extract_cycle(raw: dict) -> dict:
    """cycle-lens: results[] -> {code: {name, classification, eps_at_peak, pe_percentile, verdict}}"""
    if "error" in raw:
        return {}
    results = raw.get("results") or []
    if not results:
        r = raw.get("result")
        if r:
            results = [r]
    out = {}
    for r in results:
        code = r.get("code")
        if not code:
            continue
        decomp = r.get("decomp") or {}
        llm = r.get("llm_judgment") or {}
        out[code] = {
            "name": r.get("name", ""),
            "classification": decomp.get("classification"),
            "eps_at_peak": decomp.get("eps_at_peak"),
            "pe_percentile": decomp.get("pe_percentile"),
            "verdict": llm.get("verdict", ""),
        }
    return out


def _consistency(scores: list) -> dict:
    """三路径总分（均 0-100）一致性标注。"""
    rng = max(scores) - min(scores)
    if all(s >= 70 for s in scores):
        return {"label": "三视角共振", "range": rng}
    if all(s < 50 for s in scores):
        return {"label": "一致偏弱", "range": rng}
    if rng > 30:
        return {"label": "显著分歧", "range": rng}
    if rng > 15:
        return {"label": "基本一致", "range": rng}
    return {"label": "高度一致", "range": rng}


def _entry_score(e: dict, key: str):
    """取某路径的总分（chain.score / deep.total / val.score）。"""
    sub = e.get(key)
    if not sub:
        return None
    return sub.get("total") if key == "deep" else sub.get("score")


def _sort_key(e: dict):
    scores = [s for s in (_entry_score(e, k) for k in ("chain", "deep", "val"))
              if isinstance(s, (int, float))]
    avg = sum(scores) / len(scores) if scores else 0
    return (len(e.get("coverage", [])), avg)


def align_chain_results(raw: dict) -> list:
    chain = _extract_chain(raw.get("chain", {}))
    deep = _extract_deep(raw.get("deep", {}))
    val = _extract_val(raw.get("val", {}))
    cycle = _extract_cycle(raw.get("cycle", {}))
    codes = set(chain) | set(deep) | set(val) | set(cycle)
    out = []
    for code in codes:
        entry = {
            "code": code,
            "name": (chain.get(code, {}).get("name") or deep.get(code, {}).get("name")
                     or val.get(code, {}).get("name") or cycle.get(code, {}).get("name", "")),
            "chain": chain.get(code),
            "deep": deep.get(code),
            "val": val.get(code),
            "cycle": cycle.get(code),
        }
        entry["coverage"] = [k for k in ("chain", "deep", "val", "cycle") if entry[k]]
        scores = [s for s in (_entry_score(entry, k) for k in ("chain", "deep", "val"))
                  if isinstance(s, (int, float))]
        entry["consistency"] = _consistency(scores) if len(scores) >= 2 else {"label": "仅单路径", "range": None}
        out.append(entry)
    out.sort(key=_sort_key, reverse=True)
    return out


def align_stock_results(raw: dict) -> list:
    deep = _extract_deep(raw.get("deep", {}))
    val = _extract_val(raw.get("val", {}))
    # stock 模式:deep --stock 跑板块候选（含目标股），只取目标股做 deep+val 交叉，不展开板块同业
    target = (raw.get("deep", {}) or {}).get("stock_code") or (raw.get("val", {}) or {}).get("stock_code")
    codes = {target} if target else (set(deep) | set(val))
    out = []
    for code in codes:
        entry = {
            "code": code,
            "name": (deep.get(code, {}) or {}).get("name") or (val.get(code, {}) or {}).get("name", ""),
            "deep": deep.get(code),
            "val": val.get(code),
        }
        entry["coverage"] = [k for k in ("deep", "val") if entry[k]]
        scores = [s for s in (_entry_score(entry, k) for k in ("deep", "val"))
                  if isinstance(s, (int, float))]
        entry["consistency"] = _consistency(scores) if len(scores) == 2 else {"label": "仅单路径", "range": None}
        out.append(entry)
    out.sort(key=_sort_key, reverse=True)
    return out


def render_aligned_text(aligned: list, mode: str = "chain") -> str:
    """对齐表文本（供 LLM 输入）。"""
    if mode == "chain":
        lines = ["| 代码 | 名称 | chain | deep | val | cycle | 一致性 |",
                 "|------|------|-------|------|-----|-------|--------|"]
        for e in aligned:
            c = e.get("chain") or {}
            d = e.get("deep") or {}
            v = e.get("val") or {}
            cy = e.get("cycle") or {}
            lines.append(f"| {e['code']} | {e['name']} | {c.get('score','-')}({c.get('role','-')}) | "
                         f"{d.get('total','-')} | {v.get('score','-')}({v.get('role','-')}) | "
                         f"{cy.get('classification','-')}({cy.get('eps_at_peak','-')}) | "
                         f"{e['consistency']['label']} |")
    else:
        lines = ["| 代码 | 名称 | deep | val | 一致性 |",
                 "|------|------|------|-----|--------|"]
        for e in aligned:
            d = e.get("deep") or {}
            v = e.get("val") or {}
            lines.append(f"| {e['code']} | {e['name']} | {d.get('total','-')} | "
                         f"{v.get('score','-')}({v.get('role','-')}) | {e['consistency']['label']} |")
    return "\n".join(lines)


def render_paths_summary(raw: dict, mode: str = "chain") -> str:
    """各路径状态摘要。"""
    keys = ("chain", "deep", "val") if mode == "chain" else ("deep", "val")
    lines = []
    for k in keys:
        r = raw.get(k, {})
        if "error" in r:
            lines.append(f"- {k}: ❌ 失败 ({str(r['error'])[:80]})")
        else:
            lines.append(f"- {k}: ✓ 完成")
    return "\n".join(lines)
