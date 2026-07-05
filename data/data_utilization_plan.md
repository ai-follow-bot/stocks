# 数据利用效率改进计划

> 基于 2026-06-21 对 `optical_module` 实测得出。等下次动手改时按此文档执行。

## 实测基线（2026-06-21 optical_module 任务）

| 阶段 | 数据流 | 利用率 |
|---|---|---|
| 采集 | 财联社 9 条 (2569 字符) + akshare 29 条 (5253 字符) + Tavily 10 条 (5822 字符) | combined_text 13648 字符 |
| discovery | combined_text 全量进 StockDetector NLP | ✅ 100% |
| heuristic 评分 | 只看 `news_hits` 整数 → `min(40, nh*8)` | ❌ 内容全弃 |
| tech_option | 每股二次调 Tavily，10 条结果取 300 字符 | ❌ ~6% |
| LLM 综合 | tavily[:4000] + demand[:4000]，不传 demand_secondary | ❌ 31-92% 截断 + akshare 0% 进 LLM |
| LLM 上下文 | 输入 ~10K 字符 / Claude 200K 窗口 | ⚠️ 仅 5% 占用 |

## 五大效率问题

### 1. akshare 数据被 LLM 完全丢弃 ⚠️ 最严重
- **位置**: `chain_agent/agent.py:115`
- **现状**: `news_content = (coll["demand"].get("content_text") or "")[:4000]` 只取主轨 `demand`
- **问题**: 实测 akshare 29 条 5253 字符，财联社主轨 9 条 2569 字符——akshare 数据量是财联社 2 倍却被完全丢弃
- **影响**: 当 akshare 是补充轨时（正常情况），它的数据进 discovery 但进不了 LLM 综合报告

### 2. 4000 字符硬截断丢失 31-92% 内容
- **位置**: `chain_agent/agent.py:114-115`
- **现状**: 两处 `[:4000]`
- **问题**: Tavily 5822 字符截到 4000 丢 31%；财联社长任务（50+ 命中）25000+ 字符截到 4000 丢 84%
- **影响**: LLM 上下文 200K 窗口只用 5%，截断毫无必要

### 3. heuristic 评分信息坍缩
- **位置**: `chain_agent/scoring/heuristic.py:20`
- **现状**: `news_score = min(40, nh * 8)`
- **问题**:
  - 新闻内容、时间、情感、`importance`/`level` 字段全部丢弃
  - 只剩"命中次数"标量，5 次就溢出封顶
  - 财联社新闻有 `importance` / `level` 字段未利用

### 4. tech_option 二次 Tavily 调用浪费
- **位置**: `chain_agent/scoring/tech_option.py:85-94`
- **现状**: 每个技术期权股单独调一次 Tavily，10 条结果只取 300 字符塞 `tavily_snippet`
- **问题**: 一次调用配额 5000 字符，实际用 300 字符 → 6% 利用率；N 个候选 N 次 API 调用，配额线性放大

### 5. 三轨独立调用，无去重
- **位置**: `chain_agent/collectors/orchestrator.py:88-103`
- **现状**: 三轨并行采集后直接拼 combined_text
- **问题**: 同一新闻可能在财联社 + akshare 重复出现 → news_hits 翻倍计分 → 评分虚高

---

## 改进计划（按 ROI 排序）

### P0 - 立即改，3 行代码翻 5 倍数据量

#### P0-1: 把 demand_secondary 也拼进 LLM news_content
**文件**: `chain_agent/agent.py:115`

**改前**:
```python
news_content = (coll["demand"].get("content_text") or "")[:4000]
```

**改后**:
```python
news_content = "\n\n".join(filter(None, [
    coll["demand"].get("content_text", ""),
    coll.get("demand_secondary", {}).get("content_text", ""),
]))[:20000]
```

**收益**: akshare 数据 0% → 100% 进 LLM；需求侧舆情从 2569 → 7822 字符完整呈现

#### P0-2: 放宽 LLM 输入截断到 20000 字符
**文件**: `chain_agent/agent.py:114-115`

**改前**:
```python
tavily_content = (coll["supply"].get("content_text") or "")[:4000]
news_content = (coll["demand"].get("content_text") or "")[:4000]
```

**改后**:
```python
tavily_content = (coll["supply"].get("content_text") or "")[:20000]
# news_content 见 P0-1
```

**收益**: 截断损失 31-84% → 0%；LLM 上下文占用 5% → 25%，仍有 75% 余量

#### P0 验证步骤
1. 改完跑 `KIMI_API_KEY=... .venv/bin/python -m chain_agent.agent optical_module --llm --days 7 --top-n 10 --out /tmp/p0_after.md`
2. 对比 `/tmp/test_serenity_on.md` 看报告是否包含更多 akshare 来源的内容
3. 检查 LLM 输入字符数：在 `agent.py:130` 前加 `print(f"[LLM input] {len(user_prompt)} chars", file=sys.stderr)`

---

### P1 - 改评分质量

#### P1-1: heuristic 引入财联社 importance/level 加权
**文件**: `chain_agent/scoring/heuristic.py:19-23`

**改前**:
```python
nh = cand.get("news_hits", 0)
news_score = min(40, nh * 8)
score += news_score
if nh:
    rationale.append(f"新闻命中 {nh} 次 (+{news_score})")
```

