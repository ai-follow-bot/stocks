# 每日板块共振自进化系统 SPEC

> 版本: 1.0  
> 日期: 2026-07-14  
> 状态: 草案  

---

## 1. 目的

基于财联社每日新闻流，自动计算A股板块共振强度，输出每日共振简报，并通过自进化机制持续优化共振权重，最终收敛到稳定的预测模型。

### 核心目标

1. **每日17:00自动运行**，输出当日板块共振排行榜TOP10
2. **自进化**：根据T+1日实际涨跌幅反馈，贝叶斯更新共振权重
3. **收敛**：连续14天权重变化<1%时冻结，实现稳定运行
4. **完全复用现有基础设施**，不修改 `chain_agent/` 任何代码

---

## 2. 架构总览

```
cron (交易日 16:30)
    │
    ▼
daily_resonance.py (主入口)
    │
    ├── Agent 1: 事件分类与板块映射  (关键词匹配 + LLM兜底)
    │
    ├── Agent 2: 共振计算          (确定性Python公式)
    │
    ├── Agent 3: 报告生成          (单次LLM调用)
    │
    └── 自进化: 反馈学习           (次日运行时触发)
```

### 目录结构

```
/opt/stocks/skills/daily_resonance/
├── __init__.py
├── __main__.py              # python -m skills.daily_resonance
├── agent1_classify.py       # 事件分类与板块映射
├── agent2_resonance.py      # 共振计算
├── agent3_report.py         # 报告生成
├── evolution.py             # 自进化：反馈学习 + 权重更新
├── config.py                # 配置（权重初始值、收敛参数等）
├── data.py                  # 数据加载（新闻、板块、关键词）
└── output/                  # 每日输出
    ├── resonance_{date}.json   # 共振排行榜结构化数据
    ├── resonance_{date}.md     # 每日简报
    └── evolution_state.json    # 自进化状态持久化
```

---

## 3. 数据源（全部复用现有资产）

| 数据 | 来源 | 加载方式 |
|------|------|---------|
| 财联社新闻 | `/root/.hermes/.../latest_news.json` | `data.py` 读取 |
| 板块生态系统 | `/opt/stocks/data/sector_ecosystem.json` | `data.py` 读取 |
| 板块关键词 | `/opt/stocks/data/sector_keywords.json` | `data.py` 读取 |
| A股全名单 | `/opt/stocks/data/a_stock_list.json` | `data.py` 读取 |
| 申万行业映射 | `/opt/stocks/data/sw_sector_mapping.json` | `data.py` 读取 |
| 同花顺热度 | `/opt/stocks/data/ths_hot_cache.json` | 可选辅助因子 |
| 历史存档 | `output/evolution_state.json` | `evolution.py` 读写 |

### 财联社新闻结构

每条新闻包含以下关键字段：
- `id`, `title`, `content`, `brief` — 文本内容
- `date`, `publish_time` — 时间信息
- `stock_codes` — 关联股票代码（约3.8%的新闻有值）
- `importance` (0-500) — 重要性评分
- `level` (A/B/C) — 新闻级别
- `tags` — 标签

---

## 4. 三个Agent详细设计

### 4.1 Agent 1: 事件分类与板块映射

**文件**: `agent1_classify.py`

**输入**: 当日新闻列表 + 板块关键词表  
**输出**: `{sector_key: {events: [...], stats: {...}}}`

**处理流程**:

```
Step 1 — 关键词匹配（确定性规则）
  for each news in daily_news:
    for each sector in ecosystem:
      title_content = news.title + " " + news.content
      for keyword in sector_keywords[sector]:
        if keyword in title_content:
          mapping[news.id].add(sector)
          break  # 一个板块匹配一个关键词即可

Step 2 — 个股关联
  如果 news.stock_codes 非空：
    for code in news.stock_codes:
      stock_name = a_stock_list[code]
      将该新闻关联到 stock_name 对应的板块
  否则：
    用 a_stock_list 做名称模糊匹配（title/content中出现的股票名）

Step 3 — 事件类型推断
  基于关键词匹配结果推断事件类型：
  - 含"政策/印发/出台/鼓励" → policy
  - 含"突破/研发/量产/交付" → technology
  - 含"业绩/营收/利润/财报" → earnings
  - 含"扩产/投资/产能/项目" → capacity
  - 含"订单/中标/合同/采购" → order
  - 含"涨价/降价/供需/紧缺" → supply_demand
  - 其他 → general

Step 4 — LLM兜底（仅当关键词匹配无法确定事件类型时）
  收集 "关键词匹配数=0" 的新闻，用LLM batch分类
  限制：每天最多100条需要LLM兜底
```

