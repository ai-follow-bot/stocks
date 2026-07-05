---
name: deep-analyze
title: 产业链深度拆解 + 投资价值判断
description: 双模式 skill。输入产业链（如 mlcc）→ 拆解各个环节、找卡脖子环节、按供需/国产替代/业绩兑现三维评分筛出值得投资的公司清单；输入股票（如 300308）→ 定位其产业链环节、判断是否值得投资 + 同环节对比。复用 chain_agent 的 Tavily + akshare 双轨采集 + LLM 综合。
version: 1.0.0
tags: [investment, supply-chain, bottleneck, domestic-substitution, llm]
---

# 产业链深度拆解 + 投资价值判断

## 适用场景

- 用户给一个**产业链**（如 "MLCC"、"光模块"、"HBM"），想完整拆解各个环节，找出卡脖子环节，并按供需/国产替代/业绩兑现快慢三维评分挑出可投资标的
- 用户给一个**股票**（如 "300308"、"中际旭创"），想判断这家公司是否值得投资，需要把它放回所属产业链中看卡位、对比同环节对手

## 双模式

### Chain 模式（多公司挖掘）
```bash
python -m skills.deep-analyze --chain mlcc
python -m skills.deep-analyze --chain 光模块 --days 14 --top-n 8 --out report.md
```
输出：
1. 产业链结构图（环节 + 上下游关系）
2. 卡脖子环节标注（按集中度/国产化率/技术门槛/涨价信号打分）
3. Top N 可投资标的（每只给三维度分数 + 推荐理由 + 风险）

### Stock 模式（单股判断）
```bash
python -m skills.deep-analyze --stock 300308
python -m skills.deep-analyze --stock 中际旭创 --out verdict.md
```
输出：
1. 公司定位（主营 → 产业链 → 具体环节）
2. 该环节是否卡脖子 + 公司在环节中的卡位
3. 三维度评分（供需/国产替代/业绩兑现）
4. 同环节竞争对手对比
5. 最终判断：值得投资 / 谨慎 / 回避 + 理由

## 核心方法论

### 1. 产业链拆解

输入链名后：
- 先查 `data/sector_ecosystem.json` 拿到已知结构（4 tier + 上中下游 + 关键产品/技术）
- 再让 LLM 补充细分：把链拆成 5-8 个**具体环节**（如 MLCC → 陶瓷粉体 / 电极材料 / 离型膜 / MLCC 制造 / 包装测试 / 终端应用），每环节标注：
  - role: upstream / midstream / downstream
  - key_tech: 关键技术节点
  - global_leaders: 全球龙头（村田/Samsung/太阳诱电）
  - cn_leaders: 国内龙头
  - concentration: 全球 CR3 份额（0-1）
  - cn_share: 国产化率（0-1）

### 2. 卡脖子识别

每环节按四维打分（每维 0-5，总分 0-20，≥14 标为卡脖子）：

| 维度 | 数据来源 | 高分含义 |
|------|---------|---------|
| 供应集中度 | Tavily 搜 "{segment} market share CR3" | CR3 > 80% → 5 分 |
| 国产化率 | Tavily 搜 "{segment} 国产化率 国产替代" | CN share < 20% → 5 分 |
| 技术门槛 | LLM 知识 + 专利/工艺描述 | BME/共烧/薄层化 → 高分 |
| 涨价/缺货信号 | akshare 新闻 + Tavily 近 30 天 | 涨价/缺货 → 5 分 |

### 3. 三维投资评分（每只候选标的）

| 维度 | 满分 | 评分细则 |
|------|------|---------|
| **供需关系** | 30 | 所处环节供应紧张度 (15) + 公司市占率/产能弹性 (15) |
| **国产替代** | 30 | 环节国产化率提升空间 (15) + 公司在国产替代中的卡位 (15) |
| **业绩兑现快慢** | 40 | 订单可见度 (10) + 产能投放节奏 (15) + 当前 PE vs 远期 PE (15) |

总分 100。≥75 高仓位 / 55-75 中仓位 / <55 谨慎。

### 4. 多环节多查询搜索策略

每个环节并发发 3 条 Tavily 查询：
- `{segment} 供需 价格 涨价 产能 2026`
- `{segment} 国产替代 国产化率 突破 中国`
- `{segment} 龙头 业绩 订单 出货量 市占率`

加上 akshare 个股新闻（关键公司代码）做近 N 天舆情过滤。
LLM 综合时优先采信有数据支撑的结论，明确标"数据缺失"。

## 复用的 chain_agent 资产

| 模块 | 用途 |
|------|------|
| `chain_agent.collectors.tavily_search.TavilySearch` | 多 Key 轮询 Tavily 搜索 |
| `chain_agent.collectors.news_akshare.collect_demand_side` | 龙头股 akshare 新闻 |
| `chain_agent.discovery.stock_detector.StockDetector` | 从搜索文本中提取 A 股代码 |
| `chain_agent.llm.client.get_llm_client` | Claude / Kimi LLM 客户端 |
| `chain_agent.config` | 路径 + API key 配置 |
| `data/sector_ecosystem.json` | 已知产业链图谱（15 板块 + MLCC） |

## 文件结构

```
/opt/stocks/skills/deep-analyze/
├── SKILL.md                       # 本文档
├── __init__.py
├── __main__.py                    # CLI 入口
├── analyzer.py                    # 主 pipeline：decompose → search → bottleneck → score
├── prompts.py                     # LLM 提示词模板（链拆解/卡脖子/三维评分/单股判断）
└── report.py                      # Markdown 报告渲染
```

