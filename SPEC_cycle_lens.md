# SPEC: skills.cycle-lens（业绩-估值周期镜头）

> **状态**：已设计待开发（2026-07-10，2026-07-12 对齐 sector-strategy 原则）。本 spec 自包含，新会话可直接据此实施。
> **特性**：板块模式调 `chain_agent.sector_data.gather()` 共享数据层（板块->关键词->核心公司+搜索+数据），只写周期分析逻辑；不调 harness/deep-analyze/valuation-lens/chain_agent.agent 的分析逻辑。遵循 `.claude/skills/sector-strategy` 原则。

## 1. 目标

把"市场自我调节机制"框架做成 A 股个股/板块分析 skill：用 **股价 = EPS × PE** 分解涨跌驱动，判**业绩型/泡沫型/周期陷阱**，定位周期位置 + 自我调节阶段，给出警惕信号。

框架来源（海力士/HBM 示例）：
- 股价 = EPS × PE，三种上涨：A 泡沫型(EPS↑+PE↑↑) / B 业绩型(EPS↑↑+PE稳/降) / C 周期陷阱(低PE但E峰值)
- 市场给"周期股估值 × 成长股利润" -> 前瞻PE长期低位 = 市场自我降温（不让估值无限膨胀）
- 8步闭环：军备竞赛/紧缺 -> EPS上修 -> 股价涨 -> PE被压 -> 质疑周期/需求 -> 回撤 -> 降温 -> 待下一轮业绩验证
- 正负反馈并存：正(需求强->盈利->EPS上修->涨)；负(涨高->怕见顶->PE受压回调)
- 终极判断：风险不在"太贵"而在"EPS是否周期峰值"；低PE对周期股双刃剑；警惕信号=EPS不再上修但PE被硬拔
- 三问：看盈利(高利润可持续?) / 看需求(CapEx放缓?) / 看估值(压PE还是拔PE?)

## 2. 与现有 skill 的边界（遵循 sector-strategy 原则）

- **板块模式（--chain）调 `chain_agent.sector_data.gather(sector)`** 拿共享候选池 + 板块关键词 + board evidence + 行情，在该池上跑周期分析（与 chain/deep/val 同池，分数可进 harness align）。单股模式（--stock）直采单只数据（与 deep/val 的 analyze_stock 一致，不走 gather）。
- **不调** harness / skills.deep-analyze / skills.valuation-lens / chain_agent.agent 的分析逻辑。
- **独立 archive** `output/cycle_archive.json`（不写 valuation_stock_archive.json）。
- 复用基础设施：`chain_agent.sector_data`（板块模式数据）、`chain_agent.collectors`（Tavily/Zhipu/财联社/研报/quotes/akshare，单股模式 + 周期专属数据）、`chain_agent.llm.client` + `chain_agent.llm.parse`、`chain_agent.config`。
- 风格同 valuation-lens：确定性合成（分解/分位/上修检测都代码算）+ LLM 只做定性（三问/周期定位）。
- 周期专属数据（EPS/PE 历史序列、predict_EPS）= 分析侧 enrich（像 chain 的资金面），不进共享层。

## 3. 数据源（已全部验证可行）

| 要素 | 源 | 字段/函数 |
|---|---|---|
| 股价历史 | `akshare.stock_zh_a_hist(symbol, period="daily", adjust="qfq")` | 收盘价，近 8 季度 |
| EPS 历史 | `akshare.stock_financial_analysis_indicator(symbol, start_year)` | `摊薄每股收益(元)`（季度累计值） |
| 当前 PE / 市值 | `chain_agent.scoring.quotes.get_quote_provider().get_quotes([code])` | pe, market_cap |
| 前瞻 EPS | `chain_agent.collectors.stock_data.eastmoney_research_reports(code)` | `predict_this_year_eps`, `predict_next_year_eps` |
| 新闻/研报 | Tavily(`TavilySearch`) + 财联社(`config.HERMES_NEWS_JSON`) + 研报文本 | 同 deep-analyze/val-lens |
| 板块核心公司 | `chain_agent.discovery.stock_detector.load_core_companies(sector)` | [{code,name,segment}]（--chain 模式用） |

注：EPS 上修/下修历史无现成源（研报只存最新 predict_EPS）-> 靠 archive 累积（见 §6）。

## 4. 模块结构

```
skills/cycle-lens/
  __init__.py
  __main__.py     # CLI: --stock <code/name> | --chain <sector>  [--days N] [--out F] [--json]
  data.py         # 采集（§5）
  decompose.py    # EPS×PE 分解 + 分类 + 分位 + 峰值判定（确定性，§7）
  archive.py      # predict_EPS 累积 + 上修/下修（§6，独立 cycle_archive.json）
  analyzer.py     # 编排：data -> decompose -> archive -> LLM(三问+8步) -> report
  prompts.py      # LLM prompts（三问 / 8步闭环定位 / 警惕信号）
  report.py       # 报告渲染
```

