# SPEC: report-judge 闭环改进（重跑 + 评判驱动改进）

> **状态**：Part A 已上线；Part B 闭环已实现并验证；prompt/core_company 一键耦合已实现（2026-07-13）。
> - **prompt/core_company 一键耦合**（用户追加，取代原「prompt 仅复制建议」设计）：core_company 直接写 core_companies（name->code）；prompt_synth/conclusion/risk/search_depth 写 `data/prompt_overrides.json`，各 skill 生成时按 (sector,target) 读注入到 system prompt。三条约束：能用到（runtime 注入）、不影响别的系统（按 sector 隔离，不改全局 prompt 常量）、重复跑不歧义（指纹去重，每次重读 fresh 不追加源码；业绩更新/新股/公告触发的重跑读到同一份 override 一致生效）。验证：apply 写入 + render_override_block 注入 + scoping 隔离 + dismiss 移除 均通过。
> - Part A（重跑按钮 + retry llm_model 修复）：已上线。
> - Part B（action_items -> 聚合 -> apply -> 失效分类缓存 -> 重跑/re-judge -> 验证）：已实现。
> - 正反馈稳定化 3 处 + 修正积累到数据分类：已实现并跑通 HBM 验证。
> **验证结果**（HBM 链路实测）：applied 4 / recurring 4 / verified 1 —— 4 个 applied 关键词全被 judge 重新提（执行缺口），但仅 球硅 真进了最新报告；其余 3 个（安集科技/香农芯创/TSV）指向「股票没分进 HBM」（重分类补）。applied_keyword_hits + prev_ref(72->70/75->72) 均落地。

## 1. 目标

回答「评判结果能否改进整个任务」：能。把 judge 的 issues/suggestions 升级为**结构化 action_items**，按改进层聚合，可自动化的（关键词/核心公司）一键应用，不可自动化的（prompt）转人工，应用后重跑验证分数是否回升。形成 评判 -> 改进 -> 重跑 -> 再评判 的闭环。

## 2. Part A：重跑按钮（小，先做）

报告抽屉 report-meta 在「重新评判」旁加「重跑」按钮：
- 从 filename 抽 task_id（regex `_(\d+)\.(md|json)$`，与 judge.py `_extract_task_id` 一致）。
- 抽不到（如 `cpu_v2.md`）-> 禁用按钮 + tooltip「该报告无任务 ID，无法重跑」。
- 点击 -> ElMessageBox 确认「用原参数重新生成？会新跑一个任务」-> `stocksApi.retryTask(taskId)` -> toast「已重新入队，任务 #N」。
- 新任务跑完 -> worker 自动 judge -> 报告列表出现新文件 + 质量列。
- 前端 `StocksReport` 类型加 `task_id?: number | null`（后端 `/reports` 与 `/reports/:filename` 从 filename 抽，避免前端重复解析）。

**顺带修 retry 的 llm_model 丢失 bug**：`admin-stocks.ts:1380` `row.llm_model === 'kimi' ? 'kimi' : 'glm'` 把 `deepseek` 降级成 `glm`。改为保留原值（`['glm','kimi','deepseek'].includes(row.llm_model) ? row.llm_model : 'glm'`），与 `runTask` 的 spawn 逻辑一致。

## 3. Part B：闭环设计

### 3.1 维度低分 -> 改进层映射

| 低分维度 | 改进层 | 可自动化 | 应用方式 |
|---|---|---|---|
| coverage | `data/sector_keywords.json` / 核心公司 | ✅ | POST/DELETE `/keywords/:sector`、`/core-companies/:sector` |
| evidence | deep segment 搜索 query / `tavily_results` 深度 | 半 | 调参（task 重跑时加大 tavily_results）+ query 人工 |
| consistency | `skills/harness/prompts.py::SYNTH_SYSTEM` | ❌ | 复制建议人工改 prompt |
| depth | `skills/deep-analyze/report.py` + financials | ❌ | 复制建议人工改 |
| actionability | report 结论 prompt | ❌ | 复制建议人工改 |
| risk | report 风险 prompt | ❌ | 复制建议人工改 |

