"""三高财务数据拉取（高增长 / 高利润）。

用 akshare `stock_financial_analysis_indicator` 一次拉全：销售毛利率 / 销售净利率 /
净资产收益率(ROE) / 主营业务收入增长率 / 净利润增长率，取最近一期。

高围墙（护城河）不在此处--由 valuation-lens「稀缺」+ deep-analyze「国产替代」维度分
复用，见 three_high.py。

所有拉取 try/except，失败返回空 dict，不阻塞 pipeline。tqdm 进度条走 stderr（akshare
默认），不会污染 stdout 的 --json。
"""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import akshare as ak


def _to_float(v) -> Optional[float]:
    """容错转 float：akshare 字段可能是 None/'-'/'None'/str。"""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "None", "nan", "NaN"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def get_financials(code: str) -> dict:
    """拉单只股票的三高财务指标（最近一期）。

    返回 {period, gross_margin, net_margin, roe, rev_growth, profit_growth}；
    失败/空返回 {}。code 为 6 位代码（akshare 要不带前缀的代码）。
    """
    code = str(code).strip()
    try:
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year="2022")
    except Exception as e:
        print(f"[ce-value] 财务拉取失败 {code}: {e}", file=sys.stderr)
        return {}
    if df is None or getattr(df, "empty", True) or "日期" not in df.columns:
        return {}
    try:
        df = df.sort_values("日期", ascending=False)
        row = df.iloc[0]
    except Exception:
        return {}
    return {
        "period": str(row.get("日期", "")),
        "gross_margin": _to_float(row.get("销售毛利率(%)")),
        "net_margin": _to_float(row.get("销售净利率(%)")),
        "roe": _to_float(row.get("净资产收益率(%)")),
        "rev_growth": _to_float(row.get("主营业务收入增长率(%)")),
        "profit_growth": _to_float(row.get("净利润增长率(%)")),
    }


def get_financials_batch(codes: List[str], max_workers: int = 4) -> Dict[str, dict]:
    """并发拉多只股票财务。返回 {code: financials}，失败项值为 {}。

    限流：max_workers 默认 4，平衡速度与 akshare 风控。候选池通常 top_n*2 ≤ 30 只。
    """
    codes = [c for c in codes if c]
    out: Dict[str, dict] = {}
    if not codes:
        return out
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(get_financials, c): c for c in codes}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                out[c] = fut.result()
            except Exception as e:
                print(f"[ce-value] 财务并发失败 {c}: {e}", file=sys.stderr)
                out[c] = {}
    hit = sum(1 for v in out.values() if v)
    print(f"[ce-value] 财务拉取完成: {hit}/{len(codes)} 只有数据", file=sys.stderr)
    return out
