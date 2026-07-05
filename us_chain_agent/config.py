"""
美股产业链 Agent - 全局配置

路径与 chain_agent.config 对齐，复用 PROJECT_ROOT/DATA_DIR/OUTPUT_DIR。
所有 LLM/Tavily/Zhipu 配置直接 import chain_agent.config 复用。

注意：chain_agent.config 在 import 时会调 clear_proxy_env()（A 股 akshare 需直连）。
美股数据源（Wikipedia/Tavily 通过代理）需要走代理，这里在 import 前先捕获代理。
"""

import os
from pathlib import Path

# 在 import chain_agent.config 之前先捕获代理环境变量
# （clear_proxy_env 会清掉所有 proxy env，A 股 akshare 需直连，美股需走代理）
_PROXY_HTTP = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY", "")
_PROXY_HTTPS = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY", "")
_PROXY_ALL = os.environ.get("all_proxy") or os.environ.get("ALL_PROXY", "")

# 复用 A 股 config 的项目根路径和通用配置
from chain_agent import config as cn  # noqa: F401,E402

PROJECT_ROOT = cn.PROJECT_ROOT  # /opt/stocks
DATA_DIR = cn.DATA_DIR
OUTPUT_DIR = cn.OUTPUT_DIR

# 美股数据文件
US_STOCK_LIST_JSON = DATA_DIR / "us_stock_list.json"
US_ECOSYSTEM_JSON = DATA_DIR / "us_sector_ecosystem.json"
US_OVERFLOW_CONFIG_JSON = DATA_DIR / "us_sector_overflow_config.json"

# Tavily 落盘目录（与 A 股共用）
TAVILY_OUTPUT_DIR = cn.TAVILY_OUTPUT_DIR

# 直接复用 A 股 config 的：TAVILY_API_KEYS / ZHIPU_API_KEY / LLM_PROVIDER /
# ANTHROPIC_* / OPENAI_* / SCORING_WEIGHTS / to_hyphen / to_under


# ===== Finnhub 配置（美股行情/新闻/公司资料）=====
# 免费版 60 次/分钟，单次产业链跑 7 龙头 × 3 端点 = 21 次，够用
FINNHUB_API_KEY = os.environ.get(
    "FINNHUB_API_KEY", "d8sg55pr01qq7apvfqp0d8sg55pr01qq7apvfqpg"
)
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"


# ===== 恢复代理环境变量 =====
# chain_agent.config.clear_proxy_env() 清掉后，这里恢复（美股数据源需走代理）
if _PROXY_HTTP:
    os.environ["http_proxy"] = _PROXY_HTTP
    os.environ["HTTP_PROXY"] = _PROXY_HTTP
if _PROXY_HTTPS:
    os.environ["https_proxy"] = _PROXY_HTTPS
    os.environ["HTTPS_PROXY"] = _PROXY_HTTPS
if _PROXY_ALL:
    os.environ["all_proxy"] = _PROXY_ALL
    os.environ["ALL_PROXY"] = _PROXY_ALL

# 清掉 no_proxy（chain_agent.config 设了 localhost,127.0.0.1；美股请求都是外网，不影响）
os.environ.pop("no_proxy", None)
os.environ.pop("NO_PROXY", None)


# ===== 免责声明 =====
DISCLAIMER_TEXT = cn.DISCLAIMER_TEXT
