"""cycle-lens 数据采集（周期专属，分析侧）。

按 SPEC §5：price_hist / eps_hist / current_pe / market_cap / predict_eps / news_text / research_text。
全部 try/except，失败返回空/None，不阻塞（graceful degradation）。
板块模式由 sector_data.gather 提供候选池，这里只采单只的周期专属数据。
"""

import sys
from datetime import datetime, timedelta
from typing import Optional

from chain_agent import config


def _to_float(v) -> Optional[float]:
    try:
        if v is None or v == "" or v == "-":
            return None
        return float(v)
    except Exception:
        return None


def _quarter_ends(n: int = 8) -> list:
    """最近 n 个已结束的季度末日期（YYYY-MM-DD），按时间升序。"""
    import calendar
    today = datetime.now()
    y, m = today.year, today.month
    # 对齐到 <= 当前月的最大季度末月（3/6/9/12）
    recent_q = max(q for q in (3, 6, 9, 12) if q <= m)
    # 若该季度还没结束（如 6月但今天<6月30），用上一季度
    last_day = calendar.monthrange(y, recent_q)[1]
    if today < datetime(y, recent_q, last_day):
        recent_q -= 3
        if recent_q <= 0:
            recent_q += 12
            y -= 1
    # 从该季度起回退 n 个季度末
    ends = []
    cy, cm = y, recent_q
    for _ in range(n):
        ld = calendar.monthrange(cy, cm)[1]
        ends.append(datetime(cy, cm, ld).strftime("%Y-%m-%d"))
        cm -= 3
        if cm <= 0:
            cm += 12
            cy -= 1
    return sorted(ends)


def collect(stock_code: str, days: int = 14) -> dict:
    """返回 {price_hist, eps_hist, current_pe, market_cap, predict_eps, news_text, research_text}。

    price_hist: 近8季度末收盘价 [{date, close}]
    eps_hist: 近8季度摊薄EPS(累计) [{date, eps}]
    predict_eps: {this, next} 最新研报前瞻 EPS
    """
    out = {"price_hist": [], "eps_hist": [], "current_pe": None, "market_cap": None,
           "predict_eps": {"this": None, "next": None}, "news_text": "", "research_text": ""}

    # 1. 价格历史（新浪 stock_zh_a_daily，东财 stock_zh_a_hist 走 push2his 被封）
    try:
        import akshare as ak
        # 代码 -> sz/sh 前缀
        prefix = "sh" if stock_code.startswith(("6", "9")) else "sz"
        df = ak.stock_zh_a_daily(symbol=f"{prefix}{stock_code}", adjust="qfq")
        if df is not None and not df.empty and "date" in df.columns:
            df["date"] = df["date"].astype(str)
            q_ends = _quarter_ends(8)
            for qe in q_ends:
                sub = df[df["date"] <= qe]
                if not sub.empty:
                    close = _to_float(sub.iloc[-1].get("close"))
                    if close:
                        out["price_hist"].append({"date": qe, "close": close})
            if not out["price_hist"] and not df.empty:
                close = _to_float(df.iloc[-1].get("close"))
                if close:
                    out["price_hist"].append({"date": str(df.iloc[-1].get("date", "")), "close": close})
    except Exception as e:
        print(f"[cycle-lens] price_hist 拉取失败 {stock_code}: {e}", file=sys.stderr)

    # 2. EPS 历史（财务指标，摊薄每股收益，累计值）
    try:
        import akshare as ak
        start_year = str(datetime.now().year - 3)
        df = ak.stock_financial_analysis_indicator(symbol=stock_code, start_year=start_year)
        if df is not None and not df.empty and "日期" in df.columns:
            df["日期"] = df["日期"].astype(str)
            # EPS 列：摊薄每股收益(元) 优先，回退 基本每股收益(元)
            eps_col = None
            for c in ("摊薄每股收益(元)", "基本每股收益(元)", "每股收益(元)"):
                if c in df.columns:
                    eps_col = c
                    break
            if eps_col:
                df_sorted = df.sort_values("日期")
                # 取近8季度
                recent = df_sorted.tail(8)
                for _, row in recent.iterrows():
                    eps = _to_float(row.get(eps_col))
                    if eps is not None:
                        out["eps_hist"].append({"date": str(row["日期"]), "eps": eps})
    except Exception as e:
        print(f"[cycle-lens] eps_hist 拉取失败 {stock_code}: {e}", file=sys.stderr)

    # 3. 当前 PE / 市值
    try:
        from chain_agent.scoring.quotes import get_quote_provider
        q = get_quote_provider().get_quotes([stock_code]).get(stock_code, {}) or {}
        out["current_pe"] = q.get("pe")
        out["market_cap"] = q.get("market_cap")
    except Exception as e:
        print(f"[cycle-lens] quotes 失败 {stock_code}: {e}", file=sys.stderr)

    # 4. 前瞻 EPS（研报）
    try:
        from chain_agent.collectors.stock_data import eastmoney_research_reports
        reports = eastmoney_research_reports(stock_code, max_reports=5)
        if reports:
            # 取最新一篇有 predict_this_year_eps 的
            for r in reports:
                t = _to_float(r.get("predict_this_year_eps"))
                n = _to_float(r.get("predict_next_year_eps"))
                if t is not None:
                    out["predict_eps"] = {"this": t, "next": n}
                    break
            out["research_text"] = "\n".join(
                f"- [{r.get('publish_date','')}] {r.get('org','')} {r.get('rating','')}: {r.get('title','')}"
                for r in reports[:5]
            )
    except Exception as e:
        print(f"[cycle-lens] 研报拉取失败 {stock_code}: {e}", file=sys.stderr)

    # 5. 新闻（财联社 + Tavily）
    out["news_text"] = _collect_news(stock_code, days)
    return out


