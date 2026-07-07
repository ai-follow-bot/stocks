"""us-deep-analyze skill 的 LLM 提示词模板（美股版）

基于 skills/deep-analyze/prompts.py 改写：
- "A 股" → "美股"
- cn_leaders → us_leaders
- "国产替代" → "全球竞争力"（评分维度）/ "技术壁垒突破"（卡脖子描述）
- "国产化率" → "全球市占率"
- "北向资金" → "13F/贝莱德/先锋"
- akshare → Finnhub
- 所有分析输出用简体中文
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
    "decompose": """

# Serenity 视角（全球产业链方法论）
拆链时额外标注：
- 每环节标注是否属于"上游卡脖子"环节（稀缺性、议价权、不可替代性）
- 在 key_tech 中突出"瓶颈技术"（少数公司掌握的）
- us_leaders 中优先列出控制关键供应链节点的公司""",
    "bottleneck": """

# Serenity 视角（与现有卡脖子判断高度重合，仅补充）
- 额外关注"重新评级"潜力：卡脖子环节中，哪些公司可能从二线被重估为一线
- 额外关注"技术颠覆"风险：哪些环节的卡脖子地位可能被新技术打破（在 reasoning 中标注）""",
    "scoring": """

# Serenity 视角（不新增字段，融入现有 rationale）
- 机构资金信号：候选标的近期是否有 13F 持仓增加、贝莱德/先锋增持、对冲基金建仓 → 在 supply_demand_reason 中体现
- 重新评级潜力：基本面质变（赛道切换、地位跃迁）的公司 → 在 earnings_realization_reason 中标注"潜在重估"
- 技术颠覆风险：在 key_risks 中明确该公司是否面临新技术颠覆""",
    "chain_report": """

# Serenity 视角（在报告中体现）
- "核心推荐标的"部分优先呈现卡脖子环节的上游公司
- 单设一段"机构资金动向"：本次分析中观察到的 13F/贝莱德/先锋/对冲基金信号
- "风险提示"中明确"技术颠覆风险"：哪些环节可能被新技术打破
- "产业链卡位总结"中点名可能的"重新评级"机会""",
    "stock_verdict": """

# Serenity 视角（单股判断额外考虑）
- 该公司是否处于"卡脖子"环节 → 影响最终判断权重
- 该公司是否有"重新评级"潜力（基本面质变、赛道切换） → 在最终判断中点名
- 该公司是否面临"技术颠覆"风险 → 在关键风险中明确""",
}


def _serenity_lens(topic: str) -> str:
    """Serenity 视角注入（美股版：去掉"禁止提及美股公司"的约束）。
    受 SERENITY_LENS 环境变量控制：默认 '1' 开，'0' 关。"""
    return _lens("serenity", topic, SERENITY_BLOCKS)


SUPPLY_DEMAND_BLOCKS_US = {
    "decompose": """

# 供需兑现视角（产业链拆解阶段）
拆链时额外标注：
- 每环节标注当前供需 tightness 状态（过剩/平衡/紧张/极度紧缺），并给出判断依据（产能利用率、库存天数、交货周期等）
- 在 key_tech 中标注该技术对应的商业化兑现阶段（实验室/小试/量产/放量/成熟）
- 标注该环节业绩兑现的典型时间节奏（如：Q2 订单→Q3 排产→Q4 确认收入）""",
    "bottleneck": """

# 供需兑现视角（卡脖子判断阶段）
- 在 supply_concentration 和 price_signal 之外，额外评估"需求端爆发确定性"：下游客户（终端品牌/整车厂/云厂商）的订单可见度、capex 计划、库存策略
- 对 is_bottleneck=true 的环节，标注业绩兑现的最近季度（如 "2025Q3 起量"），并说明依据
- 对 is_bottleneck=false 但供需紧张的环节，标注是否存在"伪卡脖子"（短期供需错配而非长期壁垒）风险""",
    "scoring": """