## 5. data.py 采集

```python
def collect(stock_code: str, days: int = 14) -> dict:
    """返回 {price_hist, eps_hist, current_pe, market_cap, predict_eps, news_text, research_text}"""
```
- `price_hist`: stock_zh_a_hist 近 8 季度末收盘价（按季度末日期对齐）。
- `eps_hist`: financial_analysis_indicator `摊薄每股收益` 近 8 季度（注意是累计值）。
- `current_pe`, `market_cap`: quotes。
- `predict_eps`: 研报 predict_this_year_eps + predict_next_year_eps（取最新研报）。
- `news_text`: 财联社近 days 天 + Tavily 搜 `<name> 业绩 EPS CapEx 周期` 等 2-3 query。
- `research_text`: 研报 title/rating/predict_EPS 拼接。
- 全部 try/except，失败返回空/None，不阻塞（graceful degradation）。

## 6. archive.py（独立 cycle_archive.json）

```python
# output/cycle_archive.json 结构：{code: {name, predict_eps_history: [{ts, predict_this, predict_next}], last_run, runs}}
def upsert_predict_eps(code: str, name: str, predict_this: float, predict_next: float) -> dict:
    """存本次 predict_EPS + timestamp；对比上次 -> 返回 {revision: 上修/下修/持平/首次, prev, curr}"""
def load_entry(code: str) -> dict
```
- 独立文件 `output/cycle_archive.json`（不碰 valuation_stock_archive.json）。
- 每次跑追加 predict_eps_history（带 timestamp），对比上次 predict_this -> 上修(curr>prev)/下修(curr<prev)/持平/首次(无历史)。
- 累积越多上修/下修趋势越准。

## 7. decompose.py（确定性，核心算法）

```python
def decompose(price_hist: list, eps_hist: list) -> dict:
    """返回 {ttm_eps_series, pe_series, eps_contrib, pe_contrib, classification, pe_percentile, eps_at_peak, forward_pe}"""
```
- **EPS 差分**：A股季度EPS是累计（Q1, H1, 9M, 全年）-> 单季EPS = 本期累计 - 上期累计（年初重置）。
- **TTM EPS** = 近4季累计 EPS（用于 PE）。
- **PE 序列** = 季度末收盘价 / TTM_EPS。
- **分解**：股价变动 = EPS贡献 × PE贡献。近N季：EPS涨跌% × PE涨跌% -> 判 EPS↑? PE↑?
- **分类**：
  - A 泡沫型：EPS↑ 且 PE↑↑（PE 扩张 > EPS 增速）
  - B 业绩型：EPS↑↑ 且 PE 稳/降（股价靠 EPS 推动）
  - C 周期陷阱：PE 低分位 但 单季EPS 同比转负/高位回落
  - 兜底：数据不足标"无法分类"
- **PE 历史分位**：当前 PE 在 PE 序列中的分位（<30% 低、30-70% 中、>70% 高）。
- **EPS 峰值判定**：单季 EPS 近4季趋势（还在升 / 见顶回落 / 同比转负）。
- **前瞻 PE** = current_price / predict_this_year_eps（无研报 -> 用 TTM PE 兜底）。

## 8. analyzer.py 编排

```python
def run_cycle_lens(stock: str = None, sector: str = None, days: int = 14) -> dict:
    # --stock: 单股直采；--chain: 走共享数据层 gather 拿候选池，逐只跑 + 汇总
    if sector:
        from chain_agent import sector_data
        sd = sector_data.gather(sector, days=days, top_n=15)
        pool = sd.get("candidate_pool") or []
        keywords = sd.get("keywords") or []
        results = [run_one(c["code"], c.get("name",""), days, sector_keywords=keywords) for c in pool]
        return {mode:"chain", sector, sector_name: sd.get("sector_name"), results, summary}
    return run_one(stock_code, stock_name, days)

def run_one(code, name, days, sector_keywords=None) -> dict:
    data = data.collect(code, days)  # 周期专属数据（price/EPS/predict_EPS/news）分析侧采集
    decomp = decompose.decompose(data["price_hist"], data["eps_hist"])
    revision = archive.upsert_predict_eps(code, name, data["predict_eps"]["this"], data["predict_eps"]["next"])
    llm = llm_three_questions + cycle_stage(code, name, decomp, data, revision)  # 三问 + 8步定位
    return {code, name, decomp, revision, llm_judgment, data_quality}
```
- LLM 输入：decomp（分类/分位/峰值/前瞻PE）+ revision（上修/下修）+ news/research 文本。
- LLM 输出 JSON：三问(盈利可持续/需求CapEx/估值压PEvs拔PE) + 8步当前步 + 警惕信号(eps不上修但pe硬拔?) + 终极判断。
- 板块汇总：各股分类分布 + 板块周期位置。

