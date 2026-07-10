"""
LLM 提示词模板
"""

import os


def _lens(name: str, topic: str, blocks: dict[str, str]) -> str:
    """通用视角注入辅助函数。

    读取 `{NAME}_LENS` 环境变量，默认 '1' 开，'0' 关。
    未来新增视角只需：定义 blocks dict + 在对应 system prompt 调用 _lens()。
    """
    env_var = f"{name.upper()}_LENS"
    if os.environ.get(env_var, "1") == "0":
        return ""
    return blocks.get(topic, "")


SERENITY_BLOCKS = {
    "synthesis": """

# Serenity 视角（去美股化方法论，来自 data/serenity_methodology.md）
额外从四个维度审视推荐清单：
1. **上游卡脖子**：优先推荐产业链中稀缺、难替代环节的公司，而非终端品牌
2. **机构资金**：在推荐理由中体现十大流通股东、北向资金、机构调研频次的信号
3. **长线重置**：识别可能被市场重新评级（基本面质变、赛道切换、地位跃迁）的公司
4. **技术颠覆**：在风险点中明确提示新技术对现有龙头的威胁
约束：仅应用框架，禁止在 A 股报告中提及美股代码或美股公司作为对标或推荐。""",
}


def _serenity_lens(topic: str) -> str:
    """从 data/serenity_methodology.md 提炼的视角注入。
    受 SERENITY_LENS 环境变量控制：默认 '1' 开，'0' 关。"""
    return _lens("serenity", topic, SERENITY_BLOCKS)


SUPPLY_DEMAND_BLOCKS = {
    "synthesis": """

# 供需兑现视角（综合报告阶段）
在最终推荐与报告中**必须**以独立章节体现以下内容，不要混入 Serenity 视角或原有推荐理由：
1. **供需 tightness 总览**（独立二级标题 `## 供需 tightness 总览`）：按产业链环节逐一说明当前供需状态（过剩/平衡/紧张/极度紧缺），标注判断依据（产能利用率、库存天数、交货周期、订单 backlog）
2. **业绩兑现节奏表**（独立二级标题 `## 业绩兑现节奏表`）：每只核心推荐标的标注所处环节、预计业绩兑现季度、核心驱动因素
3. **不可替代性标注**（独立二级标题 `## 不可替代性（moat）分析`）：对 moat 深厚的公司，在推荐理由中明确壁垒来源（专利/工艺 know-how/客户认证/设备独占/生态锁定），并评估竞争对手 12-24 个月内复制的难度
4. **中外竞争力对比**（独立二级标题 `## 中外竞争力对比`）：指出国内公司在全球格局中的真实位置（领先/并跑/追赶/替代初期），必须基于数据，禁止编造海外公司市占率或技术参数
5. **风险提示补充**：对供需可能逆转、业绩兑现远（>4 个季度）、moat 浅的标的，在风险点中明确提示"兑现延迟""供需反转"或"竞争加剧"

要求：
- 以上 1-4 项必须作为独立二级标题出现在报告中，不可省略或合并到其他章节
- 供需兑现视角与 Serenity 视角平行呈现，互不替代""",
}


def _supply_demand_lens(topic: str) -> str:
    """供需兑现视角注入。
    受 SUPPLY_DEMAND_LENS 环境变量控制：默认 '1' 开，'0' 关。"""
    return _lens("supply_demand", topic, SUPPLY_DEMAND_BLOCKS)



SYNTHESIS_SYSTEM = """你是一位资深的产业链投资分析师，擅长从产业链结构、新闻舆情、估值数据中挖掘值得投资的标的。

你的输出原则：
1. **诚实**：数据缺失时明确说"数据缺失"，绝不编造数字
2. **分层**：龙头 / 二线 / 技术期权 / 新发现 四类清晰区分
3. **可执行**：每只推荐标的给出「推荐理由 + 风险点 + 建议仓位权重」
4. **产业链视角**：优先推荐在产业链中卡位关键、有议价权的环节
5. **拒绝平庸**：宁可少推荐，不要把候选清单原样输出""" + _serenity_lens("synthesis") + _lens("supply_demand", "synthesis", SUPPLY_DEMAND_BLOCKS)

SYNTHESIS_USER_TEMPLATE = """# 任务
基于以下数据，分析「{sector_name}」产业链，挖掘值得投资的公司。

# 产业链结构
{chain_text}

# 双轨采集数据
## 供给侧（Tavily AI 深度搜索）
- AI 摘要: {tavily_answer}
- 结果数: {tavily_count}
- 完整内容（截断）:
{tavily_content}

## 需求侧（akshare 近 {days} 天新闻）
- 匹配新闻数: {news_count}
- 完整内容（截断）:
{news_content}

# 资金面信号（来自东财/同花顺直连，a-stock-data skill）
## 龙虎榜（近 30 天机构净买 TOP）
{dragon_tiger_text}

## 资金面汇总（近60日涨幅 + 融资融券）
{fund_flow_text}

# 研报评级（东财研报 + 一致预期 EPS）
{research_text}

# 候选标的评分（Top {top_n}）
{scored_text}

# 输出要求
请生成一份 Markdown 格式的投资分析报告，包含：

1. **产业链概览**（200字内）：当前产业景气度、关键催化
2. **核心推荐标的**（5-8 只）：按"龙头 / 二线弹性 / 技术期权 / 新发现"分类，每只给出：
   - 推荐理由（结合产业链卡位 + 数据支撑）
   - 主要风险
   - 建议关注权重（高/中/低）
3. **新发现标的特别提示**：从「动态新发现」清单中筛出真正相关的（剔除误报，如随机6位数字），说明发现路径
4. **产业链卡位总结**：哪些环节最值得配置，哪些已饱和应回避
5. **数据缺口**：列出本次分析中数据不足的部分

报告输出（不要包裹在代码块中，直接输出 Markdown）：
"""
