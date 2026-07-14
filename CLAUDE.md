# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Critical: always use the project venv

Run anything in this repo with `/opt/stocks/.venv/bin/python` (or `source .venv/bin/activate` first). **Never use system `python3`.** System Python has `tavily-python` 0.1.9, which doesn't even export `TavilyClient` and fails with `ImportError`. The project venv has `tavily-python` 0.7.26+. The frontend web app hardcodes the venv interpreter (`STOCKS_VENV_PYTHON` in `/home/smallsite-vue/backend/src/api/admin-stocks.ts`), so anything that breaks under the venv breaks production.

## Common commands

```bash
# A-share deterministic pipeline (chain_graph → collectors → discovery → scoring → llm)
/opt/stocks/.venv/bin/python -m chain_agent.agent optical_module
/opt/stocks/.venv/bin/python -m chain_agent.agent optical_module --days 14 --top-n 20 --llm --out report.md
/opt/stocks/.venv/bin/python -m chain_agent.agent --sectors optical_module,pcb,storage --max-workers 3   # batch
/opt/stocks/.venv/bin/python -m chain_agent.agent ai_server --json --out ai_server.json

# A-share LLM-driven deep pipeline (decompose → per-segment search → bottleneck → score)
/opt/stocks/.venv/bin/python -m skills.deep-analyze --chain mlcc --days 14 --top-n 8 --out report.md
/opt/stocks/.venv/bin/python -m skills.deep-analyze --stock 300308          # 6-digit code or company name
/opt/stocks/.venv/bin/python -m skills.deep-analyze --stock 中际旭创 --out verdict.md

# Daily sector resonance (板块共振自进化)
/opt/stocks/.venv/bin/python -m skills.daily_resonance                    # 运行今日共振（处理昨日新闻）
/opt/stocks/.venv/bin/python -m skills.daily_resonance --date 2026-07-13  # 指定日期（回测用）
/opt/stocks/.venv/bin/python -m skills.daily_resonance --json             # JSON输出
/opt/stocks/.venv/bin/python -m skills.daily_resonance --no-evolve        # 跳过自进化
/opt/stocks/.venv/bin/python -m skills.daily_resonance --no-llm           # 模板报告（无LLM调用）
/opt/stocks/.venv/bin/python scripts/backtest_resonance.py                # 全量回测
/opt/stocks/.venv/bin/python scripts/backtest_resonance.py --days 30      # 最近30天回测
/opt/stocks/.venv/bin/python scripts/backtest_resonance.py --json         # JSON格式回测报告

# A-share valuation lens (稀缺+前瞻+供需 估值排序，PE 仅作方向约束)
/opt/stocks/.venv/bin/python -m skills.valuation-lens --chain optical-module --top-n 8 --out lens.md
/opt/stocks/.venv/bin/python -m skills.valuation-lens --codes 300308,300502,688498 --out lens.md
/opt/stocks/.venv/bin/python -m skills.valuation-lens --stock 300308 --out verdict.md

# US mirrors (same flags)
/opt/stocks/.venv/bin/python -m us_chain_agent.agent semiconductors --llm
/opt/stocks/.venv/bin/python -m skills.us-deep-analyze --chain ai_cloud --out us.md
/opt/stocks/.venv/bin/python -m skills.us-deep-analyze --stock AAPL

# Refresh the static A-share / US stock lists from akshare / finnhub
/opt/stocks/.venv/bin/python scripts/refresh_stock_list.py
/opt/stocks/.venv/bin/python -m us_chain_agent.scripts.refresh_us_stock_list
```

There is **no test suite and no linter config** in this repo — don't invent `pytest`/`lint` invocations. Verify changes by running the relevant module above (a `--json` run is the fastest signal). Reports land in `output/` (gitignored); Tavily raw results dump under `output/tavily/`.

## Architecture: two pipeline stacks, each with a US mirror

This is the part the README understates. There are **six runnable agent entry points**, not one, and the website picks among them:

