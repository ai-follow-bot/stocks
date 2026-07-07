"""搜索内容片段提取（共享工具）。

从搜索结果/新闻正文里提取里程碑关键词锚定的片段，跳过站点导航/记者署名等 boilerplate。
新闻前 300 字多为导航 junk，量产/送样/突破等事实往往在文章中段，直接 [:N] 会切掉关键事实。

供 skills/valuation-lens、skills/deep-analyze 等所有需要处理搜索内容的 pipeline 复用。
"""

# 强锚点（具体里程碑）优先于弱锚点（泛词）
STRONG_ANCHORS = ["量产", "送样", "突破", "订单", "放量", "起量", "商业化", "认证",
                  "国产替代", "缺货", "涨价"]
WEAK_ANCHORS = ["产能", "扩产", "规模", "落地", "在研", "垄断", "独家", "卡脖子",
                "市占率", "渗透"]


def snippet(content: str, window: int = 500, skip: int = 80) -> str:
    """从 content 提取里程碑关键词锚定的片段，跳过站点导航/记者署名等 boilerplate。

    优先在 skip 之后找强锚点（量产/送样/突破…），其次弱锚点；取锚点前 60 字起、共 window 字；
    都找不到则返回前 window 字。
    """
    if not content:
        return ""
    for anchors in (STRONG_ANCHORS, WEAK_ANCHORS):
        positions = [content.find(a, skip) for a in anchors if a]
        positions = [p for p in positions if p >= 0]
        if positions:
            start = max(0, min(positions) - 60)
            return content[start:start + window].strip()
    return content[:window].strip()
