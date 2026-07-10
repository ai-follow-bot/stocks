"""431 市场层：指数走势 + 北向资金 + 财联社情绪 -> LLM 简报。

风格 / 周期 / 资金 / 情绪 四姿态。akshare 指数偶发连接断开（重试 + 降级）。
融资融券市场级口径不稳，资金面以 北向 为主；个股融资融券在公司层由 stock_data 提供。
"""

import json
import sys
import time
from datetime import datetime, timedelta
from typing import List

import akshare as ak

from chain_agent import config
from . import common, prompts


_INDEX_SYMBOLS = [
    ("上证指数", "sh000001"),
    ("创业板指", "sz399006"),
    ("沪深300", "sh000300"),
]


def _fetch_one_index(name: str, sym: str) -> str:
    """单指数：优先腾讯源(stock_zh_index_daily_tx，东财 push2 已封服务器 IP)，
    失败回退东财(stock_zh_index_daily_em)。算近 5/20 日涨跌。"""
    df = None
    for fn_name in ("stock_zh_index_daily_tx", "stock_zh_index_daily_em"):
        try:
            fn = getattr(ak, fn_name)
            df = fn(symbol=sym)
            if df is not None and not getattr(df, "empty", True):
                break
        except Exception as e:
            print(f"[ce-value] 指数 {name} {fn_name} 失败: {str(e)[:60]}", file=sys.stderr)
            df = None
    if df is None or getattr(df, "empty", True):
        return f"- {name}: (拉取失败)"
    try:
        date_col = "date" if "date" in df.columns else df.columns[0]
        close_col = "close" if "close" in df.columns else None
        if close_col is None:
            return f"- {name}: (无收盘列)"
        df = df.sort_values(date_col, ascending=False)
        closes = df[close_col].astype(float).reset_index(drop=True)
        last = closes.iloc[0]
        chg5 = ((last / closes.iloc[4]) - 1) * 100 if len(closes) > 4 else None
        chg20 = ((last / closes.iloc[19]) - 1) * 100 if len(closes) > 19 else None
        if chg5 is not None:
            return f"- {name}: 收盘 {last:.2f}，近5日 {chg5:+.1f}%，近20日 {chg20:+.1f}%"
        return f"- {name}: 收盘 {last:.2f}"
    except Exception as e:
        return f"- {name}: (解析失败 {str(e)[:60]})"


def _fetch_index() -> str:
    """拉三大指数近 60 日涨跌。腾讯源全历史较慢(~20s/只)，3 只并发拉。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    lines = [""] * len(_INDEX_SYMBOLS)
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(_fetch_one_index, name, sym): i
                for i, (name, sym) in enumerate(_INDEX_SYMBOLS)}
        for fut in as_completed(futs):
            lines[futs[fut]] = fut.result()
    return "\n".join(lines) if lines else "(指数全部拉取失败)"


def _fetch_northbound() -> str:
    """北向资金近期净流入汇总。"""
    try:
        df = ak.stock_hsgt_fund_flow_summary_em()
        if df is None or getattr(df, "empty", True):
            return "(北向数据为空)"
        # 取最近若干交易日
        df = df.head(8)
        lines = []
        for _, r in df.iterrows():
            date = r.get("交易日", "")
            direction = r.get("资金方向", "")
            net = r.get("资金净流入", 0)
            try:
                net_f = float(net) if net not in (None, "") else 0.0
                lines.append(f"- {date} {direction}: 净流入 {net_f:.1f} 亿")
            except (ValueError, TypeError):
                continue
        return "\n".join(lines) if lines else "(北向解析为空)"
    except Exception as e:
        print(f"[ce-value] 北向拉取失败: {str(e)[:80]}", file=sys.stderr)
        return "(北向拉取失败)"


def _fetch_sentiment(limit: int = 30) -> List[str]:
    """财联社最近 limit 条标题作市场情绪信号。"""
    try:
        data = json.loads(config.HERMES_NEWS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return []
    news = data.get("news", []) or []
    return [item.get("title", "") for item in news[:limit] if item.get("title")]


def run_market_briefing() -> dict:
    """返回 {style, cycle, capital, sentiment, favor_style, summary, data_quality}。"""
    print("[ce-value] === 市场层 ===", file=sys.stderr)
    index_text = _fetch_index()
    nb_text = _fetch_northbound()
    sent_titles = _fetch_sentiment(limit=30)
    sent_text = "\n".join(f"- {t}" for t in sent_titles) or "(财联社情绪为空)"

    user = prompts.MARKET_USER_TEMPLATE.format(
        today=datetime.now().strftime("%Y-%m-%d"),
        index_days=60,
        index_data=index_text[:1500],
        capital=nb_text[:800],
        margin="(市场级融资融券未拉取，资金面以北向为主)",
        n=len(sent_titles),
        sentiment=sent_text[:2000],
    )
    data = common._llm_call_json(prompts.MARKET_SYSTEM, user) or {}
    if not data:
        print("[ce-value] 市场简报 LLM 失败，降级空", file=sys.stderr)
    data["data_quality"] = "ok" if data else "degraded"
    return data


if __name__ == "__main__":
    print(json.dumps(run_market_briefing(), ensure_ascii=False, indent=2))
