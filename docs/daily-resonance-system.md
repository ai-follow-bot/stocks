---
name: daily-resonance-system
description: "Daily sector resonance system with self-evolution, built at /opt/stocks/skills/daily_resonance/"
metadata: 
  node_type: memory
  type: project
  originSessionId: 01704151-13e1-4b45-a03f-4ab4f66e9b56
---

A 3-Agent daily sector resonance analysis system built at `/opt/stocks/skills/daily_resonance/`. Runs daily at 8:00 AM to process previous day's 财联社 news and output sector resonance rankings.

**Architecture:**
- Agent 1: Keyword-based news-to-sector mapping + event type/sentiment classification
- Agent 2: Deterministic 5-dimension resonance formula (density, sentiment, chain, diversity, importance)
- Agent 3: Report generation (template or LLM)
- Self-evolution: Bayesian weight update with convergence detection (14 days Δw<1%)

**Data sources (all reused):**
- 财联社 news from `/root/.hermes/data/investment-research/news/`
- 30 sectors from `data/sector_ecosystem.json`
- Keywords from `data/sector_keywords.json`
- A-stock list from `data/a_stock_list.json`
- LLM client from `chain_agent/llm/client.py`

**Key design decisions:**
- Runs on T-1 news at T 08:00, predicts T resonance
- Only Agent 3 uses LLM (optional); Agent 1+2 are deterministic
- Weight convergence: learning_rate = 1/(1+days), reset on 7-day accuracy < 50%
- Zero modifications to existing `chain_agent/` code

**Cost:** ~¥0.10/day, ~¥2/month

**Related:** [[workflow-spec-first]]
