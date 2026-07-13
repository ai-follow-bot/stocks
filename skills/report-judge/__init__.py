"""skills.report-judge 报告质量评判（SPEC_report_judge.md）。

后评判层：报告出完后异步调 LLM 用 rubric 评判，输出结构化质量分 + issues + 改进建议。
不进 pipeline 主链路，不影响报告产出速度。为系统自改进闭环提供数据。
"""
