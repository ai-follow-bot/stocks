# 产业链投资挖掘 Agent

一个完全自包含的产业链投资分析 CLI 工具：输入一个板块代码，输出可投资标的清单 + 推荐理由 + LLM 综合报告。

无任何外部目录依赖，复制到任何 Linux + Python 3.11+ 机器上 `pip install -r requirements.txt` 即可运行。

## 架构

5 层 pipeline：

```
输入: "optical_module"
   │
   ▼
[Layer 1] chain_graph.expand_chain()        产业链图展开
   │   加载 data/sector_ecosystem.json (15 板块, 4 tier)
   │   输出: {focus, upstream, downstream, nodes, stocks_by_sector}
   ▼
[Layer 2] collectors.collect_all()          双轨并行数据采集
   │   供给侧: Tavily AI 深度搜索 (多 Key 轮询 + AI 摘要)
   │   需求侧: akshare 个股新闻 + 宏观新闻关键词过滤
   │   设计: 双轨独立失败，统一为 content_text
   ▼
[Layer 3] discovery.discover_candidates()   动态公司发现
   │   NLP 标的发现: 5506 只 A 股全名单 + 名称匹配 + 板块关键词
   │   6 位代码误报过滤 (排除日期/订单号)
   │   合并: 文本发现 ∪ 池中已有 = 候选池
   ▼
[Layer 4] scoring.score_candidates()        可投资性评分
   │   - overflow: 龙头饱和度 + 二线折价/弹性 (PE/市值)
   │   - tech_option: 技术期权期望值 = p × (success_pe-base_pe)/base_pe
   │   - heuristic: 兜底评分 (news_hits + in_pool + tier + 代码可投性)
   │   输出: 0-100 分 + 角色 (leader/second_tier/tech_option/discovery)
   ▼
[Layer 5] llm.synthesize()                  LLM 综合报告 (可选)
   │   支持 Anthropic Claude / OpenAI 兼容 (Kimi/Moonshot/Deepseek/GLM)
   │   降级: 模板报告 (无 LLM 也能跑)
   ▼
输出: Markdown 投资分析报告 / JSON 结构化数据
```

## 快速开始

> **运行环境提示**：必须用项目自带 venv 的 Python，**不要**直接用系统 `python`。
> 系统 Python 装的 `tavily-python` 版本过旧（0.1.9），连 `TavilyClient` 都没导出，会直接报 `ImportError`。
> 项目 venv 里是 `tavily-python 0.7.26`，正常工作。
>
> ```bash
> # 所有命令请用这个 Python：
> /opt/stocks/.venv/bin/python -m chain_agent.agent <sector>
> # 或者先激活 venv 再跑：
> source /opt/stocks/.venv/bin/activate
> python -m chain_agent.agent <sector>
> ```
>
> 网站 `/home/smallsite-vue` 后端通过 API 触发任务时已经写死用这个 venv Python
> （见 `backend/src/api/admin-stocks.ts` 的 `STOCKS_VENV_PYTHON`），无需额外配置。

```bash
# 安装依赖（仅在 venv 内）
pip install -r requirements.txt

# 纯 Python 报告（无需 LLM）
/opt/stocks/.venv/bin/python -m chain_agent.agent optical_module

# 自定义时间窗口 + Top N
/opt/stocks/.venv/bin/python -m chain_agent.agent pcb --days 14 --top-n 20

# 启用 LLM 综合分析
export ANTHROPIC_API_KEY=sk-ant-...
/opt/stocks/.venv/bin/python -m chain_agent.agent storage --llm

# 多板块批量并行
/opt/stocks/.venv/bin/python -m chain_agent.agent --sectors optical_module,pcb,storage --max-workers 3 --llm

# 输出 JSON
/opt/stocks/.venv/bin/python -m chain_agent.agent ai_server --json --out ai_server.json

# 保存报告到文件
/opt/stocks/.venv/bin/python -m chain_agent.agent liquid_cooling --out report.md
```

### Tavily API Key

供给侧搜索默认使用内置的 3 个 Tavily Key（轮询 + 故障切换，配额耗尽自动 failover），
无需额外配置即可工作。如需替换为自己的 Key，设置环境变量覆盖：

```bash
export TAVILY_API_KEYS="tvly-xxx,tvly-yyy,tvly-zzz"  # 逗号分隔多个 Key
```

## 目录结构