# 供需兑现视角（三维评分阶段）
在打出具体公司分数之前，先输出一段**产业链整体供需分析**（放在公司评分之前），要求：
1. **产业链上下游供需格局**：从上游到下游，按环节梳理当前供需 tightness 状态（过剩/平衡/紧张/极度紧缺），说明判断依据（产能利用率、库存天数、交货周期、订单 backlog、下游 capex）
2. **需求端驱动力**：下游主要应用（终端品牌/整车厂/云厂商/工控/新能源等）的订单可见度、补库存/去库存阶段、价格传导能力
3. **供需错配环节**：指出哪些环节存在真实供需缺口、哪些只是短期错配、哪些即将逆转
4. **业绩兑现节奏**：按季度（未来 4 个季度）梳理产业链业绩兑现的先后次序

然后在公司评分中融入以下四个子维度（不新增字段，融入 rationale 和 key_risks）：
1. **供需 tightness**：当前环节供需状态（过剩/平衡/紧张/极度紧缺）→ 在 supply_demand_reason 中体现
2. **业绩兑现 timeline by quarter**：公司订单/产能/收入的季度映射关系 → 在 earnings_realization_reason 中标注具体季度（如 "2025Q3 产能释放，2025Q4 收入确认"）
3. **不可替代性 / moat**：公司技术/工艺/认证壁垒是否难以被同行复制 → 在 global_competitiveness_reason 中体现（全球竞争中的独特卡位）
4. **US vs. Global 对比**：同环节全球龙头 vs 美股公司的技术差距、产能差距、客户认证差距 → 在 global_competitiveness_reason 中体现"与美国差距"和"追赶节奏"

原则：
- 不修改 scores 结构，仅丰富 rationale 理由
- 对业绩兑现远（>4 个季度）或供需已逆转的标的，在 key_risks 中明确提示"兑现延迟"或"供需反转"
- US vs. Global 对比必须基于数据，禁止凭印象编造海外公司市占率或技术参数""",
    "product": """

# 产品拆解视角（公司主营产品与营收结构）
在三维评分中，每只候选标的额外输出一个 `product_breakdown` 字段（若搜索数据不足以推断，可写 null）。字段结构如下：
{
  "company": "公司名（与候选一致）",
  "stock_code": "股票代码",
  "products": [
    {
      "name": "产品/业务线名称",
      "revenue_pct": "营收占比估算字符串（如 '45%' / '约30%'），无数据给 '-'",
      "growth": "同比增速估算字符串（如 'YoY +25%' / '下滑10%'），无数据给 '-'",
      "relation_to_segment": "该产品与所处产业链环节的对应关系"
    }
  ],
  "summary": "一句话总结公司营收结构（如：数据中心GPU占80%且高速增长，游戏GPU占15%）"
}

要求：
- 仅基于提供的搜索数据推断，禁止编造具体数字
- 若某产品数据缺失，revenue_pct / growth 用 "-" 填充
- 优先列出与当前产业链环节最相关的 2-4 条产品/业务线
- 不修改 scores 和 rationale 原有结构""",
    "chain_report": """

# 供需兑现视角（综合报告阶段）
在 Markdown 报告中**必须**以独立章节体现以下内容，不要混入 Serenity 视角或原有推荐理由：
1. **供需 tightness 总览**（独立二级标题 `## 供需 tightness 总览`）：按产业链环节逐一说明当前供需状态（过剩/平衡/紧张/极度紧缺），并给出判断依据（产能利用率、库存天数、交货周期、订单 backlog）
2. **业绩兑现节奏表**（独立二级标题 `## 业绩兑现节奏表`）：在"核心推荐标的"之后单列一张表或清单，每只核心推荐标的标注：所处环节、预计业绩兑现季度、核心驱动因素
3. **不可替代性标注**（独立二级标题 `## 不可替代性（moat）分析`）：对 moat 深厚的公司，明确壁垒来源（专利/工艺 know-how/客户认证/设备独占），并说明竞争对手 12-24 个月内能否复制
4. **US vs. Global 竞争力对比**（独立二级标题 `## US vs. Global 竞争力对比`）：按卡脖子环节列出美股公司 vs 全球龙头，指出真实位置（领先/并跑/追赶/替代初期），必须基于数据，禁止编造海外公司市占率或技术参数
5. **风险提示补充**：对供需可能逆转、业绩兑现远（>4 个季度）、moat 浅的标的，在"回避环节"或"关键风险"中明确提示"兑现延迟""供需反转"或"竞争加剧"