def _collect_news(stock_code: str, days: int) -> str:
    """财联社 hermes + Tavily 搜 <name> 业绩 EPS CapEx 周期。Tavily 失败切智谱。"""
    # 先拿股票名
    name = stock_code
    try:
        sl = __import__("json").loads(config.STOCK_LIST_JSON.read_text(encoding="utf-8"))
        stocks = sl.get("stocks", sl)
        info = stocks.get(stock_code)
        if isinstance(info, dict) and info.get("name"):
            name = info["name"]
    except Exception:
        pass

    chunks = []
    # 财联社 hermes
    try:
        import json
        data = json.loads(config.HERMES_NEWS_JSON.read_text(encoding="utf-8"))
        news = data.get("news") or []
        cutoff = datetime.now() - timedelta(days=days)
        for n in news:
            pt = n.get("publish_time", "") or ""
            if pt:
                try:
                    if datetime.fromisoformat(pt.replace("Z", "")) < cutoff:
                        continue
                except Exception:
                    pass
            text = f"{n.get('title','')} {str(n.get('content',''))[:300]}"
            if name in text or stock_code in text:
                chunks.append(f"[财联社 {pt[:10]}] {n.get('title','')}")
    except Exception:
        pass

    # Tavily/智谱
    try:
        from chain_agent.collectors.orchestrator import _get_search_provider, _search_failed
        from chain_agent.collectors.zhipu_search import ZhipuSearch
        provider, provider_name = _get_search_provider()
        queries = [
            f"{name} 业绩 EPS 周期 CapEx 2026",
            f"{name} 涨价 供需 缺货 产能 国产替代",
        ]
        for q in queries:
            r = None
            src = provider_name
            if provider:
                try:
                    r = provider.search_with_ai_summary(q, max_results=4)
                except Exception:
                    r = None
                if (r is None or _search_failed(r)) and src == "tavily" and config.ZHIPU_API_KEY:
                    try:
                        r = ZhipuSearch().search_with_ai_summary(q, max_results=4)
                    except Exception:
                        r = None
            if r and (r.get("results") or r.get("answer")):
                if r.get("answer"):
                    chunks.append(f"[{src}摘要] {r['answer'][:300]}")
                for res in (r.get("results") or [])[:3]:
                    chunks.append(f"[{src}] {res.get('title','')}: {str(res.get('content',''))[:200]}")
    except Exception as e:
        print(f"[cycle-lens] 新闻搜索失败 {stock_code}: {e}", file=sys.stderr)

    return "\n".join(chunks)[:8000]
