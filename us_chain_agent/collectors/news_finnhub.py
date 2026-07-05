"""
美股个股新闻采集器（Finnhub 实现）

接口对齐 chain_agent.collectors.news_akshare.collect_stock_news：
  collect_stock_news(symbols: List[str], days: int = 7) -> Dict

Returns: {source, sector, news_count, news: [{title, content, publish_time, source, url}], content_text, error}

数据源：Finnhub /company-news?symbol=...&from=YYYY-MM-DD&to=YYYY-MM-DD
限频：60 次/分钟，每 symbol 1 次，主动 sleep 0.5s
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import requests

from us_chain_agent import config


def _get_company_news(session: requests.Session, symbol: str, days: int) -> list:
    """拉单只 ticker 的近期新闻"""
    today = datetime.now(timezone.utc).date()
    from_date = today - timedelta(days=days)
    url = f"{config.FINNHUB_BASE_URL}/company-news"
    params = {
        "symbol": symbol,
        "from": from_date.isoformat(),
        "to": today.isoformat(),
        "token": config.FINNHUB_API_KEY,
    }
    last_err = None
    for attempt in range(3):
        try:
            r = session.get(url, params=params, timeout=15)
            if r.status_code == 429:
                wait = 2 ** attempt
                print(f"[NewsFinnhub] {symbol} 429 限频，等 {wait}s", flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1)
    print(f"[NewsFinnhub] {symbol} 拉取失败: {last_err}", flush=True)
    return []


def collect_stock_news(leader_codes: List[str], days: int = 7) -> Dict:
    """拉龙头股个股新闻（接口对齐 A 股 news_akshare.collect_stock_news）"""
    if not leader_codes:
        return {
            "source": "finnhub",
            "sector": "stock_news",
            "news_count": 0,
            "news": [],
            "content_text": "",
            "error": "no symbols",
        }

    session = requests.Session()
    all_news = []
    # 与 A 股一致：最多 6 只龙头
    for sym in leader_codes[:6]:
        time.sleep(0.5)  # 限速
        items = _get_company_news(session, sym, days)
        for it in items:
            ts = it.get("datetime") or 0
            try:
                pub_time = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pub_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            title = (it.get("headline") or "").strip()
            content = (it.get("summary") or "").strip()[:500]
            if not title:
                continue
            all_news.append({
                "title": title,
                "content": content,
                "publish_time": pub_time,
                "source": f"finnhub:{sym}",
                "url": it.get("url") or "",
            })

    # 去重（按 title 前 50 字符）
    seen = set()
    deduped = []
    for n in all_news:
        key = n["title"][:50]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(n)
    deduped.sort(key=lambda x: x["publish_time"], reverse=True)
    deduped = deduped[:100]

    chunks = [
        f"[{n['publish_time']}] {n['title']} | {n['content']}"
        for n in deduped[:50]
    ]

    return {
        "source": "finnhub",
        "sector": "stock_news",
        "news_count": len(deduped),
        "news": deduped,
        "content_text": "\n".join(chunks),
        "error": None if deduped else "no news matched",
    }


if __name__ == "__main__":
    import sys
    syms = sys.argv[1:] or ["AAPL", "NVDA"]
    r = collect_stock_news(syms, days=7)
    print(f"finnhub 个股新闻: {r['news_count']} 条, error: {r.get('error')}")
    for n in r["news"][:5]:
        print(f"  - [{n['publish_time']}] {n['title'][:80]}")
        print(f"    src: {n['source']}  url: {n['url'][:80]}")
