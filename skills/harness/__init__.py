"""harness skill：三视角交叉验证

并发跑三个独立分析路径，对齐到标的级做交叉验证，LLM 综合判断：
- chain_agent（确定性扫描，规则评分 0-100）
- deep-analyze（LLM 深度拆解，供需/国产替代/业绩兑现，total_score 0-100）
- valuation-lens（估值镜头，稀缺/前瞻/供需，valuation_score 0-100）

--chain 三路径交叉；--stock 两路径（deep+val，chain_agent 无单股模式）。
"""
__version__ = "0.1.0"