要求：
- 以上 1-4 项必须作为独立二级标题出现在报告中，不可省略或合并到其他章节
- 供需兑现视角与 Serenity 视角平行呈现，互不替代""",
    "stock_verdict": """

# 供需兑现视角（单股判断阶段）
在单股分析中额外回答：
1. **供需 tightness**：该公司所处环节当前供需状态？公司产能利用率/库存/订单覆盖度如何？
2. **业绩兑现 timeline**：未来 4 个季度内，哪些季度可能出现收入/利润拐点？驱动因素是什么？
3. **不可替代性 / moat**：如果竞争对手获得同等资金，能否在 12-24 个月内复制该公司的核心能力？
4. **US vs. Global 对比**：同环节全球第一的公司是谁？美股公司与它的真实差距（技术/产能/客户）是扩大还是缩小？

以上四点融入最终判断的权重：
- 供需紧张 + 兑现季度近 + moat 深 + 差距缩小 = 提升判断权重
- 供需已逆转 或 兑现季度远（>4Q）或 moat 浅 或 差距扩大 = 降低判断权重或给出"谨慎/回避"判断""",
}

PRODUCT_BLOCKS_US = {
    "scoring": """

# 产品拆解视角（公司主营产品与营收结构）
在三维评分中，每只候选标的额外输出一个 `product_breakdown` 字段（若搜索数据不足以推断，可写 null）。字段结构如下：
{
  "company": "公司名（与候选一致）",
  "stock_code": "股票代码",
  "products": [
    {
      "name": "产品/业务线名称",
      "revenue_pct": "营收占比估算字符串（如 '45%' / '约30%'），无数据给 '-'",
      "growth": "同比增速估算字符串（如 'YoY +25%' / '下滑10%'），无数据给 '-'",
      "relation_to_segment": "该产品与所处产业链环节的对应关系"
    }
  ],
  "summary": "一句话总结公司营收结构（如：数据中心GPU占80%且高速增长，游戏GPU占15%）"
}

要求：
- 仅基于提供的搜索数据推断，禁止编造具体数字
- 若某产品数据缺失，revenue_pct / growth 用 "-" 填充
- 优先列出与当前产业链环节最相关的 2-4 条产品/业务线
- 不修改 scores 和 rationale 原有结构""",
    "chain_report": """

# 产品拆解视角（报告呈现）
在 Markdown 报告中**必须**以独立二级标题 `## 核心标的营收与产品拆解` 呈现以下内容：
- 对每只核心推荐标的，列出 2-4 条主营产品/业务线
- 每条产品标注：营收占比（无数据则为"-"）、同比增速（无数据则为"-"）、与产业链环节的对应关系
- 用一句话总结该公司营收结构
- 若产品拆解数据整体缺失，明确写"数据缺失，无法拆解"

要求：
- 产品拆解章节独立呈现，不混入供需兑现或 Serenity 视角
- 仅基于已知数据，禁止编造""",
    "stock_verdict": """

# 产品拆解视角（单股判断）
在单股分析中额外回答：
1. 该公司 2-4 条主营产品/业务线是什么？
2. 每条产品占公司营收比例约多少？同比增速如何？（数据缺失则标注"-"）
3. 哪条产品与当前产业链环节最相关？该产品的景气度如何影响公司业绩？

