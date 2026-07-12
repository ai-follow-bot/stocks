"""cycle-lens 确定性分解（SPEC §7）：EPS×PE 分解 + 分类 + 分位 + 峰值判定。

全部代码算（非 LLM）。数据不足标"无法分类"，不抛异常。
"""

from typing import Optional


def _single_quarter_eps(eps_hist: list) -> list:
    """A股季度EPS是累计（Q1/H1/9M/FY）-> 单季EPS = 本期累计 - 上期累计（年初重置）。

    返回 [{date, single}]，长度同 eps_hist（首项若 Q1 则 single=累计，否则 None）。
    """
    out = []
    for i, e in enumerate(eps_hist):
        d = e["date"][:7]  # YYYY-MM
        month = int(d.split("-")[1]) if "-" in d else 0
        if i == 0 or month == 3:
            # Q1（年初重置）或首项：单季 = 累计
            out.append({"date": e["date"], "single": e["eps"]})
        else:
            prev = eps_hist[i - 1]["eps"]
            cur = e["eps"]
            # 同年：单季 = 本期累计 - 上期累计；跨年（上期是 FY12月）则单季=本期累计（Q1）
            prev_year = eps_hist[i - 1]["date"][:4]
            cur_year = e["date"][:4]
            if cur_year == prev_year and cur is not None and prev is not None:
                out.append({"date": e["date"], "single": cur - prev})
            else:
                out.append({"date": e["date"], "single": cur})
    return out


def _ttm_eps_series(single_series: list) -> list:
    """TTM EPS = 近4季单季EPS之和。返回 [{date, ttm}]，前3项 ttm=None（不足4季）。"""
    out = []
    for i, s in enumerate(single_series):
        if i < 3:
            out.append({"date": s["date"], "ttm": None})
        else:
            last4 = [single_series[j]["single"] for j in range(i - 3, i + 1)]
            if any(v is None for v in last4):
                out.append({"date": s["date"], "ttm": None})
            else:
                out.append({"date": s["date"], "ttm": sum(last4)})
    return out


def _pe_series(price_hist: list, ttm_series: list) -> list:
    """PE = 季度末收盘价 / TTM_EPS。按 date 对齐。返回 [{date, pe}]。"""
    price_map = {p["date"][:7]: p["close"] for p in price_hist}
    out = []
    for t in ttm_series:
        ym = t["date"][:7]
        close = price_map.get(ym)
        if close and t["ttm"] and t["ttm"] > 0:
            out.append({"date": t["date"], "pe": close / t["ttm"]})
        else:
            out.append({"date": t["date"], "pe": None})
    return out


def _percentile(value: float, series: list) -> Optional[float]:
    """value 在 series（去 None）中的分位 [0,1]。"""
    vals = sorted(v for v in series if v is not None and v > 0)
    if not vals:
        return None
    below = sum(1 for v in vals if v <= value)
    return below / len(vals)


def decompose(price_hist: list, eps_hist: list, current_pe: Optional[float] = None,
              predict_eps: Optional[float] = None) -> dict:
    """返回 {ttm_eps_series, pe_series, eps_contrib, pe_contrib, classification,
    pe_percentile, eps_at_peak, forward_pe, current_price, current_ttm_eps}。"""
    res = {"ttm_eps_series": [], "pe_series": [], "eps_contrib": None,
           "pe_contrib": None, "classification": "无法分类",
           "pe_percentile": None, "eps_at_peak": "未知", "forward_pe": None,
           "current_price": None, "current_ttm_eps": None}

    if len(eps_hist) < 4 or len(price_hist) < 2:
        res["classification"] = "数据不足"
        return res

    single = _single_quarter_eps(eps_hist)
    ttm = _ttm_eps_series(single)
    pe = _pe_series(price_hist, ttm)
    res["ttm_eps_series"] = ttm
    res["pe_series"] = pe

    # 当前 TTM EPS / 价格（最近一季有值的）
    cur_ttm = next((t["ttm"] for t in reversed(ttm) if t["ttm"]), None)
    cur_price = price_hist[-1]["close"] if price_hist else None
    res["current_ttm_eps"] = cur_ttm
    res["current_price"] = cur_price

    # 分解：近N季（有值的 TTM + PE 首尾）
    valid = [(t["ttm"], p["pe"]) for t, p in zip(ttm, pe) if t["ttm"] and p["pe"]]
    if len(valid) >= 2:
        first_ttm, first_pe = valid[0]
        last_ttm, last_pe = valid[-1]
        if first_ttm and first_ttm > 0 and first_pe and first_pe > 0:
            res["eps_contrib"] = last_ttm / first_ttm - 1  # EPS 涨跌 %
            res["pe_contrib"] = last_pe / first_pe - 1     # PE 涨跌 %

    # PE 历史分位（用 current_pe 优先，否则 last PE）
    pe_vals = [p["pe"] for p in pe]
    ref_pe = current_pe if current_pe else (valid[-1][1] if len(valid) >= 1 else None)
    if ref_pe:
        res["pe_percentile"] = _percentile(ref_pe, pe_vals)

    # EPS 峰值判定：单季 EPS 近4季趋势
    singles = [s["single"] for s in single if s["single"] is not None]
    if len(singles) >= 4:
        last4 = singles[-4:]
        # 同比：最近单季 vs 4季前
        yoy = (last4[-1] - last4[0]) / abs(last4[0]) if last4[0] and last4[0] != 0 else None
        # 近2季趋势
        recent_up = last4[-1] >= last4[-2] if len(last4) >= 2 else None
        if yoy is not None and yoy < 0:
            res["eps_at_peak"] = "同比转负"
        elif last4[-1] < last4[-2] < last4[-3]:
            res["eps_at_peak"] = "见顶回落"
        elif recent_up:
            res["eps_at_peak"] = "仍在升"
        else:
            res["eps_at_peak"] = "高位震荡"

    # 前瞻 PE
    if predict_eps and cur_price and predict_eps > 0:
        res["forward_pe"] = cur_price / predict_eps

    # 分类 A/B/C
    ec = res["eps_contrib"]
    pc = res["pe_contrib"]
    pct = res["pe_percentile"]
    if ec is not None and pc is not None:
        if ec > 0.05 and pc > 0.20 and pc > ec:
            res["classification"] = "A 泡沫型"  # EPS↑ + PE↑↑（PE 扩张 > EPS 增速）
        elif ec > 0.20 and pc <= 0.10:
            res["classification"] = "B 业绩型"  # EPS↑↑ + PE 稳/降
        elif pct is not None and pct < 0.30 and res["eps_at_peak"] in ("同比转负", "见顶回落"):
            res["classification"] = "C 周期陷阱"  # PE 低分位 但 EPS 见顶/转负
        elif ec > 0.05:
            res["classification"] = "偏业绩型"  # EPS 推动但 PE 未明显压
        elif ec is not None and ec < 0:
            res["classification"] = "偏周期下行"
        else:
            res["classification"] = "震荡"

    return res
