"""
美股 Layer 3: 股票检测器

接口对齐 chain_agent.discovery.stock_detector.StockDetector：
  detect_stocks_from_text(text) -> List[{code, name, sector, match_type, matched_keyword}]
  get_sector_by_text(text) -> Optional[str]

匹配策略：
  1. ticker 正则 \\b[A-Z]{2,5}\\b + 反查 us_stock_list.json 命中
  2. 公司名匹配（不区分大小写）

板块归属：CORE_SECTOR_STOCKS 维护 3 板块龙头，命中即归属。
"""

import json
import re
from typing import Dict, List, Optional

from us_chain_agent import config

# 板块关键词（用于从文本判断板块，英文新闻为主）
_DEFAULT_SECTOR_KEYWORDS: Dict[str, List[str]] = {
    "semiconductors": [
        "semiconductor", "chip", "foundry", "TSMC", "wafer",
        "EUV", "ASML", "EDA", "advanced packaging", "CoWoS",
        "chiplet", "wafer fab", "Intel Foundry", "UMC", "GlobalFoundries",
    ],
    "cpu": [
        "CPU", "processor", "x86", "Arm", "Armv9", "server CPU",
        "datacenter CPU", "PC CPU", "Intel Xeon", "AMD EPYC",
        "Snapdragon", "Apple Silicon", "Ryzen", "Core Ultra",
    ],
    "gpu": [
        "GPU", "graphics processor", "accelerator", "H100", "H200",
        "B100", "B200", "GB200", "Blackwell", "Hopper", "Ada Lovelace",
        "CUDA", "Tensor Core", "NVLink", "datacenter GPU",
        "MI300", "MI325", "MI350", "Gaudi",
    ],
    "tpu": [
        "TPU", "ASIC", "AI accelerator", "custom silicon",
        "Trillium", "Tensor Processing Unit", "MTIA", "Trainium",
        "Inferentia", "domain specific architecture", "DSA",
        "systolic array",
    ],
    "memory": [
        "DRAM", "NAND", "HBM", "memory chip", "storage chip",
        "DDR5", "LPDDR5", "3D NAND", "HBM3e", "HBM4",
        "Micron", "SK Hynix", "Samsung memory", "Kioxia",
        "solid state drive", "SSD", "CXL memory",
    ],
    "optical_communication": [
        "optical module", "optical transceiver", "400G", "800G",
        "1.6T", "1.6T optical", "PAM4", "DFB laser", "EML",
        "VCSEL", "silicon photonics", "SiPh", "LPO",
        "WDM", "DWDM", "optical amplifier", "EDFA",
        "Lumentum", "Coherent", "Ciena", "Marvell optics",
    ],
    "cpo": [
        "CPO", "Co-Packaged Optics", "co-packaged", "optical engine",
        "OE engine", "photonic integrated circuit", "PIC",
        "3.2T optical", "51.2T switch", "Tomahawk 5",
        "broadcom CPO", "Marvell CPO",
    ],
    "mlcc": [
        "MLCC", "multilayer ceramic capacitor", "ceramic capacitor",
        "capacitor", "passive component", "0402", "0603", "0201",
        "Murata", "Samsung Electro-Mechanics", "Taiyo Yuden",
        "AVX", "KEMET", "Vishay",
    ],
    "ccl": [
        "CCL", "copper clad laminate", "laminate", "FR-4",
        "high Tg", "low loss laminate", "PTFE laminate",
        "ABF film", "Rogers", "Isola", "Park Electrochemical",
        "prepreg", "dielectric substrate",
    ],
    "pcb": [
        "PCB", "printed circuit board", "HDI", "IC substrate",
        "ABF substrate", "multilayer board", "flex PCB", "FPC",
        "rigid-flex", "mSAP", "Sanmina", "TTM Technologies",
        "Benchmark Electronics", "Shennan Circuits",
    ],
    "materials": [
        "electronic materials", "electronic grade", "resin",
        "copper foil", "glass fiber", "glass cloth",
        "barium titanate", "sputtering target", "sputtering material",
        "specialty gas", "electronic gas", "DuPont", "Lindegas",
        "Air Products", "Mitsubishi Chemical", "Sumitomo",
    ],
    "liquid_cooling": [
        "liquid cooling", "liquid cooled", "cold plate",
        "CDU", "coolant distribution unit", "immersion cooling",
        "direct-to-chip", "D2C", "quick disconnect", "QD",
        "fluorinated fluid", "dielectric fluid", "microchannel",
        "Vertiv", "Asetek", "Cool IT",
    ],
    "ai_cloud": [
        "cloud", "SaaS", "IaaS", "PaaS", "AI platform",
        "large language model", "LLM", "GPU instance",
        "Microsoft Azure", "AWS", "Google Cloud", "GCP",
        "datacenter", "Kubernetes", "transformer",
    ],
    "consumer_electronics": [
        "smartphone", "iPhone", "wearable", "TWS",
        "smart speaker", "tablet", "PC", "laptop",
        "AR VR", "mixed reality", "Apple Watch", "AirPods",
        "OLED", "Micro-OLED",
    ],
}