**改后**（需要 candidates.py 在合并时把 importance 累加进 cand）:
```python
nh = cand.get("news_hits", 0)
importance_sum = cand.get("news_importance_sum", 0)  # 来自财联社 importance 字段累加
# 基础分：命中次数对数缩放（避免溢出）
news_score = min(25, int(__import__('math').log(nh + 1) * 12))
# 加权分：重要新闻额外加分
news_score += min(15, importance_sum * 3)
score += news_score
if nh:
    rationale.append(f"新闻命中 {nh} 次 (+{min(25, int(__import__('math').log(nh+1)*12))}, 重要性 +{min(15, importance_sum*3)})")
```

**配套改动**: `chain_agent/discovery/candidates.py` 在 `news_hits` 累加处同时累加 `news_importance_sum`（财联新闻 `importance` 字段，akshare 默认 1）

**收益**: 评分从"次数"升级到"重要性×次数"，财联社 `importance` 字段从弃用到生效

#### P1-2: tech_option 改用主调 Tavily results 池
**文件**: `chain_agent/scoring/tech_option.py:85-94`

**改前**: 每个技术期权股单独调 `tavily_search.search_industry_news(stock_name)`，取 300 字符

**改后**: 主 pipeline 已有一次 `collect_supply_side(sector)` 调用，把 `tavily_results` 列表传给 `analyze_tech_options`，按股票名/代码在 results 的 title+content 里做 substring 匹配，命中即取该 result 的 content[:500] 作为 snippet；未命中再 fallback 到单独调 Tavily

**收益**: Tavily API 调用 N → 1，配额省 N-1 倍；snippet 从 300 → 500 字符，利用率 6% → 50%+

#### P1 验证步骤
1. 改完跑同一任务，对比 `news_hits` 高但 `importance_sum` 低的候选分数变化
2. 跑 tech_option 阶段时 print API 调用次数，确认从 N → 1

---

### P2 - 改数据干净度

#### P2-1: combined_text 跨轨去重
**文件**: `chain_agent/collectors/orchestrator.py:105-109`

**改前**:
```python
combined = "\n\n".join([
    supply.get("content_text", ""),
    primary_demand.get("content_text", ""),
    secondary_demand.get("content_text", ""),
]).strip()
```

**改后**:
```python
def _dedup(texts: list[str]) -> str:
    seen = set()
    out = []
    for t in texts:
        if not t: continue
        for chunk in t.split("\n"):
            # 用前 100 字符做指纹
            fp = chunk[:100].strip()
            if fp and fp not in seen:
                seen.add(fp)
                out.append(chunk)
    return "\n".join(out)

combined = _dedup([
    supply.get("content_text", ""),
    primary_demand.get("content_text", ""),
    secondary_demand.get("content_text", ""),
]).strip()
```

**收益**: 消除跨轨重复，news_hits 不虚高，候选评分更准

#### P2-2: heuristic 改对数缩放避免 5 次溢出
**文件**: `chain_agent/scoring/heuristic.py:20`

**改前**: `news_score = min(40, nh * 8)`

**改后**: `news_score = min(40, int(__import__('math').log(nh + 1) * 17))`
- nh=1 → 12 分
- nh=3 → 24 分
- nh=5 → 30 分
- nh=10 → 41 → cap 40
- nh=20 → cap 40

**收益**: 高频股 (nh=10+) 不再全部封顶 40 分，能拉开差距；低频股 (nh=1) 不再被低估

#### P2 验证步骤
1. 跑同一任务，看 combined_text 长度变化（应略减）
2. 对比候选 news_hits 分布，看封顶候选数是否减少

---

## 执行顺序建议

1. **先 P0**（3 行代码，5 分钟改完，立竿见影）
2. 跑一次 optical_module 对比报告，确认 LLM 输入字符数翻 5 倍
3. **再 P1**（heuristic + tech_option，~30 行代码，改评分质量）
4. 跑对比看候选分数变化
5. **最后 P2**（去重 + 对数缩放，~20 行代码，清理数据干净度）

## 改动文件清单

| 文件 | P0-1 | P0-2 | P1-1 | P1-2 | P2-1 | P2-2 |
|---|---|---|---|---|---|---|
| `chain_agent/agent.py` | ✅ | ✅ | | | | |
| `chain_agent/scoring/heuristic.py` | | | ✅ | | | ✅ |
| `chain_agent/discovery/candidates.py` | | | ✅ 配套 | | | |
| `chain_agent/scoring/tech_option.py` | | | | ✅ | | |
| `chain_agent/scoring/integrator.py` | | | | ✅ 传参 | | |
| `chain_agent/collectors/orchestrator.py` | | | | | ✅ | |

## 风险点

- **P0-2 放宽截断到 20000**: Kimi 上下文 128K，Claude 200K，输入 50K 内都安全；但若用 Deepseek 32K 限制需把上限降到 8000
- **P1-1 importance 字段**: 需确认财联社 `latest_news.json` 每条都有 `importance` 字段（实测首条样本有，但需确认非空率）
- **P2-1 去重指纹**: 用前 100 字符可能误判（同标题不同正文），可改用 `stock_codes + date + title[:50]` 三元组