**函数签名**:
```python
def classify_events(
    news_list: list[dict],
    sector_keywords: dict[str, list[str]],
    stock_list: dict[str, str],
    ecosystem: dict,
    llm_client: Optional[object] = None
) -> dict[str, dict]:
    """返回 {sector_key: {events: [...], stats: {...}}}"""
```

### 4.2 Agent 2: 共振计算

**文件**: `agent2_resonance.py`

**输入**: Agent 1的输出 + 历史统计数据  
**输出**: 排序后的板块共振列表

**共振公式**（确定性Python代码，非LLM）：

```python
def compute_resonance_score(
    sector_data: dict,     # Agent 1的输出
    history: dict,         # 历史数据
    weights: list[float],  # [w1, w2, w3, w4, w5]
    ecosystem: dict,       # 板块生态系统
    ths_hot: dict = None   # 同花顺热度（可选）
) -> list[dict]:
```

**五个维度**:

| 维度 | 权重默认值 | 计算方式 | 范围 |
|------|-----------|---------|------|
| 事件密度 | 0.25 | 当日事件数 / 近30日均值（上限3x） | [0, 3] |
| 情绪强度 | 0.25 | (正面事件数 - 负面事件数) / 总事件数 | [-1, 1] |
| 产业链共振 | 0.20 | 上下游板块同时出现事件的加权和 | [0, 1] |
| 事件多样性 | 0.15 | 不同事件类型数 / 5 | [0, 1] |
| 重要性加权 | 0.15 | 所有事件importance之和 / 500（上限1） | [0, 1] |

**最终分数 = (各维度加权和) × 100**，范围 [0, 100]

**产业链共振计算**:
```python
def compute_chain_resonance(sector, events, ecosystem):
    """计算产业链上下游共振强度"""
    upstream = ecosystem.get(sector, {}).get("upstream", [])
    downstream = ecosystem.get(sector, {}).get("downstream", [])
    related = ecosystem.get(sector, {}).get("related", [])
    # 检查上下游板块是否有事件
    chain_hits = 0
    for us in upstream:
        if us in all_sector_events:
            chain_hits += 1
    for ds in downstream:
        if ds in all_sector_events:
            chain_hits += 1
    total = len(upstream) + len(downstream) + len(related)
    return chain_hits / max(total, 1)
```

### 4.3 Agent 3: 报告生成

**文件**: `agent3_report.py`

**输入**: Agent 2的共振排行榜 + 关联新闻原文  
**输出**: Markdown格式的每日共振简报

**函数签名**:
```python
def generate_report(
    resonance_list: list[dict],
    sector_events: dict,
    raw_news: list[dict],
    date: str,
    llm_client: object
) -> str:
```

**报告模板**:

```markdown
# 每日板块共振简报 — {date}

## 共振总览
今日最强共振板块及驱动因素概览。

## TOP3 深度分析
### 1. {sector_name} — 共振分数: {score}
- **共振逻辑**: ...
- **关键事件**: ...
- **持续性判断**: ...

### 2. ...

### 3. ...

## 风险提示
- 情绪过热板块
- 情绪-业绩背离
- 其他风险

## 附录
- 完整排行榜TOP10
- 方法论说明
```

---

## 5. 自进化机制

**文件**: `evolution.py`

### 5.1 反馈循环

```
T日 16:30 → 运行共振系统 → 输出TOP3板块
T+1日 16:30 → 获取TOP3板块的实际涨跌幅
           → 计算预测准确率
           → 更新权重
           → 运行当日的共振系统
```

### 5.2 准确率计算

```python
def compute_accuracy(top3_sectors: list[str], market_data: dict) -> float:
    """
    计算TOP3预测准确率
    - 方向准确率：预测为"共振"的板块，实际涨跌幅>0的比例
    - 排名准确率：TOP1是否确实是涨幅最大的板块
    - 综合准确率 = 0.7 × 方向准确率 + 0.3 × 排名准确率
    """
```

### 5.3 贝叶斯权重更新

```python
def update_weights(
    weights: list[float],
    accuracy: float,
    days_run: int,
    feature_contributions: list[float]
) -> list[float]:
    """
    贝叶斯学习率衰减更新
    - learning_rate = 1.0 / (1.0 + days_run)
    - 每个维度的权重按该维度的贡献度调整
    - 归一化确保权重之和为1
    """
```

### 5.4 收敛判定

```python
def check_convergence(weight_history: list[list[float]]) -> bool:
    """
    收敛条件（全部满足）：
    1. 连续14天运行
    2. 最近14天的权重变化 Δw < 0.01
    3. 最近14天平均准确率 >= 55%
    """
```

### 5.5 市场结构变化检测

