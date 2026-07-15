"""
配置模块：权重初始值、收敛参数、路径等
"""
import os
from pathlib import Path

# ── 路径 ──────────────────────────────────────────────
SKILL_DIR = Path(__file__).parent
OUTPUT_DIR = SKILL_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

STOCKS_DIR = SKILL_DIR.parent.parent  # /opt/stocks
DATA_DIR = STOCKS_DIR / "data"

# ── 初始权重 [事件密度, 情绪强度, 产业链共振, 事件多样性, 重要性加权] ──
INITIAL_WEIGHTS = [0.29, 0.29, 0.24, 0.18, 0.00]

# ── 收敛参数 ──
CONVERGENCE_DAYS = 14          # 连续多少天判定收敛
CONVERGENCE_DELTA = 0.01       # 权重变化阈值
CONVERGENCE_MIN_ACCURACY = 0.55  # 最低准确率要求
REGIME_CHANGE_DAYS = 7         # 市场结构变化检测窗口
REGIME_CHANGE_THRESHOLD = 0.50  # 准确率低于此值触发重置

# ── 共振计算 ──
DENSITY_CAP = 3.0              # 事件密度上限（30日均值倍数）
DIVERSITY_MAX = 5              # 事件类型多样性封顶值
IMPORTANCE_CAP = 500           # 重要性加权封顶值

# ── 数据源路径 ──
HERMES_NEWS_DIR = Path("/root/.hermes/data/investment-research/news")
LATEST_NEWS_PATH = HERMES_NEWS_DIR / "latest_news.json"

# ── LLM配置 ──
# 使用 get_llm_client() 继承 chain_agent.config 的 provider/base_url/model。
# 以下为兜底值，仅在 get_llm_client() 不可用时生效。
LLM_PROVIDER = os.environ.get("DAILY_RESONANCE_LLM_PROVIDER", "anthropic")
LLM_MODEL = os.environ.get("DAILY_RESONANCE_LLM_MODEL", "deepseek-v4-flash")
LLM_MAX_TOKENS = int(os.environ.get("DAILY_RESONANCE_LLM_MAX_TOKENS", "4096"))
LLM_TEMPERATURE = 0.3
