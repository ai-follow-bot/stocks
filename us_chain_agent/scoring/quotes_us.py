"""
美股 Layer 4: 行情源（Finnhub 实现）

接口对齐 chain_agent.scoring.quotes.QuoteProvider：
  get_quotes(symbols: List[str]) -> Dict[str, {name, pe, market_cap, change_pct}]

数据源：
  - /quote: 当前价 + 当日涨跌幅 (dp)
  - /stock/metric?metric=all: peTTM + marketCapitalization (单位 M 美元)

字段约定（与 A 股对齐）：
  - market_cap 单位为「亿美元」（marketCapitalization_M / 100）
  - pe 取 peTTM
  - change_pct 取当日涨跌幅 dp

限频：Finnhub 免费版 60 次/分钟，每 symbol 调 2 次 = 7 龙头 14 次，够用。
每次调用 sleep 0.5s 防触发限频，429 时指数退避重试 3 次。
"""

import time
from typing import Dict, List

import requests

from us_chain_agent import config


class FinnhubQuoteProvider:
    """Finnhub 美股行情源（实现 A 股 QuoteProvider 协议）"""

    def __init__(self):
        self._api_key = config.FINNHUB_API_KEY
        self._base = config.FINNHUB_BASE_URL
        self._session = requests.Session()
        # 缓存 profile/metric 避免重复调用
        self._metric_cache: Dict[str, Dict] = {}
        self._quote_cache: Dict[str, Dict] = {}

    def _get_with_retry(self, path: str, params: dict, max_retries: int = 3) -> dict | list:
        url = f"{self._base}{path}"
        params = {**params, "token": self._api_key}
        last_err = None
        for attempt in range(max_retries):
            try:
                r = self._session.get(url, params=params, timeout=15)
                if r.status_code == 429:
                    wait = 2 ** attempt
                    print(f"[FinnhubQuote] 429 限频，等 {wait}s 重试", flush=True)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_err = e
                if attempt < max_retries - 1:
                    time.sleep(1)
        print(f"[FinnhubQuote] {path} 3 次重试失败: {last_err}", flush=True)
        return {}

    def _get_metric(self, symbol: str) -> dict:
        if symbol in self._metric_cache:
            return self._metric_cache[symbol]
        time.sleep(0.5)  # 主动限速
        data = self._get_with_retry(
            "/stock/metric", {"symbol": symbol, "metric": "all"}
        )
        metric = data.get("metric", {}) if isinstance(data, dict) else {}
        self._metric_cache[symbol] = metric
        return metric

    def _get_quote(self, symbol: str) -> dict:
        if symbol in self._quote_cache:
            return self._quote_cache[symbol]
        time.sleep(0.5)
        data = self._get_with_retry("/quote", {"symbol": symbol})
        self._quote_cache[symbol] = data if isinstance(data, dict) else {}
        return self._quote_cache[symbol]

    def get_quotes(self, codes: List[str]) -> Dict[str, Dict]:
        """返回 {symbol: {name, pe, market_cap, change_pct}}"""
        if not codes:
            return {}
        out = {}
        for sym in codes:
            try:
                metric = self._get_metric(sym)
                quote = self._get_quote(sym)
            except Exception as e:
                print(f"[FinnhubQuote] {sym} 拉取失败: {e}", flush=True)
                continue

            # PE (TTM)
            pe = None
            pe_raw = metric.get("peTTM") or metric.get("peAnnual")
            if pe_raw:
                try:
                    pe = float(pe_raw)
                    if pe <= 0:
                        pe = None
                except Exception:
                    pe = None

            # 市值：Finnhub 返回 M 美元，转亿美元（1 亿 = 100 M）
            market_cap = 0.0
            cap_raw = metric.get("marketCapitalization")
            if cap_raw:
                try:
                    market_cap = float(cap_raw) / 100.0  # M → 亿美元
                except Exception:
                    market_cap = 0.0

            # 当日涨跌幅
            change_pct = 0.0
            dp_raw = quote.get("dp")
            if dp_raw is not None:
                try:
                    change_pct = float(dp_raw)
                except Exception:
                    change_pct = 0.0

            # 公司名（从 metric.series 拿不到，从 profile2 兜底；这里先留空，
            # 由 stock_detector_us 反查 us_stock_list.json 填充）
            out[sym] = {
                "name": "",  # detector 填充
                "pe": pe,
                "market_cap": market_cap,
                "change_pct": change_pct,
            }
        return out


def get_quote_provider() -> FinnhubQuoteProvider:
    return FinnhubQuoteProvider()
