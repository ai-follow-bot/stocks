# FOLLOWUP — 待决策问题（2026-07-08 讨论记录）

明天公司继续聊。以下是待决策点 + 上下文。

## 已完成（本 session，均已 push main）

- **harness skill**（`skills/harness/` 三视角交叉验证：chain_agent + deep-analyze + valuation-lens）+ 前端集成（`admin-stocks.ts` TaskType/buildAgentArgs + `StocksTasks.vue` radio）
- **valuation-lens 9 条复盘修复**（B1-B5 / T1-T4）+ B2 stock 板块归一化（identify prompt 给板块列表 + `_canonical_sector_key` name 反查优先英文 key）+ unclassified 档案积累
- **双 key 清理**（删 ecosystem/overflow 的 `光模块` 空壳 duplicate）+ 中英对照表（`data/sector_key_map.json`）+ `_canonical_sector_key` 优先查对照表
- **#5 档案 sector 迁移工具**（`scripts/migrate_archive_sectors.py`，dry-run/`--apply`）+ 对 `valuation_stock_archive.json` 跑过 `--apply`（5 条旧脏 `光通信产业链`/`激光产业链` → `optical_module`）

分支：`feat/harness` / `fix/valuation-lens-review` / `fix/archive-migrate` 均已 merge main。

---

## 待决策：#4 Tavily shared days + evidence 沉淀

### 问题（用户 2026-07-08 提出）

Tavily 搜索数据不沉淀，**年初/年尾 evidence 差异大 → 评分波动**：

- valuation-lens `evidence_pool`：`_EVIDENCE_MAX_AGE_DAYS=30`（30 天淘汰）
- `search_cache`：12h（query 级 Tavily 缓存）
- deep-analyze：每次重搜，无 evidence 沉淀
- chain_agent：`search_cache` 12h

年初跑的 Tavily evidence，30 天后丢；年尾跑 evidence 是近 30 天。年初 vs 年尾 evidence 是"替换"不是"累积"，评分基于短期 evidence → 波动。**稀缺/前瞻（慢变量）evidence 本该长期有效，却跟供需（实时）一样 30 天淘汰，这是浪费**。

### #4 原 days 时间窗问题（Tavily 三路径都无效）

- `chain_agent.search_industry_news`：签名有 `days` 但实现没传 `client.search()`（死参）
- `val/deep.search_with_ai_summary`：根本无 `days` 参数
- 所以 `--days` 只管财联社/akshare，Tavily 全年份（query 带 `"2026"`）
- report 标注：只有 val 改诚实了（B1，"财联回看近 N 天，Tavily 为当年全网搜索"），chain/deep 还标"数据窗口:近 N 天"（对 Tavily 不准）
- 修法：`search_industry_news` + `search_with_ai_summary` 加 `days=days` 传 `client.search()`。代价：recall 降（时间窗剔除旧文）

### 沉淀方案（用户提出，待定）

1. **evidence_pool 分层淘汰**（val `archive.py`）：
   - S/F（稀缺/前瞻，慢变量）：30 天 → 180 天或永久
   - D（供需，实时）：保持 30 天
   - 实现：`_merge_pool` 按 dim 用不同 max_age
2. **Tavily query 长期 cache**（`search_cache.py`）：12h → 30/90 天
3. **score_history 延长**：10 → 30/50（评分走势长期沉淀）

### 待用户定

1. **范围**：只 valuation-lens？三路径（chain/deep 也加沉淀）？
2. **程度**：S/F evidence 180 天？永久？Tavily cache 30 天？90 天？
3. **分层**：S/F 长期 D 短期（推荐）？全长期（简单但 D 可能用过时供需）？

### 推荐

**只 val**（已有 evidence_pool，改动最小）+ **S/F 永久 D 30 天**（慢变量不过时）+ **Tavily cache 90 天**。这样年初/年尾 S/F evidence 累积，D 实时，评分稳。

---

## 其它暂缓

- #2 双 key 清理：✅ 已做
- #4 Tavily shared days + 沉淀：⏳ 待定（见上）
- #5 迁移工具：✅ 已做