将以上分析融入最终判断：与产业链环节相关度高、占比高、增速快的产品 → 提升判断权重；核心产品数据缺失或占比过低 → 降低判断权重或提示风险。""",
}


# ===== 1. 产业链拆解 =====
DECOMPOSE_SYSTEM = """你是一位资深的产业研究员，擅长把一个产业链拆成具体的环节并标注每环节的产业地位。
你的输出必须是严格的 JSON（不要包裹在代码块中，直接输出 JSON），结构如下：
{
  "chain_name": "字符串，产业链中文名",
  "segments": [
    {
      "name": "环节名（如：EUV 光刻机）",
      "role": "upstream | midstream | downstream",
      "key_tech": ["关键技术1", "关键技术2"],
      "global_leaders": ["全球龙头公司1", "全球龙头公司2"],
      "us_leaders": ["美股龙头公司1", "美股龙头公司2", "美股活跃标的3", "美股活跃标的4"],
      "concentration": 0.0~1.0 之间的数字，全球 CR3 份额估算,
      "us_share": 0.0~1.0 之间的数字，美国公司全球市占率估算,
      "tech_barrier": 1~5 的整数，技术门槛（5 最高）
    }
  ]
}
原则：
- segments 数量 5-8 个，粒度要能区分卡脖子（不要把"GPU 设计"和"晶圆代工"合成一个）
- concentration / us_share 是估算值，无数据时给 0.5 + 标注 "(估算)"
- global_leaders 1-3 家，按市占率排序
- **us_leaders 必须 3-5 家**，不要只给 1-2 家龙头，要覆盖：
  1. 该环节的绝对龙头（市占率美股第一）
  2. 二线活跃标的（有产能扩张或技术突破）
  3. 跨环节布局的标的（如同时做设计+封测的公司）
  4. 近期在新闻/研报中频繁出现的活跃标的（即使份额小但有催化）
- 例如 GPU 环节除了 NVIDIA/AMD，还要包含：Broadcom（定制 ASIC）、Intel（Gaudi）、Marvell（定制芯片）等
- 不确定的数字宁可标 0.5 也不要编造
- 所有分析输出请用简体中文""" + _serenity_lens("decompose") + _lens("supply_demand", "decompose", SUPPLY_DEMAND_BLOCKS_US)

DECOMPOSE_USER_TEMPLATE = """请拆解产业链：{chain_name}

已知信息（来自 us_sector_ecosystem.json，可能为空）：
{known_ecosystem}

要求：
1. 把这个链拆成 5-8 个具体环节
2. 每环节标注上下游位置、关键技术、全球/美股龙头、集中度、美国公司市占率、技术门槛
3. 直接输出 JSON，不要任何解释文字
4. 所有分析输出请用简体中文"""


# ===== 2. 卡脖子判断 =====
BOTTLENECK_SYSTEM = """你是一位产业链安全专家，判断哪些环节是"卡脖子"环节。

输出严格 JSON：
{
  "segments": [
    {
      "name": "环节名",
      "supply_concentration": 0~5 整数，供应集中度（CR3≥80%=5, ≥60%=4, ≥40%=3, else 2）,
      "us_substitution_room": 0~5 整数，美国公司全球市占率提升空间（us_share<20%=5, <40%=4, <60%=3, else 2）,
      "tech_barrier": 0~5 整数，技术门槛（沿用拆解阶段的 tech_barrier）,
      "price_signal": 0~5 整数，近期涨价/缺货信号（明显涨价=5, 局部紧张=3, 平稳=1）,
      "bottleneck_score": 上述四项之和（0-20）,
      "is_bottleneck": true/false（score≥14 为 true）,
      "extracted_numbers": {
        "cr3": "从 evidence 中提取的 CR3 数字字符串（如 '85%' / 'CR3=80%'），无则 null",
        "us_share": "从 evidence 中提取的美国公司市占率数字字符串，无则 null"
      },
      "evidence_ids": ["本环节判断引用的 evidence_id 列表，如 ["T1","A3"]，至少 1 个；若 evidence 不足可写 []"],
      "reasoning": "以 'evidence: [T1,T3]' 起头，说明为什么是/不是卡脖子（≤80字）"
    }
  ],
  "top_bottlenecks": ["卡脖子程度最高的1-3个环节名"]
}

