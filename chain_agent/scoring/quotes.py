"""
Layer 4: 行情源抽象（双源可切换）

- AkshareQuoteProvider（默认）：akshare.stock_zh_a_spot_em() 一次性拉全 A 股行情
- EasyquotationQuoteProvider（可选）：easyquotation.use('qq').stocks(codes)

通过环境变量 QUOTE_PROVIDER 切换。
"""

from abc import ABC, abstractmethod
from typing import Dict, List

from .. import config


class QuoteProvider(ABC):
    """行情源抽象基类"""

    @abstractmethod
    def get_quotes(self, codes: List[str]) -> Dict[str, Dict]:
        """
        返回 {code: {name, pe, market_cap, change_pct}}
        """


class AkshareQuoteProvider(QuoteProvider):
    """akshare 行情源"""
    _cache = None  # 全 A 股行情缓存（类级，多实例共享，一次性拉）

    def __init__(self):
        try:
            import akshare as ak
        except ImportError as e:
            raise ImportError("akshare 未安装，请运行: pip install akshare") from e
        self._ak = ak
        # _cache 为类属性，多实例共享（避免重复批量拉全 A 股行情）

    def _ensure_cache(self):
        if AkshareQuoteProvider._cache is not None:
            return
        import time
        last_err = None
        for attempt in range(3):
            try:
                df = self._ak.stock_zh_a_spot_em()
                # 字段：代码, 名称, 最新价, 涨跌幅, 总市值, 市盈率-动态, ...
                AkshareQuoteProvider._cache = {}
                for _, row in df.iterrows():
                    code = str(row.get("代码", ""))
                    if not code:
                        continue
                    pe_raw = row.get("市盈率-动态")
                    try:
                        pe = float(pe_raw) if pe_raw not in (None, "-", "") else None
                        if pe is not None and pe <= 0:
                            pe = None
                    except Exception:
                        pe = None
                    cap_raw = row.get("总市值")
                    try:
                        cap = float(cap_raw) / 1e8 if cap_raw not in (None, "-") else 0.0  # 转亿元
                    except Exception:
                        cap = 0.0
                    chg_raw = row.get("涨跌幅")
                    try:
                        chg = float(chg_raw) if chg_raw not in (None, "-") else 0.0
                    except Exception:
                        chg = 0.0
                    chg60_raw = row.get("60日涨跌幅")
                    try:
                        chg60 = float(chg60_raw) if chg60_raw not in (None, "-") else None
                    except Exception:
                        chg60 = None
                    AkshareQuoteProvider._cache[code] = {
                        "name": str(row.get("名称", "")),
                        "pe": pe,
                        "market_cap": cap,
                        "change_pct": chg,
                        "change_60d": chg60,
                    }
                if attempt > 0:
                    print(f"[AkshareQuote] 第 {attempt+1} 次重试成功，"
                          f"拉到 {len(AkshareQuoteProvider._cache)} 只", flush=True)
                return
            except Exception as e:
                last_err = e
                print(f"[AkshareQuote] 拉取行情失败 (尝试 {attempt+1}/3): {e}",
                      flush=True)
                if attempt < 2:
                    time.sleep(2)
        print(f"[AkshareQuote] 3 次重试全失败，PE/市值将为 null", flush=True)
        AkshareQuoteProvider._cache = {}

    def get_quotes(self, codes: List[str]) -> Dict[str, Dict]:
        self._ensure_cache()
        return {c: AkshareQuoteProvider._cache.get(c, {}) for c in codes if c in AkshareQuoteProvider._cache}


class EasyquotationQuoteProvider(QuoteProvider):
    """easyquotation 行情源（可选）"""

    def __init__(self):
        try:
            import easyquotation
        except ImportError as e:
            raise ImportError(
                "easyquotation 未安装，请运行: pip install easyquotation"
                " 或切换 QUOTE_PROVIDER=akshare"
            ) from e
        try:
            self._q = easyquotation.use("qq")
        except Exception as e:
            raise RuntimeError(f"easyquotation 初始化失败: {e}")

    def get_quotes(self, codes: List[str]) -> Dict[str, Dict]:
        if not codes:
            return {}
        try:
            data = self._q.stocks(codes)
        except Exception as e:
            print(f"[EasyquotationQuote] 拉取失败: {e}")
            return {}
        out = {}
        for code, info in data.items():
            if code not in codes:
                continue
            pe = info.get("PE")
            try:
                pe = float(pe) if pe else None
                if pe is not None and pe <= 0:
                    pe = None
            except Exception:
                pe = None
            cap = info.get("总市值", 0) or info.get("market_cap", 0) or 0
            try:
                cap = float(cap)
            except Exception:
                cap = 0.0
            chg = info.get("涨跌(%)", 0) or 0
            try:
                chg = float(chg)
            except Exception:
                chg = 0.0
            out[code] = {
                "name": info.get("name", ""),
                "pe": pe,
                "market_cap": cap,
                "change_pct": chg,
            }
        return out


def get_quote_provider() -> QuoteProvider:
    """根据 config.QUOTE_PROVIDER 创建行情源实例"""
    name = config.QUOTE_PROVIDER
    if name == "easyquotation":
        try:
            return EasyquotationQuoteProvider()
        except Exception as e:
            print(f"[QuoteProvider] easyquotation 不可用 ({e})，降级 akshare")
            return AkshareQuoteProvider()
    return AkshareQuoteProvider()