关键词/核心公司由 `chain_agent/discovery/stock_detector.py::_load_sector_keywords`（mtime 缓存）与 `chain_agent/agent.py::_get_sector_leaders` 运行时读，改完下次 task 即生效 -> 这是最干净的自动闭环。

### 3.2 judge 增强：结构化 action_items

`prompts.py` 的 `JUDGE_SYSTEM` 增加输出 `action_items`，`judge.py` 解析规整，存入 judgment entry（向后兼容，旧 entry 无此字段）：

```json
"action_items": [
  {"target": "keyword_add", "sector": "半导体材料", "value": "CMP抛光液",
   "severity": "high", "rationale": "coverage 维度指出缺该细分材料标的", "source_dim": "coverage"},
  {"target": "prompt_synth", "sector": "半导体材料", "value": "综合时对 chain/deep/val 分歧>20分的标的强制解释",
   "severity": "medium", "rationale": "consistency 多份报告未解释矛盾", "source_dim": "consistency"}
]
```

target 取值与可自动化的对应：
- `keyword_add` / `keyword_remove` -> **可自动应用**（低风险，name-based）
- `core_company_add` / `core_company_remove` -> **半自动**（需 name->code 解析，先走 review，二期再接 auto-generate）
- `prompt_synth` / `prompt_conclusion` / `prompt_risk` / `search_depth` -> **仅 review**（复制建议）

prompt 强约束：action_items 必须由前面 issues 推出（rationale 引 source_dim），不得无中生有；keyword_add 的 value 必须是具体词不是泛指。

### 3.3 改进队列 archive

`output/improvements_archive.json`：
```json
{
  "<fingerprint>": {
    "target": "keyword_add", "sector": "半导体材料", "value": "CMP抛光液",
    "severity": "high", "rationale": "...",
    "source_judgments": ["ban-dao-ti-cai-liao_...92.md", "hbm_...101.md"],
    "count": 2,
    "status": "pending|applied|dismissed",
    "applied_at": null, "applied_task_id": null
  }
}
```
- fingerprint = `{target}|{sector}|{value}` 去重键。
- 聚合：扫所有 judgment 的 action_items，按 fingerprint 合并 source_judgments + count，已 dismissed 的不再计入 pending。
- 状态机：pending（聚合出来）-> applied（人工点了应用）/ dismissed（人工忽略）。applied 保留记录（追溯 + 防重复提）。

### 3.4 改进队列 UI

header 加「改进队列」按钮（badge 显示 pending 数），弹 dialog：
- 分 3 tab：🔑 关键词 / 🏢 核心公司 / 📝 prompt 建议
- 每条卡片：[severity badge] value - rationale；副行「来自 N 份报告」（可展开列文件名 + 等级）。
- 关键词类：「应用」(调 apply endpoint) + 「忽略」。
- 核心公司类：仅「复制名称」+ 「忽略」（二期接 auto-generate 后再加「应用」）。
- prompt 类：「复制建议」(clipboard) + 「忽略」。
- applied/dismissed 灰显移到底部，保留可查。

### 3.5 应用 + 验证闭环

1. 关键词「应用」-> 后端 `POST /improvements/:fingerprint/apply` -> 内部调 `POST /keywords/:sector`（add）或 `DELETE /keywords/:sector/:keyword`（remove）-> 标 applied。
2. 应用后该队列项关联的 source_judgments 报告，可点「重跑」（Part A）重新生成 + 自动 judge。
3. 新 judgment 的 coverage 分对比旧分（judgment entry 加 `prev_scores` 软快照：upsert 时若 task_id 相同、coverage 分变了，记 `prev_total`/`prev_coverage` 作「前次 X->本次 Y」走势，不进 LLM）。
4. 改进队列顶部显示「已应用 N 条，相关报告均分 +X 分」的闭环成效（从 applied 项的 source judgments 取最新 judgment 算 delta）。

