# SPEC: skills.report-judge（报告质量评判）

> **状态**：已设计待开发（2026-07-12）。本 spec 自包含，新会话可直接据此实施。
> **特性**：报告出完后异步调 LLM 用 rubric 评判，输出结构化质量分 + issues + 改进建议。不进 pipeline 主链路，不影响报告产出速度。为系统自改进闭环（出报告->评判->聚合->改模块）提供数据。

## 1. 目标

给 chain_agent / deep-analyze / valuation-lens / cycle-lens / harness / ce-value 产出的报告做**后评判**：

- **质量分**（A/B/C/D）：一眼看出哪些报告值得细读、哪些有明显问题。
- **结构化 issues**：候选遗漏 / 证据不足 / 视角矛盾 / 分析模板化 / 数据过期 / 风险遗漏，带 severity + detail。
- **改进建议**：可操作的改进方向（如「候选池漏了 XX，建议加关键词 YY」）。
- **跨视角冲突**：harness/ce-value 报告里 chain/deep/val/cycle 分数/分类有无未解释的矛盾。
- **聚合统计**：积累 20+ 份后，按 issue 类型/板块/任务类型聚合，暴露系统性弱点。

## 2. 与现有 skill 的边界

- **不调** harness / deep-analyze / valuation-lens / cycle-lens / chain_agent.agent / ce-value（不重跑分析）。
- **只读**报告文本（markdown）+ pipeline 输出 JSON（如果有的话）+ task 元数据（task_type/sector/days/data_quality）。
- **复用** `chain_agent.llm.client`（get_llm_client）+ `chain_agent.llm.parse`（json_from_llm）+ `chain_agent.config`。
- **独立 archive** `output/report_judge_archive.json`（不碰 valuation_stock_archive / cycle_archive）。
- 评判 LLM 最好用**与报告生成不同的模型**（如报告用 DeepSeek，评判用 kimi/GLM），避免同源盲区。环境变量 `JUDGE_LLM_PROVIDER` 控制（默认 `auto` = 优先 GLM，fallback DeepSeek）。

## 3. 触发时机

- **异步**：报告写完后（task status=success），worker 自动调 judge（不阻塞报告展示）。
- **手动**：CLI `python -m skills.report-judge --file <report.md> [--json]` 单独评判一份。
- **批量**：`python -m skills.report-judge --batch --limit 20` 评判最近 20 份未评判的报告。
- judge 失败不影响报告本身（graceful degradation，judge 结果为空）。

## 4. 数据源

| 要素 | 来源 | 说明 |
|---|---|---|
| 报告文本 | `STOCKS_OUTPUT_DIR/<filename>.md` | markdown 原文 |
| task 元数据 | `stocks_tasks` SQLite（通过 filename 里的 task_id 反查）| task_type / sector / days / data_quality / llm_model |
| pipeline JSON（可选） | `STOCKS_OUTPUT_DIR/<filename>.json`（如果 --json 跑过）| 候选池 / 分数 / evidence 数 / 路径状态 |
| 板块核心公司 | `chain_agent.discovery.stock_detector.load_core_companies(sector)` | 检查候选覆盖 |
| 历史评判 | `output/report_judge_archive.json` | 上次评判结果（对比改善/退化） |

## 5. 模块结构

```
skills/report-judge/
  __init__.py
  __main__.py     # CLI: --file / --batch / --json
  rubric.py       # rubric 定义 + 评判 prompt 构建
  judge.py        # 评判逻辑：读报告 + 元数据 -> LLM -> 结构化输出
  archive.py      # 评判结果存档 + 聚合统计
  prompts.py      # LLM prompt（rubric system + user template）
```

## 6. rubric.py（评判维度）

```python
RUBRIC = [
    {"key": "coverage", "name": "候选覆盖", "check": "核心公司是否都在？有无明显遗漏？", "weight": 20},
    {"key": "evidence", "name": "证据质量", "check": "evidence 编号是否支撑结论？有无空引？降级时是否标注？", "weight": 20},
    {"key": "consistency", "name": "视角一致性", "check": "chain/deep/val/cycle 分数/分类有无矛盾？矛盾是否被解释？", "weight": 15},
    {"key": "depth", "name": "分析深度", "check": "有无具体数据（EPS/PE/市占率/订单）？还是套话？", "weight": 15},
    {"key": "actionability", "name": "可操作性", "check": "结论是否明确（推荐/回避/观望）？还是模棱两可？", "weight": 15},
    {"key": "risk", "name": "风险提示", "check": "有无提周期峰值/解禁/估值泡沫/竞争加剧？", "weight": 15},
]
```

