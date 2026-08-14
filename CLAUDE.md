# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A go/no-go experiment, not a product: does an LLM forecaster beat the crowd (prediction market prices) on already-resolved binary questions, measured by Brier score? The "model" is not trained weights — it's a scaffold around Claude (ensemble of independent runs, median aggregation, a prompt that forces a calibrated number). The edge, if any, lives in the scaffold. If the experiment goes positive, `schema.sql` is the Supabase schema for the follow-on "AI vs You" v0 app, and `forecast.py`'s scaffold ports directly to a Next.js route.

`ROADMAP.md` has the full phase plan (data pipeline → forecasting harness → scoring/backtest → go/no-go gate → v0 product) and the current "done when" checklist — read it for where the project stands before starting new work. "Complete" there means a measured out-of-sample edge, not just runnable code.

### Anti-leakage rules (from ROADMAP.md — not all enforced in code yet)

Since the data is *resolved* markets, the model can cheat by recalling or being handed the answer. Rules 1–2 and part of 4 are enforced today (`--min-close-date`, no retrieval in `LLMForecaster`); the rest are roadmap targets, not current guarantees — don't assume `context` is leakage-free or that results are already split in/out-of-sample:

1. Forecast using only information available before the market's close date.
2. Prefer questions that resolved after the model's training cutoff.
3. Strip outcome hints from any text shown to the model (a `context` like "Above 2.7 million" gives away the answer).
4. Compare against the market price at a **fixed** lead time before close, not a random snapshot — the fetchers' `--hours-before-close`/`--lookback-hours` approximate this but aren't a strict fixed offset.
5. Split data into in-sample (tuning) and out-of-sample (the reported result) — not yet implemented; there's currently one pass over one dataset.

## Commands

```bash
# Zero-setup sanity check (no API key, no network): runs MockForecaster on the bundled sample
python src/run_experiment.py --mock

# Real run against Claude (needs ANTHROPIC_API_KEY in .env)
pip install -r requirements.txt
cp .env.example .env
python src/run_experiment.py --data data/your_resolved.json --n-ensemble 5

# Pull fresh backtest data (both are leakage-guarded via --min-close-date)
python src/kalshi_fetcher.py --limit 200 --min-close-date 2026-02-01 --out data/kalshi_resolved.json
python src/polymarket_fetcher.py --limit 200 --min-close-date 2026-02-01 --min-volume 20000 --out data/polymarket_resolved.json
```

There is no test suite, linter, or build step in this repo — `--mock` is the correctness check (it must NOT show a real edge; if it does, the harness itself is broken).

## Architecture

Linear pipeline, `run_experiment.py` orchestrates all of it:

1. `data_loader.load_resolved` — reads a JSON array of resolved questions (`id`, `question`, `market_prob`, `outcome`, `close_date`, optional `context`), validates required fields and value ranges.
2. `forecast.build_forecaster` — returns either `MockForecaster` (deterministic-ish noise around 0.5, no real edge, used by `--mock`) or `LLMForecaster` (ensemble of `n_ensemble` independent Claude calls per question at `temperature=1.0`, aggregated by **median** — not mean — so one wild outlier run doesn't skew the estimate). Both implement the `Forecaster.forecast(question, context) -> float` interface.
3. `scoring.py` (pure stdlib, deliberately no numpy/pandas) — `brier_score`, `log_loss`, `calibration` (binned ECE), and `bootstrap_brier_gap` (paired bootstrap on `market_brier - ai_brier` per resample, giving a CI on the edge, not just a point estimate). `verdict()` turns the CI into a plain-English "REAL EDGE / NO EDGE / INCONCLUSIVE" read: CI entirely above 0 is a real edge, entirely below 0 is no edge, straddling 0 means gather more questions (aim for 100–200+).
4. `run_experiment.py` writes `out/results.csv` (per-question rows) and `out/calibration.png` (predicted-vs-actual scatter against the y=x diagonal).

### Data fetchers (`kalshi_fetcher.py`, `polymarket_fetcher.py`)

Standalone CLIs, same output contract as `data_loader.load_resolved` expects. Each solves a *settled-price trap*: a market's price after it closes has already converged to ~0/~1 (the answer), so using it as `market_prob` would hand the market an unbeatable Brier score and guarantee a fake "no edge" result. Both instead pull the crowd's price from a window *before* close:

- **Kalshi**: candlesticks endpoint, `--hours-before-close` (default 24) back from `close_time`. Also filters out auto-generated parlay markets (`mve_collection_ticker`) and caps `--max-per-series` so one event with many correlated contracts can't dominate the sample and understate bootstrap uncertainty. `--max-certainty` can drop markets the crowd had already decided, since a sample dominated by 0.99/0.01 markets is also unbeatable.
- **Polymarket**: two APIs — Gamma for market discovery (`closed=true`, must resolve to literally "Yes"/"No" outcomes, i.e. no multi-candidate fields) and CLOB `/prices-history` for the pre-close price of the winning token, via `--lookback-hours`. Sorted by most-recently-closed by default, not by volume — volume-sorting skews toward lopsided "sure thing" markets and biases the Brier comparison.

Both fetchers take `--min-close-date` as the **leakage guard**: only pull markets that closed after the model's training cutoff, so an LLM backtest can neither recall nor look up the outcome from training data. This is the single rule that makes the experiment's results real — retrieval/web search is intentionally not wired into `LLMForecaster` for the same reason (see `retrieval` param in `forecast.py`, which raises `NotImplementedError` if enabled).

## Conventions in this codebase

- Python 3.8-compatible type hints (`from __future__ import annotations`), stdlib-first — `scoring.py` avoiding numpy/pandas is intentional, not an oversight.
- CLI scripts (`run_experiment.py`, `*_fetcher.py`) each own their `argparse` setup and are run directly with `python src/<script>.py`, not installed as a package (no `setup.py`/`pyproject.toml`).
- Comments in this repo tend to explain *why a naive approach would silently produce a wrong number* (settled-price trap, correlated-market bias, volume-sort bias) — that context matters when touching fetcher or scoring logic; don't remove it as "obvious."