```
/opt/stocks/
├── README.md
├── requirements.txt
├── .venv/                          # 项目自带 venv (Python 3.11)
├── data/                           # 内置静态数据
│   ├── sector_ecosystem.json       # 产业链图 (15 板块, 4 tier)
│   ├── a_stock_list.json           # A 股全名单 (5506 只)
│   └── sector_overflow_config.json # 龙头/二线/技术期权股配置
├── chain_agent/                    # 主包
│   ├── __init__.py
│   ├── config.py                   # 全局配置 + 板块命名规约
│   ├── chain_graph.py              # L1: 产业链展开
│   ├── collectors/
│   │   ├── tavily_search.py        # L2: Tavily 多 Key 轮询
│   │   ├── news_akshare.py         # L2: akshare 新闻
│   │   └── orchestrator.py         # L2: 双轨并行采集
│   ├── discovery/
│   │   ├── stock_detector.py       # L3: NLP 标的发现
│   │   └── candidates.py           # L3: 候选池合并
│   ├── scoring/
│   │   ├── quotes.py               # L4: 行情源抽象 (akshare/easyquotation)
│   │   ├── overflow.py             # L4: 龙头饱和度 + 二线弹性
│   │   ├── tech_option.py          # L4: 技术期权期望值
│   │   ├── heuristic.py            # L4: 兜底评分
│   │   └── integrator.py           # L4: 信号整合
│   ├── llm/
│   │   ├── client.py               # L5: Anthropic + OpenAI 兼容
│   │   └── prompts.py              # L5: 提示词模板
│   └── agent.py                    # 主入口 + CLI
├── scripts/
│   └── refresh_stock_list.py       # akshare 刷新 A 股名单
└── output/                         # 报告输出目录
```

## 环境变量配置

| 变量 | 默认 | 说明 |
|------|------|------|
| `TAVILY_API_KEYS` | 内置 3 个 dev Key（轮询） | Tavily API Key 列表，逗号分隔，多 Key 自动轮询。设置后覆盖内置默认 |
| `ANTHROPIC_API_KEY` | - | Claude API Key |
| `OPENAI_API_KEY` / `KIMI_API_KEY` | - | OpenAI 兼容 API Key (Kimi/Deepseek/GLM) |
| `OPENAI_BASE_URL` | Kimi 默认 | OpenAI 兼容端点 |
| `OPENAI_MODEL` | kimi-k2 | OpenAI 兼容模型名 |
| `ANTHROPIC_MODEL` | claude-sonnet-4-5-20250929 | Claude 模型名 |
| `LLM_PROVIDER` | auto | auto / anthropic / kimi / none |
| `LLM_MAX_TOKENS` | 8192 | LLM 输出最大 token 数 |
| `QUOTE_PROVIDER` | akshare | akshare / easyquotation |

## 数据刷新

```bash
# 用 akshare 重新拉取 A 股全名单 (沪深北)
python scripts/refresh_stock_list.py
```

## 评分逻辑

每个候选标的最终 0-100 分，由四部分加权：

| 信号 | 来源 | 加分 |
|------|------|------|
| 龙头饱和度 | overflow (PE + 市值) | 角色判定 |
| 二线折价 + 弹性 | overflow | 折价 × 30 + 弹性 × 5 |
| 技术期权期望值 | tech_option | ev × 30 |
| 新闻命中 | collectors → discovery | min(40, hits × 8) |
| 池中已有 | overflow_config | +15 |
| A 股可投 | 6 位数字代码 | +20 |
| Tier 核心环节 | ecosystem.json | +15 (T1/T2) / +5 (T3/T4) |
| 新闻+池双确认 | discovery | +10 |

角色分类：
- `leader` — 龙头（饱和度高时不一定是好买点）
- `second_tier` — 二线（折价 + 弹性，溢出受益）
- `tech_option` — 技术期权（成功概率 × 估值跃升空间）
- `discovery` — 新发现（待 LLM 甄别是否误报）

## 板块命名规约

- `sector_ecosystem.json` 用下划线 (`optical_module`)
- `sector_overflow_config.json` 用连字符 (`optical-module`)
- `config.to_hyphen()` / `config.to_under()` 负责双向转换

## 设计原则

- **数据驱动**：板块图、龙头清单、关键词全部走 JSON 配置文件，不在代码硬编码
- **依赖注入**：scoring 层接收 `quote_provider` 参数，不全局 singleton
- **抽象 + 默认实现**：`QuoteProvider` 抽象基类 + `AkshareQuoteProvider` 默认实现
- **环境变量优先**：API key、模型名、provider 选择全部支持环境变量覆盖
- **降级友好**：Tavily/LLM/easyquotation 任一不可用时自动降级，仍能产出报告