- 每个维度 0-100 分，加权合成总分 -> A(≥85) / B(70-84) / C(55-69) / D(<55)。
- LLM 输出每维度的 score + 1-2 句 reason + 具体 issue（如果有）。

## 7. judge.py 评判逻辑

```python
def judge_report(filepath: str, task_meta: dict = None) -> dict:
    """读报告 + 元数据 -> LLM 评判 -> 结构化输出。

    返回 {
        quality_score: "A"|"B"|"C"|"D",
        total_score: int,  # 0-100
        dimensions: [{key, name, score, reason, issues: [str]}],
        cross_path_conflicts: [str],  # 跨视角未解释的矛盾
        suggestions: [str],  # 改进建议
        judged_at: iso,
        llm_provider: str,
    }
    """
```

- 构建 prompt：report 全文（截断 20K）+ task 元数据（task_type/sector/data_quality）+ rubric。
- LLM 输出 JSON（复用 json_from_llm 防御解析）。
- 失败返回 `{quality_score: null, error: ...}`（不阻塞）。

## 8. prompts.py

```
JUDGE_SYSTEM = """你是买方研究主管，评判下属（AI）写的 A 股投资分析报告质量。
你只看报告文本 + 元数据，不重新分析。按 rubric 逐维度打分 + 指出具体问题。
严格输出 JSON：
{"dimensions": [{"key": "coverage", "score": 0-100, "reason": "...", "issues": ["..."]}, ...],
 "cross_path_conflicts": ["..."],
 "suggestions": ["..."],
 "total_score": 0-100}
不要包裹代码块。"""

JUDGE_USER_TEMPLATE = """# 报告元数据
- 任务类型：{task_type}
- 板块：{sector}
- 数据质量：{data_quality}
- LLM 模型：{llm_model}

# 评判 rubric
{rubric_text}

# 报告全文
{report_text}

请按 rubric 逐维度评判，输出 JSON。"""
```

## 9. archive.py（评判存档 + 聚合）

```python
# output/report_judge_archive.json
# {filename: {quality_score, total_score, dimensions, conflicts, suggestions, judged_at, task_type, sector}}

def upsert_judgment(filename: str, judgment: dict, task_meta: dict) -> dict
def load_judgment(filename: str) -> dict
def aggregate_stats(limit: int = 50) -> dict
    # 返回 {
    #   avg_score: float,
    #   score_dist: {"A": n, "B": n, "C": n, "D": n},
    #   top_issues: [{type, count, examples}],
    #   by_task_type: {task_type: {avg, count}},
    #   by_sector: {sector: {avg, count}},
    #   trend: [{date, avg}],  # 按天均分趋势
    # }
```

## 10. __main__.py CLI

```bash
# 评判单份
python -m skills.report-judge --file output/ban-dao-ti-cai-liao_20260712-100218_92.md --json

# 批量评判最近 N 份未评判的
python -m skills.report-judge --batch --limit 20

# 聚合统计
python -m skills.report-judge --stats
```

## 11. 后端 + 前端接入

### 后端 admin-stocks.ts

- `GET /admin/stocks/reports/:filename` 返回里加 `judgment` 字段（从 archive 读）。
- 报告列表 `GET /admin/stocks/reports` 每条加 `quality_score`（A/B/C/D/null）。
- 新端点 `POST /admin/stocks/reports/:filename/judge` 手动触发评判。
- 新端点 `GET /admin/stocks/reports/judge-stats` 聚合统计。
- task worker（`runTask`）在 task success 后异步调 judge（不阻塞 output_file 返回）。

### 前端 StocksReports.vue