# 各板块核心股票代码（用于确定板块归属优先级）
CORE_SECTOR_STOCKS: Dict[str, List[str]] = {
    "semiconductors": [
        "TSM", "ASML", "AMAT", "LRCX", "KLAC", "ACMR", "UCTT",
        "ARM", "SNPS", "CDNS", "KEYS", "TXN", "QRVO",
    ],
    "cpu": [
        "INTC", "AMD", "AAPL", "QCOM", "ARM", "MEDH",
    ],
    "gpu": [
        "NVDA", "AMD", "INTC", "SMCI",
    ],
    "tpu": [
        "AVGO", "MRVL", "GOOG", "META", "AMD",
    ],
    "memory": [
        "MU", "WDC", "STX", "NXPI", "GCT",
    ],
    "optical_communication": [
        "AVGO", "LITE", "CIEN", "MRVL", "AAOI", "NPTN", "IPG",
        "POET", "COHR", "AFBK", "LASR",
    ],
    "cpo": [
        "AVGO", "MRVL", "LITE", "AAOI", "CIEN", "POET",
    ],
    "mlcc": [
        "AVX", "KEM", "VSH",
    ],
    "ccl": [
        "ROG", "PEK", "MTLHY",
    ],
    "pcb": [
        "TEL", "FLEX", "SANM", "BHE", "TTMI",
    ],
    "materials": [
        "DD", "EMN", "FCX", "AA", "LIN", "APD", "ATI", "CE",
        "NEE", "WLK",
    ],
    "liquid_cooling": [
        "VRT", "SMCI", "MRCY", "MOD", "ALS",
    ],
    "ai_cloud": [
        "MSFT", "GOOG", "GOOGL", "AMZN", "CRM", "NOW", "PLTR",
        "ORCL", "IBM", "ADBE", "SNOW", "DDOG", "MDB", "NET",
        "DASH", "SHOP",
    ],
    "consumer_electronics": [
        "AAPL", "SONY", "SONOS", "HPQ", "DELL", "LOGI",
        "GPRO", "FIT", "UIS", "LPL", "SMSN",
    ],
}

# ticker 正则：2-5 位大写字母（避免单字母误伤）
_TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b")

# 常见英文词，即使匹配 ticker 正则也要跳过（这些通常不是股票）
_COMMON_WORDS = {
    "AI", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "HE", "IF",
    "IN", "IS", "IT", "ME", "MY", "NO", "OF", "ON", "OR", "SO",
    "TO", "UP", "US", "WE", "YEAR", "NEW", "OLD", "ONE", "TWO",
    "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "CAN",
    "HAD", "HER", "WAS", "ONE", "OUR", "OUT", "DAY", "GET", "HAS",
    "HIM", "HIS", "HOW", "ITS", "MAY", "OWN", "TOO", "WHO", "BUY",
    "HOT", "COLD", "BIG", "TOP", "LOW", "HIGH", "OPEN", "CLOSE",
    "OVER", "PLUS", "SEE", "SAY", "SET", "PUT", "RUN", "ADD", "END",
    "CEO", "CFO", "CTO", "COO", "USA", "UK", "EU", "PR", "HR",
    "GDP", "CPI", "IPO", "PE", "PB", "ROE", "ROA", "EPS", "EBIT",
    "ETF", "M&A", "AI", "ML", "NLP", "API", "SaaS", "IaaS",
    "PaaS", "IT", "IP", "TV", "PC", "VR", "AR", "GPS", "LED",
    "USD", "EUR", "JPY", "CNY", "HKD", "GBP", "AUD", "CAD",
    "NYSE", "NASDAQ", "AMEX", "OTC", "SEC", "FED", "FOMC",
    "Q1", "Q2", "Q3", "Q4", "H1", "H2", "YTD", "QOQ", "YOY",
    "P/E", "P/B", "PEG", "ROE",
}


