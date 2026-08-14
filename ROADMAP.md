# ai-forecast-edge — Roadmap

## What this project is trying to prove

One core question: **can an AI forecast prediction-market questions better than the market itself?**

Everything below — data, code, product — exists to answer that honestly. "Complete" does not mean "the code runs." It means: I measured a real edge, out-of-sample, and I'm confident I didn't fool myself.

---

## The one rule that decides everything: no leakage

The data is *resolved* markets — outcomes are already known. If the AI is asked a question that already resolved, it may simply know the answer from training, or an outcome hint may leak through the `context` field. That is not an edge; it is lookahead bias. If leakage isn't controlled, every result downstream is fake.

**Anti-leakage rules (enforced everywhere):**

1. The AI forecasts using only information available **before** the market's close date.
2. Prefer questions that resolved **after** the model's training cutoff, so it couldn't have memorized the outcome.
3. Strip outcome hints from any text shown to the model (e.g. a `context` of "Above 2.7 million" gives away the answer — remove or neutralize it).
4. Compare the AI's probability against the market price captured at a **fixed lead time** before close (e.g. 7 days), not a random snapshot.
5. Split data into in-sample (for tuning) and out-of-sample (for the real verdict). Never report the tuned numbers as the result.

---

## Phases

### Phase 0 — Define the test (do this first)
Write down, in the README:
- What counts as "edge": AI Brier score beats market Brier score out-of-sample, **and** a betting simulation is profitable after fees.
- The anti-leakage rules above.

**Done when:** there is a one-page spec of what's being measured and how it stays honest.

### Phase 1 — Real data pipeline
Currently ~5–10 markets. To claim anything statistically, target **500–1,000+ resolved markets** across categories (bare minimum ~200).

Sources with APIs:
- **Kalshi** — real-money, settled markets + price history. Best "real edge" source.
- **Polymarket** — real-money, resolved markets + price history (Gamma / CLOB APIs).
- **Manifold Markets** — very open API, thousands of resolved questions, easiest bulk pull. Play-money, so treat as calibration research, not real-money edge.
- **Metaculus** — API with community forecasts on resolved questions; strong calibration benchmark.

**Make the data better:**
- Capture market price at a **consistent lead time** before close, so comparisons are fair.
- Store `category` and `volume`/`liquidity` so you can see *where* the AI wins.
- Balance categories so results generalize instead of reflecting one lucky bucket.
- Snapshot the info-available-at-forecast-time for leakage control.

**Done when:** a script pulls resolved markets into the DB (`schema.sql`) automatically, with: question, market_prob at fixed lead time, outcome, category, close_date, liquidity.

### Phase 2 — Forecasting harness
For each question, get the AI's probability reproducibly, with anti-leakage rules enforced. Start with one model; add an ensemble later.

**Done when:** one command produces AI probabilities for the whole dataset, logged with timestamps.

### Phase 3 — Scoring & backtest
- Brier score and log-loss: AI vs market.
- Calibration curve (`out/calibration.png`).
- Betting simulation: bet when the AI disagrees with the market beyond a threshold, subtract realistic fees, report ROI and hit-rate.
- Report in-sample vs out-of-sample separately.

**Done when:** `out/results.csv` shows AI Brier, market Brier, and a fee-adjusted P&L on held-out data.

### Phase 4 — Go / No-Go gate
Run it and check honestly:

- [ ] Sample is big enough (200+ resolved markets, multiple categories)
- [ ] No leakage (spot-check: does the AI "know" answers it shouldn't?)
- [ ] AI Brier genuinely beats market Brier, **out-of-sample**
- [ ] Betting sim is positive **after** fees
- [ ] The edge holds in more than one category

Most boxes check → real project, go to Phase 5.
They don't → don't force it. Either narrow to the niche where the AI *does* win, or pivot to the entertainment angle below.

### Phase 5 — Free-to-play v0
Wrap the measured edge into a product: humans predict against the AI's picks, or the AI's disagreements-with-market surface as a feed.
Stack: Next.js front end + Python scoring behind FastAPI.

---

## Honest caveat

Liquid real-money markets (Kalshi, Polymarket) are fairly efficient, so a durable edge is genuinely hard and many attempts fail Phase 4. That's fine. The human-vs-AI **entertainment** angle can work even if the AI only matches the market, because there the product is the game, not the edge. Know which one you're building.

---

## How to work through this with Claude Code

Feed one phase at a time as a task. For bigger phases, use plan mode (Shift+Tab twice) so Claude lays out the plan and waits for your OK before editing. Review every diff.

**Phase 1 starter prompt (paste into Claude Code):**

> Read schema.sql and the existing files in data/. Then write a Python ingestion script in src/ that pulls resolved markets from Kalshi's API into the database defined by schema.sql. For each market, capture: id, question, outcome, close_date, category, liquidity, and the market probability as of 7 days before close_date. Do not include any field that reveals the outcome. Add the script's dependencies to requirements.txt. Write it so I can later add Polymarket and Manifold ingestion the same way. Don't run it yet — show me the plan and the code first.

Then, once data is flowing, move to the Phase 2 harness, then Phase 3 scoring, then the Phase 4 gate.
