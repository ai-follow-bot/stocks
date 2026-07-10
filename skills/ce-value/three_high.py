"""431 三高筛选：高增长 / 高利润 / 高围墙，确定性打分（软阈值连续模型）。

- 高增长 = 财务增速（营收/利润，ramp 到 0-100）+ deep「业绩兑现」维度
- 高利润 = 财务毛利率/净利率/ROE（ramp 到 0-100）
- 高围墙 = val「稀缺」+ deep「国产替代」维度（均 0-100，复用）

财务数据来自 financials.py（akshare）；维度分来自 harness aligned。
缺数据时该维度用可用部分或标 None，不阻塞。三高达标 = 三维均 ≥70。
"""

from typing import Optional

# 三高达标阈值
HIGH_THRESHOLD = 70


def _ramp(v: Optional[float], lo: float, hi: float) -> Optional[float]:
    """软阈值：v<=lo->0, v>=hi->100, 中间线性。v 为 None 返回 None。"""
    if v is None:
        return None
    if hi <= lo:
        return 0.0
    if v <= lo:
        return 0.0
    if v >= hi:
        return 100.0
    return (v - lo) / (hi - lo) * 100.0


def _avg(vals) -> Optional[float]:
    """非 None 值的平均；全 None 返回 None。"""
    xs = [v for v in vals if v is not None]
    if not xs:
        return None
    return sum(xs) / len(xs)


def _score_growth(fin: dict, deep_dims: dict) -> Optional[float]:
    """高增长：财务增速(ramp) + deep 业绩兑现，各 0-100 后均值。"""
    rev = fin.get("rev_growth")
    prof = fin.get("profit_growth")
    fin_part = _avg([_ramp(rev, -10, 40), _ramp(prof, -10, 50)])
    er = deep_dims.get("earnings_realization") if deep_dims else None
    if er is not None:
        er = float(er)
    return _avg([fin_part, er]) if (fin_part is not None or er is not None) else None


def _score_profit(fin: dict) -> Optional[float]:
    """高利润：毛利率/净利率/ROE ramp 到 0-100 后均值。"""
    gm = _ramp(fin.get("gross_margin"), 15, 55)
    nm = _ramp(fin.get("net_margin"), 3, 25)
    roe = _ramp(fin.get("roe"), 5, 20)
    return _avg([gm, nm, roe])


def _score_moat(val_dims: dict, deep_dims: dict) -> Optional[float]:
    """高围墙：val 稀缺 + deep 国产替代，均 0-100，均值。"""
    sc = val_dims.get("scarcity") if val_dims else None
    ds = deep_dims.get("domestic_substitution") if deep_dims else None
    if sc is not None:
        sc = float(sc)
    if ds is not None:
        ds = float(ds)
    return _avg([sc, ds])


def score_one(entry: dict, fin: dict) -> dict:
    """对单只 aligned 标的打三高分。

    entry: harness aligned 项 {code, name, chain, deep:{total, dims}, val:{score, dims}, ...}
    fin: financials.get_financials(code) 结果（可能 {}）
    返回 {growth, profit, moat, composite, three_high(bool), note}
    """
    deep = entry.get("deep") or {}
    val = entry.get("val") or {}
    deep_dims = deep.get("dims") or {}
    val_dims = val.get("dims") or {}

    growth = _score_growth(fin, deep_dims)
    profit = _score_profit(fin)
    moat = _score_moat(val_dims, deep_dims)

    dims_present = [d for d in (growth, profit, moat) if d is not None]
    composite = sum(dims_present) / len(dims_present) if dims_present else None

    three_high = (
        growth is not None and profit is not None and moat is not None
        and growth >= HIGH_THRESHOLD and profit >= HIGH_THRESHOLD and moat >= HIGH_THRESHOLD
    )

    flags = []
    if growth is not None and growth >= HIGH_THRESHOLD:
        flags.append("高增长")
    if profit is not None and profit >= HIGH_THRESHOLD:
        flags.append("高利润")
    if moat is not None and moat >= HIGH_THRESHOLD:
        flags.append("高围墙")

    missing = []
    if growth is None:
        missing.append("增长")
    if profit is None:
        missing.append("利润")
    if moat is None:
        missing.append("围墙")

    return {
        "growth": round(growth, 1) if growth is not None else None,
        "profit": round(profit, 1) if profit is not None else None,
        "moat": round(moat, 1) if moat is not None else None,
        "composite": round(composite, 1) if composite is not None else None,
        "three_high": three_high,
        "flags": flags,
        "missing": missing,
    }


def score_batch(aligned: list, financials: dict) -> list:
    """对 aligned 列表逐只打三高分，写回每项的 'three_high' 字段，返回原 list。"""
    for entry in aligned:
        code = entry.get("code")
        fin = financials.get(code, {}) or {}
        entry["three_high"] = score_one(entry, fin)
    # 按 composite 降序（None 沉底）
    def key(e):
        c = (e.get("three_high") or {}).get("composite")
        return (c is not None, c if c is not None else -1)
    aligned.sort(key=key, reverse=True)
    return aligned