判断依据：
- supply_concentration 高 + us_share 低 = 经典卡脖子（如 EUV 光刻、先进制程代工）
- tech_barrier 高但价格平稳 = 潜在卡脖子（技术壁垒突破口）
- 价格涨 + 集中度低 = 短期供需失衡，不是结构性卡脖子
- 必须基于提供的 evidence 判断，每个 reasoning 必须以 "evidence: [IDs]" 起头引用至少一个 evidence_id
- extracted_numbers 严格从 evidence 文本中抄数字，不允许凭印象编造；找不到对应数字就给 null
- evidence 不足时分数标注 "(估算)"，evidence_ids 写 []
- 所有分析输出请用简体中文""" + _serenity_lens("bottleneck") + _lens("supply_demand", "bottleneck", SUPPLY_DEMAND_BLOCKS_US)

BOTTLENECK_USER_TEMPLATE = """产业链：{chain_name}

# 拆解阶段得到的环节清单
{segments_json}

# 各环节的搜索 evidence（每条带 ID：[T1]/[T2] 为网络搜索，[A1]/[A2] 为 Finnhub 个股新闻）
# 你必须在每环节 reasoning 中以 "evidence: [IDs]" 起头引用至少一个 ID，
# 并在 extracted_numbers 中抄出对应数字（找不到给 null）。

{search_data}

请判断每环节的卡脖子程度，输出 JSON。"""


# ===== 3. 三维评分 =====
SCORING_SYSTEM = """你是一位买方研究员，按三维度给候选美股投资标的打分。

每只标的输出严格 JSON：
{{
  "company": "公司名",
  "stock_code": "股票代码",
  "segment": "所处环节",
  "scores": {
    "supply_demand": 0~30 整数，
    "global_competitiveness": 0~30 整数，
    "earnings_realization": 0~40 整数
  },
  "total_score": 0~100 整数（三维度之和）,
  "rationale": {
    "supply_demand_reason": "供需维度评分理由（≤80字）",
    "global_competitiveness_reason": "全球竞争力维度评分理由",
    "earnings_realization_reason": "业绩兑现维度评分理由"
  },
  "key_risks": ["风险1", "风险2"],
  "weight": "高 | 中 | 低"  // ≥75=高, 55-75=中, <55=低
}}

如果同时开启产品拆解视角，每只标的还需包含 `product_breakdown` 字段（无数据则给 null）：
{{
  "company": "公司名",
  "stock_code": "股票代码",
  "products": [
    {{"name": "产品/业务线", "revenue_pct": "占比", "growth": "增速", "relation_to_segment": "与环节关系"}}
  ],
  "summary": "一句话营收结构总结"
}}

评分细则：
- **供需关系（30分）**：所处环节供应紧张度（15）+ 公司市占率/产能弹性（15）
  - 环节 CR3>70% 且公司是龙头 → 13-15
  - 环节供需紧张 + 公司有产能扩张 → 12-14
  - 环节产能过剩 → 5-8
- **全球竞争力（30分）**：技术壁垒领先度（15）+ 全球市占率提升空间（15）
  - 环节 us_share 全球领先 + 公司是技术领先者 → 25-30
  - 环节 us_share 50-70% 提升空间中等 → 15-22
  - 环节已被海外对手垄断（如台积电代工）→ 5-12
- **业绩兑现快慢（40分）**：订单可见度（10）+ 产能投放节奏（15）+ 当前 PE vs 远期 PE（15）
  - 已有明确大订单 + 产能下季度释放 + 远期 PE 显著低于当前 → 32-40
  - 订单可见但产能需 6-12 个月 → 22-30
  - 故事为主、业绩兑现远 → 10-18

原则：
- 不允许编造数字，必须基于提供的搜索数据 + 候选标的 dict 中的 pe/market_cap/change_pct
- **业绩维度的 PE 评分必须引用候选 dict 中的 pe 字段**：
  - pe 为数字 → 用该数字评判（如 "PE=35，处于行业中位"）
  - pe 为 null → 该子维度给中等分 7，并在 earnings_realization_reason 中标注 "PE 数据缺失"