```python
def check_regime_change(accuracy_history: list[float]) -> bool:
    """
    如果连续7天准确率 < 50%，认为市场结构发生变化
    触发：重置权重到均匀分布，重置学习率计数器
    """
```

### 5.6 状态持久化

```json
// output/evolution_state.json
{
  "last_date": "2026-07-14",
  "days_run": 30,
  "converged": true,
  "converged_at": "2026-07-10",
  "weights": [0.22, 0.30, 0.22, 0.13, 0.13],
  "weight_history": [[0.25, 0.25, 0.20, 0.15, 0.15], ...],
  "accuracy_history": [0.52, 0.58, ...],
  "feature_contribution_history": [...],
  "top3_history": [
    {"date": "2026-07-14", "top3": ["optical_module", "pcb", "hbm"], "accuracy": 0.67}
  ],
  "sector_daily_counts": {
    "optical_module": {"dates": {...}, "avg_30d": 12.5},
    ...
  }
}
```

---

## 6. 主入口

**文件**: `__main__.py`

```python
def main(date: str = None):
    """
    主入口：运行每日共振系统
    
    流程：
    1. 加载所有数据（新闻、板块、关键词、A股名单）
    2. 加载进化状态
    3. 如果有前一天的预测，执行反馈学习
    4. Agent 1: 事件分类与板块映射
    5. Agent 2: 共振计算
    6. Agent 3: 报告生成
    7. 保存进化状态
    8. 输出结果
    """
```

**CLI接口**:
```bash
# 运行今日共振
/opt/stocks/.venv/bin/python -m skills.daily_resonance

# 指定日期（回测）
/opt/stocks/.venv/bin/python -m skills.daily_resonance --date 2026-07-13

# 仅运行（不触发自进化）
/opt/stocks/.venv/bin/python -m skills.daily_resonance --no-evolve

# 输出JSON
/opt/stocks/.venv/bin/python -m skills.daily_resonance --json
```

---

## 7. cron配置

```bash
# crontab -e
# 交易日16:30运行每日共振系统
30 16 * * 1-5 cd /opt/stocks && .venv/bin/python -m skills.daily_resonance >> output/cron.log 2>&1
```

---

## 8. 成本估算

| 组件 | 每日成本 | 月度成本 |
|------|---------|---------|
| Agent 1（关键词匹配） | ¥0 | ¥0 |
| Agent 1（LLM兜底） | ~¥0.02 | ~¥0.44 |
| Agent 2（共振计算） | ¥0 | ¥0 |
| Agent 3（报告生成） | ~¥0.08 | ~¥1.76 |
| **合计** | **~¥0.10** | **~¥2.20** |

---

## 9. 回测计划

利用历史财联社数据（2026-04-19至今）进行回测：

```
1. 对每一天运行 Agent 1 + Agent 2（不含Agent 3，不含自进化）
2. 记录每天的共振排行榜TOP3
3. 用T+1日实际涨跌幅计算准确率
4. 运行自进化模拟，观察收敛曲线
5. 回测指标：
   - 方向准确率（TOP3涨跌幅>0的比例）
   - 平均超额收益（TOP3 vs 大盘）
   - 收敛天数
   - 稳定性（权重变化方差）
```

---

## 10. 风险与限制

1. **新闻质量依赖**: 财联社新闻的覆盖范围和时效性直接影响结果
2. **板块覆盖局限**: 当前30个板块偏AI算力基础设施，覆盖面有限
3. **情绪分析的简化**: 当前用关键词正负匹配，准确度有限
4. **无基本面验证**: 共振信号未经业绩/估值验证，可能是纯情绪驱动
5. **过拟合风险**: 权重可能过度拟合历史数据，市场结构变化时需要重置

---

## 11. 实施计划

| 阶段 | 内容 | 文件 | 预估 |
|------|------|------|------|
| Phase 1 | 目录结构 + `config.py` + `data.py` | 数据加载 | 30min |
| Phase 2 | `agent1_classify.py` — 关键词匹配+事件分类 | Agent 1 | 45min |
| Phase 3 | `agent2_resonance.py` — 共振计算 | Agent 2 | 45min |
| Phase 4 | `agent3_report.py` — 报告生成 | Agent 3 | 30min |
| Phase 5 | `evolution.py` — 自进化+收敛 | 自进化 | 45min |
| Phase 6 | `__main__.py` — 主入口+CLI | 主入口 | 30min |
| Phase 7 | 回测+调参 | 验证 | 60min |
| Phase 8 | cron配置+文档 | 部署 | 15min |

---

## 12. 不修改的文件清单

以下文件**绝不修改**（仅读取不写入）：
- `chain_agent/` 下的所有文件
- `data/` 下的所有JSON文件
- `/root/.hermes/` 下的所有文件
- `/home/smallsite-vue/` 下的所有文件