| Entry module | Market | Style | What it does |
|---|---|---|---|
| `chain_agent.agent` | A-share | Deterministic | 6-layer pipeline: `chain_graph.expand_chain` → `collectors.orchestrator.collect_all` → `discovery.discover_candidates` → `stock_data.enrich_candidates` → `scoring.integrator.score_candidates` → optional `llm_synthesize` |
| `skills.deep-analyze` (`__main__`→`analyzer`) | A-share | LLM-driven | `decompose_chain` (LLM splits chain into segments, `_llm_call_with_continue` 防 max_tokens 截断) → `search_all_segments` (4 queries/segment + akshare leader news, concurrent, evidence 编 `T1/A1`, 用 `snippet`) → `identify_bottlenecks` (LLM, reasoning 强制 `evidence:[IDs]` 起头) → `score_candidates` (LLM 3-dim 供需/国产替代/业绩兑现, 分批 + 整批失败逐只重试, 注入 `background_prior`) → `_upsert_deep_archive` 写回 `deep_key_facts`/`deep_score_history`（total≥55）→ 确定性 report. `analyze_stock` wraps `analyze_chain` with `force_include_codes` + a customer-structure search (`C1/C2` evidence) + verdict LLM. |
| `skills.valuation-lens` (`__main__`→`analyzer`) | A-share | LLM-driven | 估值镜头：以「稀缺+前瞻+供需」三维度重估，PE 仅作方向约束（`_compute_valuation_score` 确定性执行：**PE verdict 基于当批 PE 分布算**（非 LLM）+ **软阈值连续模型**；三高标的 PE 不参与）。`--chain`（**自动发现候选**：板块搜索 + `StockDetector` + 财联社热度（`HERMES_NEWS_JSON`）+ **per-stock 知识档案**（`output/valuation_stock_archive.json`：evidence_pool/key_facts/score_history，val≥60 积累；**24h 内跳过 Tavily 复用档案+财联社 per-stock 实时**，prior 注入 LLM 增量更新），不读 `overflow_config`、无任何手填参数）/`--codes`/`--stock` 三入口；S/F/D evidence-id 强制引用；复用 `chain_agent/llm/parse.py`。**无 US 镜像**。 |
| `us_chain_agent.agent` | US | Deterministic | Mirror of `chain_agent` over `data/us_*` files; collectors use Finnhub + Wikipedia instead of akshare/财联社 |
| `skills.us-deep-analyze` | US | LLM-driven | Mirror of `skills.deep-analyze` |
| `skills.ce-value` (`__main__`->`analyzer`) | A-share | LLM-driven | 431 中国特色价值投资：宏观->市场->行业->公司 层层收敛 + 三高(高增长/高利润/高围墙)筛选 + 卡脖子抓手。`run_macro_briefing`（财联社政策+Tavily 全球/流动性+akshare 宏观序列 CPI/社融/M2/PMI）-> `run_market_briefing`（指数+北向+融资融券+财联社情绪，指数 flaky 时降级）-> 选板块（`--chain` 用户指定 / 无则 `sector_picker` LLM 从 ecosystem 选 1-N 个）-> 公司层 **in-process 调 `skills.harness.orchestrator.run_harness_chain`**（三视角，不再包 subprocess）-> `financials.get_financials_batch`（akshare `stock_financial_analysis_indicator` 拉毛利率/净利率/ROE/增速）-> `three_high.score_batch`（ramp 软阈值，高围墙复用 val 稀缺+deep 国产替代，缺数据降级）-> 卡脖子来自 harness 透传的 `deep_bottlenecks`。`--max-sectors` 控自动选板块上限。**无 US 镜像**。 |

**`chain_agent`/`us_chain_agent` is the shared core.** Both `skills/*-deep-analyze` import `chain_agent.config`, `chain_agent.collectors.*` (Tavily/Zhipu/akshare/cache), `chain_agent.discovery.stock_detector`, `chain_agent.scoring.quotes`, and `chain_agent.llm.client`. So changes to `chain_agent/` ripple into the deep-analyze skills — don't treat them as independent.

**The three scoring styles are not interchangeable:**
- `chain_agent` scoring is rule-based, 0–100, with roles `leader`/`second_tier`/`tech_option`/`discovery` (overflow saturation + tech-option expected value + heuristic + a 4-dim enrich of 资金面/龙虎榜/研报/解禁 from `collectors/stock_data.py`).
- `deep-analyze` scoring is LLM-judged 3-dim (供需30/国产替代30/业绩兑现40), gated by a bottleneck analysis, and uses an **evidence-id system** (`T1/T2…` web, `A1/A2…` akshare, `C1/C2…` customer) that downstream prompts force the LLM to cite; PE 阶梯扣分硬约束（>500 上限10…）.
- `valuation-lens` scoring is **hybrid**: LLM judges 3-dim (稀缺/前瞻/供需, 0-100 each), then code deterministically 合成 `valuation_score = 0.35·稀缺 + 0.30·前瞻 + 0.25·供需 + PE_adj`（PE verdict 基于当批 PE 分布算，非 LLM；软阈值连续模型；**三高标的 PE 不参与**）。LLM 给维度分，代码算最终分 + PE 方向。

