"""
产业链投资挖掘 Agent - 全局配置

所有路径相对项目根目录解析，无任何外部依赖。
"""

import os
from pathlib import Path

# ===== 路径 =====
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # /opt/stocks
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 数据文件
ECOSYSTEM_JSON = DATA_DIR / "sector_ecosystem.json"
STOCK_LIST_JSON = DATA_DIR / "a_stock_list.json"
OVERFLOW_CONFIG_JSON = DATA_DIR / "sector_overflow_config.json"

# Tavily 结果落盘
TAVILY_OUTPUT_DIR = OUTPUT_DIR / "tavily"

# ===== 财联社新闻（直接复用 hermes 采集结果，不自己跑）=====
# hermes 的 latest_news.json 由它自己的 cron 实时更新，本项目只读不写
# 通过环境变量可覆盖路径；hermes 不在则降级为 akshare-only
HERMES_NEWS_JSON = Path(os.environ.get(
    "HERMES_NEWS_JSON",
    "/root/.hermes/data/investment-research/news/latest_news.json",
))


# ===== 板块命名规约 =====
# sector_ecosystem.json 用下划线 (optical_module)
# sector_overflow_config.json 用连字符 (optical-module)
# 内部统一用下划线为 canonical
def to_hyphen(sector: str) -> str:
    return sector.replace("_", "-")


def to_under(sector: str) -> str:
    return sector.replace("-", "_")


# ===== Tavily 配置 =====
# 多 Key 轮询。默认沿用 ~/.hermes/scripts/investment-research 的 3 个 Key，
# 环境变量 TAVILY_API_KEYS（逗号分隔）可覆盖。
_DEFAULT_TAVILY_API_KEYS = [
    "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",  # 主 Key #1
    "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",  # 备用 Key #2
    "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",  # 备用 Key #3
]
_env_keys = [
    k.strip() for k in os.environ.get("TAVILY_API_KEYS", "").split(",") if k.strip()
]
TAVILY_API_KEYS = _env_keys or _DEFAULT_TAVILY_API_KEYS


# ===== 智谱 BigModel web_search_pro 配置（Tavily 兜底）=====
# 优先 Tavily，失败自动切智谱。智谱按搜索次数付费。
# search_std：标准版；search_pro：增强版（更全、更准、更贵）
# key 来自 ~/.claude/settings.json 的 ANTHROPIC_AUTH_TOKEN（智谱 anthropic endpoint 和
# 原生 API 共用同一个 key，格式 id.secret）
_DEFAULT_ZHIPU_API_KEY = "a70c6eb7fa354119b4f39db731deb6ae.KNsywSEW8wi5EscB"
ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", _DEFAULT_ZHIPU_API_KEY)
ZHIPU_SEARCH_ENGINE = os.environ.get("ZHIPU_SEARCH_ENGINE", "search_pro_sogou")


# ===== 行情源配置 =====
# easyquotation (默认，腾讯源，稳定) | akshare (备选，东财源，偶发 RemoteDisconnected)
# 切换：export QUOTE_PROVIDER=akshare
QUOTE_PROVIDER = os.environ.get("QUOTE_PROVIDER", "easyquotation").lower()


# ===== 东财直连限流（a-stock-data skill 集成，collectors/stock_data.py 用）=====
# 两次东财请求最小间隔(秒)；批量 enrich 候选股时建议 1.0~2.0
EM_MIN_INTERVAL = float(os.environ.get("EM_MIN_INTERVAL", "1.0"))
EM_TIMEOUT = int(os.environ.get("EM_TIMEOUT", "15"))

# iwencai 语义搜索（预留，本轮不启用；需在 ~/.claude/settings.json 配置）
IWENCAI_API_KEY = os.environ.get("IWENCAI_API_KEY", "")


# ===== LLM 配置 =====
# provider: auto | anthropic | openai | kimi | none
# 默认 anthropic -> Volcengine ark（Anthropic 兼容 endpoint，统一接入多模型）。
# auto 优先 Anthropic（Volcengine），其次 OpenAI 兼容；kimi 显式走 Moonshot。
# Claude Code 宿主环境通过 settings.json 注入 ANTHROPIC_BASE_URL/ANTHROPIC_API_KEY，
# 项目默认指向 Volcengine，key 必须来自环境变量（settings.json 或 .env）。
LLM_PROVIDER = os.environ.get("CHAIN_AGENT_LLM_PROVIDER", "anthropic")
LLM_MAX_TOKENS = int(os.environ.get("CHAIN_AGENT_LLM_MAX_TOKENS", "16384"))
# 单次 LLM 调用超时（秒）：挂起的连接快失败，让上层降级/重试生效。
# 注意：kimi 在 harness 多路径并发抢配额下，单次长输出（decompose/bottleneck/评分）
# 实测可达 300-500s，故默认 600s（= SDK 默认）避免误杀合法慢调用；真正卡死的连接
# 由各 skill 的路径墙钟上限（harness per-path timeout）兜底。
LLM_REQUEST_TIMEOUT = float(os.environ.get("LLM_REQUEST_TIMEOUT", "600"))