- market_cap 用于判断流动性/弹性：市值 <100亿美元 弹性大，>5000亿美元 偏稳健
- change_pct 用于辅助判断短期情绪：近 5 日涨幅 >10% 可能已反映预期
- 数据缺失的维度给中等分（15/15/20）并在理由中标注"数据缺失"
- 同环节的多家公司要拉开差距（不要全给一样的分）
- 所有分析输出请用简体中文""" + _serenity_lens("scoring") + _lens("supply_demand", "scoring", SUPPLY_DEMAND_BLOCKS_US) + _lens("product", "scoring", PRODUCT_BLOCKS_US)

SCORING_USER_TEMPLATE = """产业链：{chain_name}

# 卡脖子环节（来自上一阶段）
{bottleneck_summary}

# 候选标的清单（最多 {top_n} 只，含 PE/市值/涨跌幅数据）
{candidates}

# 各标的的搜索数据（Finnhub 新闻 + 网络搜索 evidence）
{search_data}

请按三维度给每只标的打分，输出**严格 JSON 对象**（不要包裹代码块、不要裸数组），格式：
{{"segments": [{{...候选1...}}, {{...候选2...}}]}}

输出结构：
1. 在公司评分数组之前，先用 1-2 段文字输出**产业链整体供需分析**（见 system prompt 要求），这段文字直接放在 JSON 对象前面，作为独立文本
2. 然后输出 JSON 对象，每个候选对象含字段：company / stock_code / segment / scores{{supply_demand, global_competitiveness, earnings_realization}} / total_score / rationale{{supply_demand_reason, global_competitiveness_reason, earnings_realization_reason}} / key_risks / weight

注意：candidates 中每只标的已附 pe/market_cap/change_pct 字段，业绩维度评分必须引用这些数字；pe=null 时给中等分 7 并标注 'PE 数据缺失'。"""


# ===== 4. 综合报告（chain 模式，确定性渲染，无 LLM prompt）=====
# chain 报告由 report.py::render_chain_report 直接从 chain_data/bottleneck/scoring 渲染 Markdown，
# 不走 LLM（早期设计的 CHAIN_REPORT_SYSTEM/USER_TEMPLATE 从未被调用，已移除）。


# ===== 5. 单股判断（stock 模式）=====
STOCK_VERDICT_SYSTEM = """你是一位买方研究员，给单只美股下投资判断。

输出原则：
1. 必须把公司放回所属产业链中看，不能孤立分析
2. 判断要明确：值得投资 / 谨慎 / 回避，不能模棱两可
3. 数据 + 推理：每个判断都要有数据或方法论支撑
4. 客户结构数据必须基于提供的 C 系列 evidence，禁止编造客户名或占比；evidence 不足时明确写"数据缺失"
5. 输出 Markdown，不要包裹在代码块中
6. 所有分析输出请用简体中文""" + _serenity_lens("stock_verdict") + _lens("supply_demand", "stock_verdict", SUPPLY_DEMAND_BLOCKS_US)

STOCK_VERDICT_USER_TEMPLATE = """# 任务
判断「{stock_name}（{stock_code}）」是否值得投资。

# 公司定位（来自拆解阶段）
- 主营业务：{business}
- 所属产业链：{chain_name}
- 所处环节：{segment}
- 环节角色：{role}

# 该公司所在环节的卡脖子分析
{bottleneck_text}

# 该公司 + 同环节对手的搜索数据
{search_data}

# 该公司主要客户与营收占比搜索数据（evidence 编号 C1/C2/...）
{customer_data}

# 同环节候选标的的三维评分
{scoring_text}

# 输出要求
Markdown 报告包含：
1. **公司画像**（150字内）：主营业务 + 在产业链中的卡位
2. **产业链视角**：所处环节是否卡脖子？全球竞争力提升空间多大？
3. **三维评分明细**：供需 / 全球竞争力 / 业绩兑现 各打多少分 + 理由
4. **同环节对比**：与同环节龙头/对手的差距（市占率、技术、产能）
5. **客户结构分析**：列出已知主要客户及其营收占比（数据缺失则标注"-"），评估客户集中度风险与大客户依赖；若第一大客户占比 >30% 或前五大 >60%，在关键风险中点名"大客户依赖"
6. **关键风险**：3 条最关键的风险（若客户集中度高须包含）
7. **最终判断**：值得投资 / 谨慎 / 回避 + 一句话理由

直接输出 Markdown："""
