"""cycle-lens LLM prompts（SPEC §8）：三问 + 8步闭环定位 + 警惕信号 + 终极判断。

确定性分解（decompose.py）已给出分类/分位/峰值/上修下修，LLM 只做定性。
"""

import json

CYCLE_SYSTEM = """你是 A 股周期研究员，用"市场自我调节机制"框架（股价 = EPS × PE）做定性判断。

框架：
- 股价 = EPS × PE。三种上涨：A 泡沫型(EPS↑+PE↑↑) / B 业绩型(EPS↑↑+PE稳/降) / C 周期陷阱(低PE但EPS峰值)。
- 8步闭环：1军备竞赛/紧缺 -> 2EPS上修 -> 3股价涨 -> 4PE被压 -> 5质疑周期/需求 -> 6回撤 -> 7降温 -> 8待下一轮业绩验证。
- 正负反馈：正(需求强->盈利->EPS上修->涨)；负(涨高->怕见顶->PE受压回调)。
- 终极：风险不在"太贵"而在"EPS是否周期峰值"；低PE对周期股双刃剑；警惕信号=EPS不再上修但PE被硬拔。

严格输出 JSON：
{"profitability": "看盈利判断(高利润可持续? 2-3句)",
 "demand": "看需求判断(CapEx放缓? 2-3句)",
 "valuation": "看估值判断(压PE还是拔PE? 2-3句)",
 "cycle_stage": "8步闭环当前步号(1-8) + 一句话说明",
 "warning": "警惕信号(EPS不上修但PE硬拔? 已出现/未出现/观察中 + 理由)",
 "verdict": "终极判断(业绩驱动? 周期峰值风险? 2-3句)"}
不要包裹代码块，直接输出 JSON。"""

CYCLE_USER_TEMPLATE = """# 标的：{name}（{code}）
{sector_keywords}

# 确定性分解（代码算，勿重复）
- 分类：{classification}
- EPS 贡献（近N季涨跌）：{eps_contrib}
- PE 贡献（近N季涨跌）：{pe_contrib}
- PE 历史分位：{pe_percentile}（<0.3 低位 / 0.3-0.7 中位 / >0.7 高位）
- EPS 峰值：{eps_at_peak}
- 前瞻 PE：{forward_pe}（基于研报 predict_EPS；null=无研报，用 TTM PE）
- 当前 TTM EPS：{current_ttm_eps} | 当前价：{current_price} | 当前 PE：{current_pe}
- predict_EPS 上修/下修：{revision}（prev={prev} -> curr={curr}）

# TTM EPS 序列（近8季）
{ttm_series}

# PE 序列（近8季）
{pe_series}

# 新闻 + 研报
{news_research}

请按框架输出 JSON（profitability/demand/valuation/cycle_stage/warning/verdict）。"""


def build_user(name: str, code: str, decomp: dict, revision: dict,
               data: dict, sector_keywords: str = "") -> str:
    """拼 CYCLE_USER_TEMPLATE。"""
    ttm_series = "\n".join(
        f"- {t['date']}: {t['ttm']:.4f}" if t.get("ttm") else f"- {t['date']}: None"
        for t in decomp.get("ttm_eps_series", [])
    )
    pe_series = "\n".join(
        f"- {p['date']}: {p['pe']:.2f}" if p.get("pe") else f"- {p['date']}: None"
        for p in decomp.get("pe_series", [])
    )
    news_research = (data.get("research_text") or "") + "\n" + (data.get("news_text") or "")
    sk = f"\n# 板块关键词（锚定板块边界）\n{sector_keywords}\n" if sector_keywords else ""

    def _fmt_pct(v):
        return f"{v*100:+.1f}%" if isinstance(v, (int, float)) else "N/A"

    def _fmt_pe(v):
        return f"{v:.2f}x" if isinstance(v, (int, float)) and v > 0 else "N/A"

    def _fmt_pctile(v):
        return f"{v*100:.0f}%" if isinstance(v, (int, float)) else "N/A"

    return CYCLE_USER_TEMPLATE.format(
        name=name, code=code, sector_keywords=sk,
        classification=decomp.get("classification", "N/A"),
        eps_contrib=_fmt_pct(decomp.get("eps_contrib")),
        pe_contrib=_fmt_pct(decomp.get("pe_contrib")),
        pe_percentile=_fmt_pctile(decomp.get("pe_percentile")),
        eps_at_peak=decomp.get("eps_at_peak", "N/A"),
        forward_pe=_fmt_pe(decomp.get("forward_pe")),
        current_ttm_eps=f"{decomp['current_ttm_eps']:.4f}" if decomp.get("current_ttm_eps") else "N/A",
        current_price=f"{decomp['current_price']:.2f}" if decomp.get("current_price") else "N/A",
        current_pe=_fmt_pe(data.get("current_pe")),
        revision=revision.get("revision", "N/A"),
        prev=revision.get("prev") if revision.get("prev") is not None else "N/A",
        curr=revision.get("curr") if revision.get("curr") is not None else "N/A",
        ttm_series=ttm_series or "N/A",
        pe_series=pe_series or "N/A",
        news_research=news_research[:8000] or "N/A",
    )