### Frontend integration (the production caller)

The website at `/home/smallsite-vue` (separate repo, not under `/opt/stocks`) drives this repo via `backend/src/api/admin-stocks.ts`:
- A SQLite task queue + `setInterval` worker (10s tick, concurrency 1, PID-liveness recovery) `spawn`s `STOCKS_VENV_PYTHON -m <module> <args>`.
- `task_type` selects the module: `chain` (default) → `chain_agent.agent` / `us_chain_agent.agent`; `deep_chain` → `skills.deep-analyze` / `skills.us-deep-analyze` with `--chain`; `harness`/`harness_stock` → `skills.harness`; `ce_value` → `skills.ce-value`; `stock` → same skill with `--stock`; `valuation` → `skills.valuation-lens` with `--chain` (**A-share only — US+valuation/harness/ce_value blocked at `POST /tasks`**). `market: 'us'` swaps in the US modules (except `valuation`/`harness`/`ce_value`, which have no US mirror).
- At spawn it injects env vars: `SERENITY_LENS`, `SUPPLY_DEMAND_LENS` (Serenity/供需 lenses), `DECOMPOSE_INJECT_KEYWORDS` (the `inject_keywords` task flag), and selects LLM model (`deepseek` default vs `glm`/`kimi`).
- The admin API also **reads and writes** `data/sector_ecosystem.json`, `data/sector_overflow_config.json` (and US variants) directly — so uncommitted edits to those JSON files (often seen in `git status`) may have come from the web UI, not a local editor. Invalidate-by-mtime caches in the TS layer mean the running Node process may need a reload to pick up JSON changes.

When debugging "a task ran wrong from the website," reconstruct the exact command from `buildAgentArgs` in that file — it's the source of truth for flags.

## Config: `chain_agent/config.py` is the single source (README is stale)

Trust `config.py`, not the README, for current defaults. Notable points:

- **Sector naming convention**: `sector_ecosystem.json` keys use underscores (`optical_module`); `sector_overflow_config.json` keys use hyphens (`optical-module`). `config.to_hyphen()` / `to_under()` convert. Chain inputs may be Chinese or English keys.
- **`QUOTE_PROVIDER`** default is `easyquotation` (README says akshare). `AkshareQuoteProvider` is the alternate. `get_quote_provider()` is the DI seam; scoring layers take a `quote_provider`/`quotes` arg, never a global.
- **LLM env vars are namespaced**: `CHAIN_AGENT_LLM_PROVIDER` (default `openai` -> DeepSeek-v4-flash; `auto` = Anthropic→OpenAI(DeepSeek)→template fallback) and `CHAIN_AGENT_LLM_MAX_TOKENS` (default `16384`). The bare `LLM_PROVIDER`/`LLM_MAX_TOKENS` names in the README do **not** work.
- **LLM env vars are namespaced**: `CHAIN_AGENT_LLM_PROVIDER` (default `anthropic` → Volcengine ark Anthropic 兼容 endpoint，统一接入多模型；备选 `auto`/`openai`/`kimi`) and `CHAIN_AGENT_LLM_MAX_TOKENS` (default `16384`). 宿主环境（Claude Code）通过 settings.json 注入 `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY`，项目默认指向 Volcengine，**不做 force-override**（与旧版不同）。
- **The "Anthropic" client points at Volcengine ark**: `ANTHROPIC_BASE_URL` 默认 `https://ark.cn-beijing.volces.com/api/plan`，`ANTHROPIC_MODEL` 默认 `deepseek-v4-flash`（推理模型，思考走 reasoning_content，答案在 message.content）。**不设默认 API key**（必须来自 env），旧版的 Zhipu key 已移除。
- **OpenAI-compatible** 是备选 provider：`CHAIN_AGENT_LLM_PROVIDER=openai` 时走 `OPENAI_BASE_URL=https://api.deepseek.com`，`OPENAI_MODEL=deepseek-v4-flash`。Kimi 仍可选（设 `CHAIN_AGENT_LLM_PROVIDER=kimi` + Moonshot base + `KIMI_API_KEY` + `kimi-k2.6`）。网站 task_type 默认 `llm_model='deepseek'`。
- **API keys are baked in as defaults** (3× Tavily dev keys, Zhipu key, DeepSeek key, Kimi key) — this is the "self-contained, runs anywhere" design. Env vars (`TAVILY_API_KEYS` comma-separated, `ZHIPU_API_KEY`, `OPENAI_API_KEY`/`DEEPSEEK_API_KEY`/`KIMI_API_KEY`, `FINNHUB_API_KEY`) override them. Edit `config.py` to rotate defaults.
- **Proxy handling differs by market.** `chain_agent/config.py` calls `clear_proxy_env()` on import (A-share akshare/东财 must go direct; respects `EM_MIN_INTERVAL`/`EM_TIMEOUT` for 东财 rate limiting). `us_chain_agent/config.py` snapshots proxy env *before* importing `chain_agent.config`, then restores it afterward — US sources (Finnhub/Wikipedia/Tavily) need the proxy. Keep this ordering intact if you touch either config.

