---
name: sector-strategy
description: |
  添加/修改 /opt/stocks 的板块分析策略（chain_agent / skills.deep-analyze / valuation-lens /
  新策略）时，遵循「共享数据层 + 分析逻辑分离」原则：数据采集统一走
  chain_agent/sector_data.gather()（板块->关键词->核心公司+搜索+数据），各策略只写分析逻辑。
  触发词：「加策略」「新增分析策略」「加个 lens」「板块策略」「新分析视角」「周期镜头」「事件驱动」
  「改 chain/deep/val 的采集」「sector_data」。
  不触发：纯数据文件编辑（sector_ecosystem/keywords/core_companies JSON）、纯前端改动、
  单只股票分析（analyze_stock 不走板块采集）。
---

# 板块分析策略 · 共享数据层 + 分析逻辑分离

## 核心原则

**所有板块相关分析走同一条数据采集路子，只是分析逻辑不一样。**

```
sector_data.gather(sector)   ← 共享数据层（唯一采集入口，chain_agent/sector_data.py）
  产出 {sector, canon, sector_name, keywords, core_companies,
        candidate_pool, board_evidence(T/A), data(quotes)}
        │
        ├── 策略A 分析逻辑（只写这一段）
        ├── 策略B 分析逻辑
        └── 策略C 分析逻辑
```

- **共享（sector_data，别重写）**：板块、关键词、核心公司、候选池（core+搜索+财联社+档案，多标签过滤）、board 搜索 evidence、基础行情。
- **分析侧（各策略自己写）**：策略专属搜索（如 deep 的 segment、val 的 S/F/D）、策略专属数据（如 chain 的资金面/龙虎榜）、打分/估值逻辑、prompt。

## 数据三件套在哪

| 数据 | 来源 | 共享层用法 |
|---|---|---|
| 板块 | ecosystem canon/sector_name | gather 输入 |
| 板块关键词 | sector_keywords.json (SECTOR_KEYWORDS) | gather 搜索 query + 财联社过滤；各策略 prompt 注入 `sd["keywords"]` |
| 板块核心公司 | sector_keywords.json core_companies + 多标签 KB | gather 候选种子；deep 还 force_include |

降级链（所有搜索点统一）：Tavily 3key 轮询 -> 智谱兜底 -> 财联社/core/archive/akshare。
共享层已内置 Tavily->智谱 failover，策略别自己写搜索 failover。

## 新增策略的标准 recipe

```python
# skills/<新策略>/analyzer.py
def analyze_chain(chain, days=14, top_n=8):
    from chain_agent import sector_data
    sd = sector_data.gather(chain, days=days, top_n=top_n)   # 共享数据，不自己采
    candidates = sd["candidate_pool"]                        # 已过滤的候选池
    # ===== 只写策略专属分析逻辑 =====
    # 用 sd["board_evidence"] 作背景、sd["data"] 拿 PE、sd["keywords"] 注入 prompt
    # 额外数据（如历史 EPS）-> 分析侧 enrich（像 chain_agent/agent.py 的 enrich_candidates），
    #   不塞进 sector_data，除非多个策略都要才考虑上提共享
    scored = _your_score(candidates, sd, ...)
    return {"chain_name": sd["sector_name"], "candidates": scored, ...}
```

接 harness 并行（可选）：`skills/harness/orchestrator.py::run_harness_chain` 的 paths 加一路
`("<name>", "skills.<新策略>", [...args], _path_timeout(...))`，align 自动按 code 对齐它的分数。

## 改现有策略的规则

- **改分析逻辑**（打分维度、prompt、估值方法）：直接改对应 skill，不动 sector_data。✅
- **改数据采集**（候选发现、搜索源、过滤）：先看该不该进 sector_data。
  - 多策略受益的采集改动 -> 改 `sector_data.py`（共享层），三路径自动继承。
  - 单策略专属数据（如 chain 资金面、deep segment）-> 留各策略，不进共享层。
- **别在策略里重写采集**：若发现某策略自己写了 collect_all/discover/搜索，refactor 成调 `gather()`（参考 feat/sector-data-layer 分支对 chain/deep/val 的迁移）。

## 关键文件

| 角色 | 路径 |
|---|---|
| 共享数据层 | `chain_agent/sector_data.py`（gather / _board_search / _cailianshe_hot / _recall_archive） |
| 多标签归属 | `chain_agent/discovery/stock_detector.py`（determine_sectors / _CODE_TO_SECTORS） |
| 三路径分析 | `chain_agent/agent.py`（chain）、`skills/deep-analyze/analyzer.py`、`skills/valuation-lens/analyzer.py` |
| 搜索 failover | `chain_agent/collectors/orchestrator.py`（_get_search_provider/_search_failed）+ tavily_search/zhipu_search |
| harness 对齐 | `skills/harness/orchestrator.py` + `align.py` |
| 431 编排 | `skills/ce-value/analyzer.py`（macro->market->sector_picker->harness->financials->three_high） |

## Do / Don't

- ✅ 新策略 `from chain_agent import sector_data; sd = sector_data.gather(...)` 起手。
- ✅ 策略 prompt 注入 `sd["keywords"]`（与 chain/deep/val 一致）。
- ✅ 额外数据需求走分析侧 enrich（像 chain 的资金面）。
- ✅ 采集层改动优先进 sector_data，让三路径继承。
- ❌ 别在策略里自己写 collect_all / discover_candidates / Tavily 调用（重复采集 + 漏 failover）。
- ❌ 别把策略专属搜索（segment/S-F-D）塞进共享层（那是分析逻辑）。
- ❌ 别让候选池各策略各发现各的（统一用 gather 的 pool，靠 harness align 对齐分数）。

## 验证

改完跑 `python -m <策略> <板块> --json --top-n 8`，确认：候选池来自 gather（关键词/core 在）、
走 Tavily->智谱降级、分析逻辑产出策略专属分数、不崩。