## CLI 用法

```bash
# 产业链多公司挖掘
python -m skills.deep-analyze --chain mlcc
python -m skills.deep-analyze --chain 光模块 --days 14 --top-n 8 --out mlcc.md

# 单股判断
python -m skills.deep-analyze --stock 300308
python -m skills.deep-analyze --stock 中际旭创 --out verdict.md

# 公共参数
#   --days N        回看新闻窗口（默认 14）
#   --top-n N       chain 模式输出 Top N（默认 8）
#   --out PATH      输出文件
#   --json          输出结构化 JSON（chain 模式可用）
```

## 输出示例（chain 模式）

```markdown
# MLCC 产业链深度拆解

## 1. 产业链结构（6 环节）

| 环节 | 上下游 | 全球龙头 | 国产化率 | 卡脖子分 |
|------|--------|---------|---------|---------|
| 陶瓷粉体（钛酸钡） | 上游 | 日本堺化学/村田 | 25% | 16 ⚠️ |
| 镍内电极 | 上游 | 日本昭和电工 | 30% | 14 ⚠️ |
| 离型膜 | 上游 | 日本东丽/三井 | 35% | 12 |
| MLCC 制造 | 中游 | 村田/Samsung/太阳诱电 | 30% | 17 ⚠️ |
| 包装测试 | 下游 | — | 80% | 4 |
| 终端应用 | 下游 | — | — | — |

## 2. 卡脖子环节：MLCC 制造 + 陶瓷粉体

[LLM 综合分析为什么卡脖子 + 国产替代突破口]

## 3. Top 5 可投资标的

| 排名 | 代码 | 名称 | 环节 | 供需 | 国替 | 业绩 | 总分 | 推荐权重 |
|------|------|------|------|------|------|------|------|---------|
| 1 | 300408 | 三环集团 | MLCC 制造 | 25 | 22 | 32 | 79 | 高 |
| 2 | 000636 | 风华高科 | MLCC 制造 | 24 | 24 | 26 | 74 | 中 |
| 3 | 300285 | 国瓷材料 | 陶瓷粉体 | 22 | 25 | 25 | 72 | 中 |
| 4 | 603678 | 火炬电子 | 高可靠 MLCC | 20 | 18 | 28 | 66 | 中 |
| 5 | 301511 | 达利凯普 | RF MLCC | 18 | 20 | 22 | 60 | 低 |

每只附：推荐理由 / 主要风险 / 关注权重
```

## 设计原则

- **数据 + LLM 双轨**：能用搜索/akshare 拉到的数据先拉，LLM 在数据基础上综合判断，不允许编造数字
- **环节粒度可控**：默认 5-8 环节，太粗看不出卡脖子，太细搜索成本高
- **可解释**：每个分数都要能拆解到具体维度 + 数据来源 + evidence_id 引用
- **降级友好**：搜索源全挂 → 走 akshare + LLM 知识并标 `data_quality=degraded`；LLM 不可用 → 输出搜索摘要 + 模板评分

## 搜索源降级链

`_get_search_provider` 按以下顺序选可用源，全失败返回 `(None, None)`：

1. **Tavily**（首选，有 AI 摘要）— `TAVILY_API_KEYS` 多 Key 轮询
2. **智谱 BigModel web_search_pro**（兜底，付费）— `ZHIPU_API_KEY` + `ZHIPU_SEARCH_ENGINE`（search_std/search_pro）
3. 都失败 → akshare 个股新闻仍可拉，bottleneck/scoring 评分标 `data_quality=degraded`

两个 provider 都实现 `search_with_ai_summary(query, max_results) -> {results, answer}` 接口，下游无感切换。智谱不返回 AI 摘要，`answer` 字段留空。

## evidence_id 引用机制

`_segment_search` 给每条搜索结果和 akshare 新闻编号：

- `[T1] [T2] ...` — 网络搜索结果（Tavily 或 智谱）
- `[A1] [A2] ...` — akshare 个股新闻

`content_text` 改成 `[T1] {title} | {content}` 格式，喂给 BOTTLENECK prompt。LLM 被强制要求：

1. 每环节 `reasoning` 必须以 `evidence: [T1,T3]` 起头引用至少一个 ID
2. `extracted_numbers` 字段从 evidence 文本中抄 CR3/国产化率数字，找不到给 null
3. `evidence_ids` 数组列出该环节判断引用的所有 ID

报告"卡脖子分析"表会展示 `extracted_numbers` 列，每条 reasoning 旁标注 `[evidence: IDs]`，可追溯到具体搜索结果。

## 候选股排序与评分数据

`score_candidates` 候选池构建后按以下优先级排序再截断到 `top_n*2`：

1. `force_include`（stock 模式强制塞入的目标股）
2. `cn_leaders`（拆解阶段 LLM 给出的龙头）
3. `news_discovery`（搜索文本中发现的活跃标的）
4. 同 source 优先级内：卡脖子环节优先 → 新闻命中数多 → 市值大

每只候选股注入 PE/市值/涨跌幅（来自 `chain_agent.scoring.quotes.get_quote_provider().get_quotes(codes)`），SCORING prompt 强制要求 LLM 在业绩维度评分中引用这些数字；`pe=null` 时该子维度给中等分 7 并标注 "PE 数据缺失"。