def _is_common_word(ticker: str) -> bool:
    return ticker in _COMMON_WORDS


class StockDetectorUS:
    """美股股票检测器"""

    def __init__(self):
        self.stock_list_file = config.US_STOCK_LIST_JSON
        self.stock_list = self._load_stock_list()

    def _load_stock_list(self) -> Dict:
        if not self.stock_list_file.exists():
            print(f"⚠️ 美股列表不存在: {self.stock_list_file}")
            print("请运行: python -m us_chain_agent.scripts.refresh_us_stock_list")
            return {}
        with open(self.stock_list_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "stocks" in data and isinstance(data["stocks"], dict):
            return data["stocks"]
        return data

    def detect_stocks_from_text(self, text: str) -> List[Dict]:
        if not self.stock_list or not text or len(text) < 2:
            return []

        detected = []
        seen_codes = set()

        # 方法 1: ticker 正则匹配 + 反查 stock list
        for ticker in set(_TICKER_RE.findall(text)):
            if _is_common_word(ticker):
                continue
            if ticker not in self.stock_list:
                continue
            if ticker in seen_codes:
                continue
            seen_codes.add(ticker)
            info = self.stock_list[ticker]
            detected.append({
                "code": ticker,
                "name": info.get("name", "") if isinstance(info, dict) else str(info),
                "sector": self._determine_sector(ticker),
                "match_type": "ticker",
                "matched_keyword": ticker,
            })

        # 方法 2: 龙头公司名匹配（不区分大小写）
        # 美股公司名多为 "APPLE INC" / "MICROSOFT CORP"，文本中通常只写 "Apple" / "Microsoft"
        # 只对 CORE_SECTOR_STOCKS 中的龙头做名字匹配，避免 18000+ 全名单的误伤
        text_lower = text.lower()
        for sector, codes in CORE_SECTOR_STOCKS.items():
            for code in codes:
                if code in seen_codes:
                    continue
                info = self.stock_list.get(code)
                if not info:
                    continue
                name = info.get("name", "") if isinstance(info, dict) else str(info)
                if not name or len(name) < 4:
                    continue
                name_lower = name.lower()
                matched = None
                if name_lower in text_lower:
                    matched = name
                else:
                    first_word = name_lower.split()[0] if " " in name_lower else ""
                    if first_word and len(first_word) >= 4 and first_word.isalpha() and first_word in text_lower:
                        matched = first_word
                if matched:
                    seen_codes.add(code)
                    detected.append({
                        "code": code,
                        "name": name,
                        "sector": sector,
                        "match_type": "name",
                        "matched_keyword": matched,
                    })

        return detected

    def _determine_sector(self, code: str) -> Optional[str]:
        for sector, codes in CORE_SECTOR_STOCKS.items():
            if code in codes:
                return sector
        return None

    def get_sector_by_text(self, text: str) -> Optional[str]:
        text_lower = text.lower()
        for sector, keywords in _DEFAULT_SECTOR_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in text_lower:
                    return sector
        return None


if __name__ == "__main__":
    d = StockDetectorUS()
    sample = (
        "NVIDIA (NVDA) announced new GPUs. Apple (AAPL) unveiled iPhone 17. "
        "Microsoft Azure cloud revenue beat estimates."
    )
    print(f"sample: {sample}")
    print("detected:")
    for s in d.detect_stocks_from_text(sample):
        print(f"  {s}")
    print(f"sector by text: {d.get_sector_by_text(sample)}")