- 报告列表加「质量」列：A/B/C/D badge（绿/蓝/橙/红），null 不显示。
- 报告抽屉 header（report-meta 区）加质量 badge + issues 数量。report-meta 区已有类型标签 + 降级标记（展示优化已做），质量 badge 加在旁边。
- 报告抽屉底部加「评判详情」折叠区：各维度分数条 + issues 清单 + 建议。
- 新页面或 tab「评判统计」：聚合统计可视化（分数分布 + top issues + 趋势）。

## 12. 自动触发（worker 集成）

在 `admin-stocks.ts` 的 task worker（`runTask` 函数），task status 变 success 后：

```ts
// 报告写完，异步触发评判（不阻塞）
if (task.status === 'success' && task.output_file) {
  spawn(STOCKS_VENV_PYTHON, ['-m', 'skills.report-judge',
    '--file', task.output_file], { cwd: STOCKS_ROOT, detached: true });
}
```

- `detached: true` 子进程独立，不阻塞 worker 下一轮 tick。
- judge 失败静默（日志到 stderr，不影响报告）。
- 环境变量 `REPORT_JUDGE_ENABLED`（默认 `1`）控制是否自动评判。

## 13. 约定

- 数据驱动、优雅降级（judge 失败不影响报告，quality_score=null）。
- LLM 输出防御解析（复用 `chain_agent.llm.parse.json_from_llm`）。
- 日志走 stderr（不污染 stdout --json）。
- 评判 LLM 优先用与报告不同的模型（`JUDGE_LLM_PROVIDER` 控制）。
- 评判结果存独立文件 `output/report_judge_archive.json`。
- 不修改报告原文（只读 + 评判）。

## 14. 验证

```bash
# 单份评判
/opt/stocks/.venv/bin/python -m skills.report-judge --file output/ban-dao-ti-cai-liao_20260712-100218_92.md --json

# 批量
/opt/stocks/.venv/bin/python -m skills.report-judge --batch --limit 10

# 聚合统计
/opt/stocks/.venv/bin/python -m skills.report-judge --stats

# 前端
# 报告列表有质量列；报告抽屉有评判详情；统计页可看
```

- 确认：评判出 A/B/C/D + 维度分 + issues + 建议；批量跑 10 份不崩；聚合统计有分布 + top issues。
- 跑两次（改了报告后）验证历史对比（改善/退化）。

## 15. 开发顺序

1. `rubric.py` + `prompts.py`（rubric 定义 + prompt 模板）。
2. `judge.py`（读报告 + LLM + 解析，单份评判）。
3. `archive.py`（存档 + 聚合统计）。
4. `__main__.py`（CLI：--file / --batch / --stats）。
5. 后端 admin-stocks.ts（报告列表加 quality + judge 端点 + worker 自动触发）。
6. 前端 StocksReports.vue（质量列 + 评判详情 + 统计页）。
7. 端到端验证（单份 + 批量 + 聚合 + 前端展示）。

## 16. 参考实现

- LLM 调用 + parse：参考 `skills/ce-value/common.py`（`_llm_call_json` + `json_from_llm`）。
- archive 模式：参考 `skills/cycle-lens/archive.py`（load/save/upsert，独立文件）。
- CLI 模式：参考 `skills/cycle-lens/__main__.py`（argparse + --json + --out）。
- 前端展示：参考 `StocksReports.vue` 的 report-meta badge 区（类型标签 + 降级标记已做，质量 badge 加旁边）。
- worker 集成：参考 `admin-stocks.ts` 的 `runTask`（spawn 子进程，detached）。

## 17. 与 sector-strategy 原则的关系

report-judge **不是板块分析策略**（不调 sector_data.gather，不做板块/关键词/核心公司/搜索/数据采集）。它是**元评判层**（读报告 -> 评判 -> 改进），不遵循 sector-strategy 的「共享数据层 + 分析逻辑分离」原则。它是独立的 post-process skill。

但它的反馈会**驱动 sector-strategy 层的改进**：如果 judge 发现「候选覆盖不足」（coverage 低分），指向 gather 搜索召回不够 -> 改 sector_data 的 query；如果「视角矛盾未解释」（consistency 低分），指向 harness 综合 prompt -> 改 _llm_synthesize。这就是闭环。