### Search provider chain (shared by both stacks)

`TavilySearch` (multi-key rotation + failover) is primary; `ZhipuSearch` (BigModel `web_search_pro`) is the paid fallback. Both implement `search_with_ai_summary(query, max_results)` and `search_industry_news(...)`. Results are cached by query in `collectors/search_cache.py` (provider-agnostic — Tavily/Zhipu results are interchangeable in cache). The orchestrator runs **three tracks in parallel**: supply (Tavily/Zhipu), demand-primary (财联社 via hermes), demand-secondary (akshare); tracks fail independently and dedup into `combined_text`.

### External dependency the README doesn't mention

`config.HERMES_NEWS_JSON` reads `/root/.hermes/data/investment-research/news/latest_news.json` for 财联社 news (maintained by a separate hermes cron). If hermes is absent or stale, the orchestrator silently degrades the demand track to akshare-only. This is the one real "external directory dependency" despite the README's "no external dependencies" claim.

## Data files (`data/`, all tracked)

- `sector_ecosystem.json` — A-share chain graph. Top level is `{metadata, <sector_key>: {...}}` (28 sectors). Each sector has `upstream`/`downstream`/`related`/`include_sectors`/`tier`/`key_products`/`technologies`. The frontend lists sectors as `Object.keys(ecosystem).filter(k => k !== 'metadata')`.
- `us_sector_ecosystem.json` — US equivalent.
- `sector_overflow_config.json` (hyphen keys) / `us_sector_overflow_config.json` — leader/second-tier/tech-option stock configs per sector; `chain_agent.agent._get_sector_leaders` reads `leaders[].code`.
- `a_stock_list.json` (~5506 stocks) / `us_stock_list.json` — name↔code map; `StockDetector` and stock-mode resolution read these.
- `sector_keywords.json` — per-sector keywords injected into decompose prompts (gated by `DECOMPOSE_INJECT_KEYWORDS`).
- `serenity_methodology.md` — market-neutral investment framework (4 mental models + 12 heuristics). A-share prompts may cite the **framework only**; US-ticker analysis is intentionally decoupled into `.claude/skills/serenity-perspective/`. Do not leak US tickers into A-share reports.
- `data_utilization_plan.md` — dated efficiency audit with `file:line` references; consult it before refactoring data flow (some items there are already fixed — verify against current code).

## Local Claude skills (`.claude/skills/`, not shipped)

- `serenity-perspective/` — US-stock analysis via Serenity methodology; has `scripts/analyze_dataset.py` and a `serenity.csv` source. Separate channel from the A-share pipeline.
- `integrate-website/` — workflows for wiring changes into the `/home/smallsite-vue` frontend.

## Daily resonance: cron & outputs

The daily resonance system (`skills/daily_resonance/`) processes 财联社 news each trading day at 16:30 (T日), producing a sector resonance ranking with self-evolving weights.

### cron (交易日 16:30)

```bash
# crontab -e
30 16 * * 1-5 cd /opt/stocks && .venv/bin/python -m skills.daily_resonance >> output/daily_resonance_cron.log 2>&1
```