## 4. 后端改动

- `skills/report-judge/prompts.py` + `judge.py`：输出 + 解析 + 规整 action_items（clamp target 取值、severity）。
- `skills/report-judge/archive.py`：judgment entry 加 `action_items`、`prev_total`/`prev_coverage`（upsert 时对比同 task_id 旧值）。
- 新 `skills/report-judge/improvements.py`（或在 archive.py 扩展）：`aggregate_improvements()` / `apply_improvement(fingerprint)` / `dismiss_improvement(fingerprint)`。
- `admin-stocks.ts`：
  - `/reports` 与 `/reports/:filename` 响应加 `task_id`（filename 抽）。
  - `GET /reports/improvements`（聚合队列，pending 优先）。
  - `POST /reports/improvements/:fingerprint/apply`（keyword 类内部调 keywords CRUD；他类返回 400「需人工」）。
  - `POST /reports/improvements/:fingerprint/dismiss`。
- 修 `admin-stocks.ts:1380` retry 的 llm_model。

## 5. 前端改动

- `StocksReports.vue`：report-meta 加「重跑」按钮（task_id 缺则禁用）+ 确认框；header 加「改进队列」按钮 + dialog（3 tab + apply/dismiss/copy）。
- `api/index.ts`：`retryTask` 已有；加 `getImprovements()` / `applyImprovement(fp)` / `dismissImprovement(fp)`；`StocksReport` 加 `task_id`。

## 6. 开发顺序（分两期）

**期 1（小、立即值）**：
1. 后端：`/reports` + `/reports/:filename` 加 task_id；修 retry llm_model。
2. 前端：report-meta「重跑」按钮 + 确认 + toast。
3. 验证：重跑一份报告，新报告出现 + 自动 judge。

**期 2（闭环）**：
1. judge prompt + 解析 action_items + archive 存（+ prev_scores）。
2. improvements.py 聚合 + apply/dismiss（复用 keywords CRUD）。
3. 后端 improvements 3 端点。
4. 前端改进队列 dialog（3 tab + apply/dismiss/copy + 成效行）。
5. 验证：低 coverage 报告 -> 改进队列出 keyword_add -> 应用 -> 重跑 -> 新 judge coverage 升。

## 7. 边界 / 风险

- **不自动改 prompt**：prompt 类只复制建议，人工改源码（避免 LLM 误改 prompt 致质量退化）。
- **apply 二次确认**：关键词 apply 前弹确认（防误加/误删）。
- **LLM 建议可能错**：聚合 count（多份报告才显眼）+ 人工 gate 兜底；severity 由 LLM 给但仅作排序提示。
- **向后兼容**：action_items / prev_scores 是 judgment entry 的新字段，旧 entry 无则聚合时跳过，不破坏既有 archive。
- **重跑 llm_model**：retry 修好后重跑才真正用同模型；此前重跑会把 deepseek 报告用 glm 重跑（已存在的坑）。
- **不碰 deep segment query 自动改**：evidence 低分只提示「加大 tavily_results」或人工改 query，不做自动 query 生成（风险高、收益不确定）。

## 8. 与 sector-strategy 原则的关系

闭环的「应用关键词/核心公司」落在 sector-strategy 的共享数据层（`sector_keywords.json` / core_companies），改的是数据不是分析逻辑 -> 符合「数据驱动、分析逻辑分离」。prompt 类改的是分析侧（harness synthesis / report），属 sector-strategy 的分析逻辑层，人工 gate 改。闭环本身是元层（评判 -> 改进），不调 gather、不做板块采集。

相关：[[report-judge-spec]]（前置已实现）、[[sector-data-shared-layer]]（应用落点）、[[harness-kimi-timeout]]（重跑超时沿用既有路径分级）。
