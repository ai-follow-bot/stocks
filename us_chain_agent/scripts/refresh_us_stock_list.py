#!/usr/bin/env python3
"""
刷新美股名单 (data/us_stock_list.json)

数据源：Finnhub `/stock/symbol?exchange=US` 一次拉全部美股 Common Stock
- 需环境变量 FINNHUB_API_KEY
- 60 次/分钟限频，本脚本只调 1 次
- 输出 ~8000 只美股（含 NYSE/NASDAQ/AMEX）

输出格式与 a_stock_list.json 对齐：
  {version, update_time, total_count, stocks: {AAPL: {name: "APPLE INC"}, ...}}

用法:
  export FINNHUB_API_KEY=xxx
  python -m us_chain_agent.scripts.refresh_us_stock_list
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from us_chain_agent import config  # noqa: E402  触发代理恢复

FINNHUB_SYMBOLS_URL = "https://finnhub.io/api/v1/stock/symbol"


def _fetch_us_symbols(api_key: str) -> list:
    """拉 US 全部股票，过滤 Common Stock"""
    params = {"exchange": "US", "token": api_key}
    r = requests.get(FINNHUB_SYMBOLS_URL, params=params, timeout=30)
    r.raise_for_status()
    all_items = r.json()
    # 仅保留 Common Stock（剔除 ETF/ADR/Preferred 等）
    return [x for x in all_items if x.get("type") == "Common Stock"]


def main():
    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        print("❌ 缺少 FINNHUB_API_KEY 环境变量", file=sys.stderr)
        sys.exit(1)

    print("📊 拉取美股全名单 (Finnhub /stock/symbol)...", file=sys.stderr)
    try:
        items = _fetch_us_symbols(api_key)
    except Exception as e:
        print(f"❌ 拉取失败: {e}", file=sys.stderr)
        sys.exit(1)

    stocks = {}
    for it in items:
        symbol = (it.get("symbol") or "").strip()
        name = (it.get("description") or it.get("displaySymbol") or "").strip()
        if not symbol or not name:
            continue
        # 跳过优先股、权证等非主股票（通常含 . 后缀的特殊符号）
        if any(symbol.endswith(s) for s in (".W", ".R", ".U")):
            continue
        stocks[symbol] = {"name": name}

    # 补 Finnhub 可能漏掉的关键龙头（ADR 等）
    extras = {
        "TSM": "Taiwan Semiconductor Manufacturing ADR",
        "ASML": "ASML Holding ADR",
        "ARM": "Arm Holdings ADR",
        "TM": "Toyota Motor ADR",
        "SONY": "Sony Group ADR",
        "NVO": "Novo Nordisk ADR",
        "RIVN": "Rivian Automotive",
        "LCID": "Lucid Group",
        "SMCI": "Super Micro Computer",
        "PLTR": "Palantir Technologies",
        "NOW": "ServiceNow",
    }
    for sym, name in extras.items():
        stocks.setdefault(sym, {"name": name})

    out = {
        "version": "1.0",
        "update_time": datetime.now().isoformat(),
        "total_count": len(stocks),
        "stocks": stocks,
    }

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_file = config.US_STOCK_LIST_JSON
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"✅ 已保存: {out_file}", file=sys.stderr)
    print(f"   总数: {len(stocks)} 只美股", file=sys.stderr)
    # 抽样
    for sym in ("AAPL", "NVDA", "MSFT", "TSM", "ASML", "GOOG", "AMZN", "META"):
        if sym in stocks:
            print(f"   {sym}: {stocks[sym]['name']}", file=sys.stderr)


if __name__ == "__main__":
    main()