### Output directory: `skills/daily_resonance/output/`

| File | Description |
|------|-------------|
| `resonance_{date}.json` | Structured TOP10 resonance data |
| `resonance_{date}.md` | Daily briefing (Markdown) |
| `evolution_state.json` | Self-evolution state (weights, history) |
| `backtest_summary.json` | Backtesting results (from `scripts/backtest_resonance.py`) |

### Architecture

```
财联社 news → Agent 1 (keyword match + LLM fallback) → sector events
  → Agent 2 (deterministic resonance: density/sentiment/chain/diversity/importance)
  → Agent 3 (LLM report or template)
  → Evolution (T+1 feedback → Bayesian weight update → convergence)
```

Key points:
- **Agent 1 LLM兜底**: keyword-unmatched news (≤100/day) get LLM batch classification via `chain_agent.llm.client.get_llm_client().synthesize()`.
- **Self-evolution**: weights start at `[0.25, 0.25, 0.20, 0.15, 0.15]`, converge after 14+ days with Δw < 0.01 and avg accuracy ≥ 55%. Regime-change detection resets if 7 consecutive days < 50% accuracy.
- **Cost**: ~¥0.10/day (Agent 1 LLM兜底 ¥0.02 + Agent 3 report ¥0.08).
- **No chain_agent modifications**: reads only; all data from `data/` JSON files and hermes news.

## Conventions to preserve

- **Data-driven, no hardcoding**: sector graphs, leader lists, keywords live in JSON, not Python.
- **Graceful degradation**: Tavily→Zhipu→none, Anthropic→Kimi→template, easyquotation→akshare, 财联社→akshare. Every layer must still produce a (possibly degraded) report. `deep-analyze` sets `data_quality: "degraded"` when all sources fail.
- **LLM-output parsing is defensive and now shared**: `chain_agent/llm/parse.py::json_from_llm` / `split_text_and_json` do char-level brace matching that tolerates markdown fences, multiple JSON blocks, and top-level arrays. This is the canonical copy — all three LLM skills (`skills/valuation-lens`, `skills/deep-analyze`, `skills/us-deep-analyze`) use it directly (the US mirror was the last to migrate off its local copy). Content-snippet extraction is likewise shared via `chain_agent/collectors/snippet.py::snippet` (里程碑关键词锚定，跳过导航 boilerplate — all three use it instead of `content[:N]`, which slices off mid-article milestones). Scoring is batched (`DEEP_ANALYZE_SCORE_BATCH`, default 8; `VALUATION_LENS_BATCH`, default 4), auto-continues on `max_tokens` truncation, and retries singly on batch parse failure.
- **Search-provider logs go to stderr, not stdout**: `tavily_search.py` and `zhipu_search.py` print key-rotation/progress/error logs with `file=sys.stderr`. Skills emit `--json` on stdout — logging there would corrupt JSON parsing. The frontend merges stdout+stderr into one log buffer, so stderr is safe and still visible. Keep this when adding new collectors.
- **Cross-skill knowledge archive** (`output/valuation_stock_archive.json`, shared via `chain_agent/knowledge/archive.py`): valuation-lens writes `key_facts`/`evidence_pool`/`score_history` (稀缺/前瞻/供需 dims, val≥60 入档), deep-analyze writes `deep_key_facts`/`deep_score_history` (供需/国产替代/业绩兑现 dims, total≥55 入档). Dims coexist — writes are merge-upserts that preserve the other skill's keys. **Read-back**: deep-analyze 评分前读 `background_prior = {val_lens: key_facts, deep: deep_key_facts}`（读 val-lens 的 + 自己上次的结论；**只注理由不注分数**，防锚定；`prev_score` 只在输出作"前次 X→本次 Y"走势展示，不进 LLM 输入）。val-lens 24h 内复用 `evidence_pool` 跳过 Tavily，财联社始终实时并标 `[新增]`（publish_time > last_run）。Age-culling 取 `max(last_run, deep_last_run)` so deep-only entries aren't dropped. **当前边界**：24h-skip/evidence_pool 复用仅 val-lens（deep 每次重搜）；us-deep-analyze 无档案；chain_agent 不积累；板块档案（`output/valuation_sector_archive.json`）仅 val-lens。`valuation-lens/analyzer.py` is split into `archive.py`/`search.py`/`scoring.py` + a thin orchestrator.
