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


def _fetch_index() -> str:
    """拉三大指数近 60 日，算近 5/20 日涨跌。akshare 偶发断连，每只重试 2 次。"""
    lines = []
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
    for name, sym in _INDEX_SYMBOLS:
        df = None
        for attempt in range(2):
            try:
                df = ak.stock_zh_index_daily_em(symbol=sym)
                break
            except Exception as e:
                if attempt == 0:
                    time.sleep(0.5)
                else:
                    print(f"[ce-value] 指数 {name} 拉取失败: {str(e)[:80]}", file=sys.stderr)
        if df is None or getattr(df, "empty", True):
            lines.append(f"- {name}: (拉取失败)")
            continue
        try:
            df = df.sort_values("date" if "date" in df.columns else df.columns[0], ascending=False)
            close_col = "close" if "close" in df.columns else None
            if close_col is None:
                lines.append(f"- {name}: (无收盘列)")
                continue
            closes = df[close_col].astype(float).reset_index(drop=True)
            last = closes.iloc[0]
            chg5 = ((last / closes.iloc[4]) - 1) * 100 if len(closes) > 4 else None
            chg20 = ((last / closes.iloc[19]) - 1) * 100 if len(closes) > 19 else None
            lines.append(f"- {name}: 收盘 {last:.2f}，近5日 {chg5:+.1f}%，近20日 {chg20:+.1f}%" if chg5 is not None else f"- {name}: 收盘 {last:.2f}")
        except Exception as e:
            lines.append(f"- {name}: (解析失败 {str(e)[:60]})")
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
