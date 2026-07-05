"""
Layer 3: 股票检测器（重写）

从新闻/研报文本中动态发现 A 股标的。算法借鉴 ~/.hermes/scripts/investment-research/stock_detector_v2.py
（独立重写，并加了 6 位数字误报过滤）。
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from .. import config

# 板块关键词默认值（与 sector_ecosystem.json 对齐）
# 运行时优先从 data/sector_keywords.json 加载（可由后台动态修改），缺失则回退到此默认
_DEFAULT_SECTOR_KEYWORDS = {
    "optical_module": ["光模块", "光通信", "光纤", "800G", "1.6T", "硅光", "LPO", "CPO",
                       "光器件", "光引擎", "光芯片", "英伟达", "NVIDIA"],
    "pcb": ["PCB", "印刷电路板", "覆铜板", "AI服务器", "服务器PCB", "高层板", "HDI", "IC载板"],
    "storage": ["HBM", "存储", "DRAM", "SSD", "内存", "NAND", "闪存", "存储芯片"],
    "optical_chip": ["光芯片", "激光芯片", "光交换", "激光器", "EML", "VCSEL"],
    "ai_server": ["AI服务器", "服务器", "算力服务器", "智算中心", "GPU服务器"],
    "liquid_cooling": ["液冷", "散热", "温控", "冷却", "热管理", "液冷服务器", "CDU", "冷板"],
    "hbm_components": ["HBM", "TSV", "DRAM颗粒"],
    "cooling_components": ["冷却液", "快接头", "CDU", "冷板", "浸没液"],
    "ccl": ["覆铜板", "CCL", "高速材料", "高Tg"],
    "copper_foil": ["铜箔", "电子铜箔", "压延铜箔"],
    "chiplet": ["Chiplet", "先进封装", "CoWoS", "EMIB", "2.5D", "3D封装", "UCIe"],
    "switch": ["交换机", "白盒", "IB", "以太网", "Arista"],
    "datacenter": ["数据中心", "AIDC", "智算中心", "PUE"],
    "power_supply": ["电源", "PSU", "HVDC", "UPS", "BBU"],
    "ocs": ["OCS", "光交换", "MEMS", "光路交换", "光纤阵列"],
    "mlcc": ["MLCC", "多层陶瓷电容", "陶瓷电容", "电容器", "BME", "Ni电极",
             "陶瓷粉体", "车规电容", "高容值"],
}

# JSON 配置路径（与 sector_overflow_config.json 同目录）
KEYWORDS_JSON = config.DATA_DIR / "sector_keywords.json"


def _load_sector_keywords() -> Dict[str, List[str]]:
    """从 data/sector_keywords.json 加载，缺失/出错回退到默认值"""
    try:
        if KEYWORDS_JSON.exists():
            data = json.loads(KEYWORDS_JSON.read_text(encoding="utf-8"))
            sectors = data.get("sectors", {})
            # 合并：JSON 中有就用 JSON 的，缺失板块用默认
            merged = dict(_DEFAULT_SECTOR_KEYWORDS)
            merged.update(sectors)
            return merged
    except Exception as e:
        print(f"[stock_detector] 加载 sector_keywords.json 失败，用默认: {e}")
    return dict(_DEFAULT_SECTOR_KEYWORDS)


# 模块加载时初始化（运行时通过 reload_keywords() 刷新缓存）
SECTOR_KEYWORDS = _load_sector_keywords()


def reload_keywords() -> Dict[str, List[str]]:
    """重新加载 JSON 关键词（后台修改后调用，刷新模块级 SECTOR_KEYWORDS）"""
    global SECTOR_KEYWORDS
    SECTOR_KEYWORDS = _load_sector_keywords()
    return SECTOR_KEYWORDS

# 各板块核心股票代码（用于确定板块归属优先级）
CORE_SECTOR_STOCKS = {
    "optical_module": ["300308", "300502", "300394", "688498", "300570", "300548"],
    "pcb": ["002463", "300476", "002916", "002636", "603228", "600183"],
    "storage": ["301308", "688525", "300223", "603986"],
    "optical_chip": ["688048", "688205", "688498"],
    "ai_server": ["000938", "603019", "000977", "601138"],
    "liquid_cooling": ["300499", "002837", "301018", "300990", "603912"],
    "mlcc": ["300408", "000636", "603678", "603267", "002859", "300285", "301511"],
}


# 6 位数字代码合法范围校验（A 股规则）
# 沪市主板: 600/601/603/605/688(科创板) 开头
# 深市主板: 000/001/002/003 开头
# 深市创业板: 300/301 开头
# 北交所: 8 开头（43/83/87/920 等）
A股_PREFIX = {
    "600", "601", "603", "605", "688", "689",  # 沪市
    "000", "001", "002", "003", "300", "301",  # 深市
    "430", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839",
    "870", "871", "872", "873", "920",  # 北交所
}


def _is_valid_a_share_code(code: str) -> bool:
    """校验 6 位代码是否符合 A 股规则（剔除随机 6 位数字误报）"""
    if not (len(code) == 6 and code.isdigit()):
        return False
    # 排除日期格式 YYYYMM, YYYYMMDD 片段（如 202506, 20261225）
    if code.startswith("20") and (code[2:4] in ("19", "20", "21", "22", "23", "24", "25", "26")):
        # 像 2025XX, 2026XX 这种大概率是年份
        if 4 <= int(code[2:4]) <= 12 or code[4:6] == "00":
            return False
    return code[:3] in A股_PREFIX


class StockDetector:
    """股票检测器 - 基于 A 股全名单 + 名称匹配"""

    def __init__(self):
        self.stock_list_file = config.STOCK_LIST_JSON
        self.stock_list = self._load_stock_list()

    def _load_stock_list(self) -> Dict:
        if not self.stock_list_file.exists():
            print(f"⚠️ 股票列表不存在: {self.stock_list_file}")
            print("请运行: python scripts/refresh_stock_list.py")
            return {}
        with open(self.stock_list_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 兼容两种结构：{stocks: {code: info}} 或 {code: info}
        if "stocks" in data and isinstance(data["stocks"], dict):
            return data["stocks"]
        return data

    def detect_stocks_from_text(self, text: str) -> List[Dict]:
        """从文本中检测股票，返回 [{code, name, sector, match_type, matched_keyword}]"""
        if not self.stock_list or not text or len(text) < 2:
            return []

        detected = []
        text_upper = text.upper()
        seen_codes = set()

        # 方法 1: 6 位数字代码匹配（带合法性校验）
        for code in set(re.findall(r"\b(\d{6})\b", text)):
            if not _is_valid_a_share_code(code):
                continue
            if code not in self.stock_list:
                continue
            if code in seen_codes:
                continue
            seen_codes.add(code)
            info = self.stock_list[code]
            detected.append({
                "code": code,
                "name": info.get("name", "") if isinstance(info, dict) else str(info),
                "sector": self._determine_sector(code),
                "match_type": "code",
                "matched_keyword": code,
            })

        # 方法 2: 股票名称匹配
        for code, info in self.stock_list.items():
            if code in seen_codes:
                continue
            name = info.get("name", "") if isinstance(info, dict) else str(info)
            if not name or len(name) < 2:
                continue
            if name in text or name in text_upper:
                # 过滤过于通用的名称（如 "中兴"，"长城" 等单字或双字通用词）
                if len(name) <= 2:
                    continue
                seen_codes.add(code)
                detected.append({
                    "code": code,
                    "name": name,
                    "sector": self._determine_sector(code),
                    "match_type": "name",
                    "matched_keyword": name,
                })

        return detected

    def _determine_sector(self, code: str) -> Optional[str]:
        """根据代码确定板块归属"""
        for sector, codes in CORE_SECTOR_STOCKS.items():
            if code in codes:
                return sector
        return None

    def get_sector_by_text(self, text: str) -> Optional[str]:
        """根据文本关键词判断板块"""
        for sector, keywords in SECTOR_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                return sector
        return None


if __name__ == "__main__":
    d = StockDetector()
    sample = "中际旭创(300308)获英伟达1.6T光模块大单，新易盛300502 800G出货超预期"
    for s in d.detect_stocks_from_text(sample):
        print(s)