## 9. report.py 渲染

```
# {name} 业绩-估值周期镜头
> 框架：股价 = EPS × PE | 分类：业绩型/泡沫型/周期陷阱 | 自我调节机制

## 1. 驱动分解（确定性）
- 分类：B 业绩型（EPS↑↑ + PE 稳）
- EPS 贡献：+85% | PE 贡献：+5%
- PE 历史分位：25%（低位）| 前瞻 PE：8.2x
- EPS 峰值：仍在升（单季同比 +60%）
- 上修/下修：上修（predict_EPS 上次 2.1 -> 本次 3.2）

## 2. 三问判断（LLM）
- 看盈利：HBM 高利润可持续？...
- 看需求：云厂商 CapEx 放缓？...
- 看估值：市场在压 PE 还是拔 PE？...

## 3. 自我调节定位
- 8 步闭环当前：第3步（股价涨 -> PE 被压）
- 正负反馈：...

## 4. 警惕信号 + 终极判断
- 警惕：EPS 不再上修但 PE 开始硬拔（尚未出现）
- 终极：业绩驱动 + 估值受控；风险在 EPS 是否周期峰值，非"太贵"
```
- **不显示股票代码**（与 chain_agent/deep/val/ce-value 一致，只显示名称）；verdict 标题可保留代码作主体标识。

## 10. __main__.py CLI

```bash
python -m skills.cycle-lens --stock 300308 --out cycle.md
python -m skills.cycle-lens --stock 中际旭创 --json
python -m skills.cycle-lens --chain HBM --out cycle.md   # 跑 core_companies
```
- `--stock` / `--chain` 互斥（必选一）。
- `--days`（新闻回看，默认14）、`--out`、`--json`。
- 公司名->代码：复用 `chain_agent.discovery.stock_detector` 或 a_stock_list 反查。

## 11. 前端 + 后端接入

- 后端 `admin-stocks.ts`：
  - `buildAgentArgs` 加 `task_type === 'cycle'` -> `module: 'skills.cycle-lens'`，`--stock` 或 `--chain` + `--days` + `--out`。
  - POST /tasks 校验：A 股 only（US+cycle 拦截）；`--chain` 用 sector，`--stock` 用 stock_input。
  - LlmModel 默认 deepseek（同其它 skill）。
- 前端 `StocksTasks.vue`：加 radio `cycle` "业绩估值镜头"（A 股 only）；onMarketChange reset；任务历史"任务类型"列加映射 `cycle: '业绩估值'`。
- `admin-stocks.ts` 的 `LlmModel` / `TaskType` 类型加 `'cycle'`。
- **改 admin-stocks.ts 后必须 `tsc` + `pm2 restart followbot-backend`**（见 memory backend-rebuild-required）。

## 12. 约定（必须保留）

- 数据驱动、优雅降级（每层 try/except，失败仍出降级报告）、LLM 输出防御解析（复用 `chain_agent.llm.parse.json_from_llm`）。
- 搜索/akshare 日志走 stderr（不污染 stdout --json）。
- 报告不显示候选代码（名称即可）。
- A 股 only（US 镜像后续，不在本期）。
- 默认 LLM = DeepSeek（kimi 已默认关 thinking，见 config.KIMI_THINKING_ENABLED）。

## 13. 验证

```bash
# 单股
/opt/stocks/.venv/bin/python -m skills.cycle-lens --stock 300308 --json
# 板块（HBM/存储/光模块龙头）
/opt/stocks/.venv/bin/python -m skills.cycle-lens --chain HBM --out cycle.md
```
- 确认：分解出 A/B/C 分类 + PE 分位 + 前瞻 PE + 上修/下修 + 三问 + 8步定位。
- 跑两次（不同时间）验证 predict_EPS 累积 + 上修/下修检测（首次标"无基线"）。
- 前端：建 cycle 任务 -> 出报告。

## 14. 开发顺序建议

1. data.py（采集，验证能拿到 price/EPS/predict_EPS）。
2. decompose.py（分解算法，单测 EPS 差分/TTM/分类/分位）。
3. archive.py（predict_EPS 累积 + 上修/下修）。
4. prompts.py + analyzer.py（LLM 三问 + 8步）。
5. report.py + __main__.py（CLI）。
6. 前端 + 后端接入（task_type cycle）+ tsc/pm2 restart/vite build。
7. 端到端验证（单股 + 板块）。

## 15. 参考实现

- 数据采集模式：参考 `skills/valuation-lens/search.py` + `skills/ce-value/financials.py`（akshare 封装 + try/except）。
- archive 模式：参考 `chain_agent/knowledge/archive.py`（load/save/upsert），但用独立文件。
- LLM 调用 + parse：参考 `skills/ce-value/common.py`（`_llm_call_json` + `json_from_llm`）。
- 报告渲染：参考 `skills/ce-value/report.py`（不显示代码、graceful 降级）。