# 评判用低温度（默认 0.2）：评判是确定性任务，低温度降方差（同报告多次判分波动从 ±15 收窄）。
# 报告生成不用此值（保留默认温度，允许 LLM 有分析发散）；仅 judge 传 temperature。
JUDGE_TEMPERATURE = float(os.environ.get("JUDGE_TEMPERATURE", "0.2"))

# kimi-k2.6 默认开启思考（reasoning_content），评分/JSON 场景下思考 token 吃光 max_tokens
# 致答案截断 + 重试爆炸 + 超时。默认对 kimi 关闭思考（答案直出 content，快且完整）。
# 设 KIMI_THINKING_ENABLED=1 可重新开启（如需深度推理场景）。
KIMI_THINKING_ENABLED = os.environ.get("KIMI_THINKING_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")

# Anthropic（指向 Volcengine ark Anthropic 兼容 endpoint，统一接入多模型；
# 不设默认 key，必须来自 env。Claude Code 宿主环境通过 settings.json 注入
# ANTHROPIC_AUTH_TOKEN（Volcengine 特定变量），也作为 ANTHROPIC_API_KEY 的 fallback。
ANTHROPIC_API_KEY = (
    os.environ.get("ANTHROPIC_API_KEY", "")
    or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
)
ANTHROPIC_BASE_URL = os.environ.get(
    "CHAIN_AGENT_ANTHROPIC_BASE_URL",
    "https://ark.cn-beijing.volces.com/api/plan",
)
ANTHROPIC_MODEL = os.environ.get("CHAIN_AGENT_ANTHROPIC_MODEL", "deepseek-v4-flash")

# OpenAI 兼容（备选 provider；默认 provider 已切至 anthropic → Volcengine）
# 切回 DeepSeek：设 CHAIN_AGENT_LLM_PROVIDER=openai（使用以下配置）
# 切回 Kimi：设 CHAIN_AGENT_LLM_PROVIDER=kimi + OPENAI_BASE_URL=https://api.moonshot.cn/v1 + KIMI_API_KEY
OPENAI_API_KEY = (
    os.environ.get("OPENAI_API_KEY", "")
    or os.environ.get("DEEPSEEK_API_KEY", "")
    or os.environ.get("KIMI_API_KEY", "")
    or ""  # 无默认 key；如需自包含部署，可在此填入 DeepSeek key
)
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com")
OPENAI_MODEL = os.environ.get("CHAIN_AGENT_OPENAI_MODEL", "deepseek-v4-flash")


# ===== 评分阈值（借鉴 sector-overflow-effect）=====
SCORING_WEIGHTS = {
    "leader_saturation": 0.30,
    "valuation_discount": 0.25,
    "news_momentum": 0.20,
    "tech_option_value": 0.15,
    "supply_chain_position": 0.10,
}


def clear_proxy_env():
    """清除代理环境变量（akshare/requests 直连需要）"""
    for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "all_proxy", "ALL_PROXY"]:
        os.environ.pop(k, None)
    os.environ.setdefault("no_proxy", "localhost,127.0.0.1")


# 启动时自动清代理
clear_proxy_env()

# 把 anthropic base_url 同步到环境变量，让 anthropic SDK 自动读取
# （SDK 只认 os.environ 或构造参数，不认模块变量）
# 注意：不同于旧版，此处不再 force-override 宿主环境注入的值。
# Claude Code 的 settings.json 已正确指向 Volcengine，自然生效。
if ANTHROPIC_BASE_URL:
    os.environ["ANTHROPIC_BASE_URL"] = ANTHROPIC_BASE_URL


# ===== 免责声明 =====
DISCLAIMER_TEXT = "本报告由公开资料整理，仅供参考，不构成任何投资建议。市场有风险，投资需谨慎。"
