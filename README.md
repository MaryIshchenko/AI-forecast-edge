# ai-forecast-edge

The first experiment before you build anything: does your AI forecast better than the crowd? If it doesn't, no legal structure or app matters. This repo answers that cheaply.

The "model" is not trained weights — it's a scaffold around Claude (ensemble of runs, median aggregation, calibration). The edge lives in the scaffold, not the weights.

## Run it in 30 seconds (no API key)

```bash
python src/run_experiment.py --mock
```

This runs the whole pipeline on 8 sample questions with a fake forecaster so you can see the scoring, bootstrap confidence read, `out/results.csv`, and `out/calibration.png`. The mock has no real edge, so the crowd "wins" — that's the test correctly refusing to flatter you.

## Run it for real

```bash
pip install -r requirements.txt
cp .env.example .env    # add your ANTHROPIC_API_KEY
python src/run_experiment.py --data data/your_resolved.json --n-ensemble 5
```

## The one rule that makes results real (leakage)

For a backtest on already-resolved questions, do not give the model web search — today's internet knows the answer. The clean setup:

- Use questions that resolved after the model's training cutoff, and
- run with retrieval off.

Then the model can neither recall nor look up the outcome. Retrieval is only honest for open questions in the live app, where the future genuinely isn't known. This is a QA/leakage problem, which is your strength — treat it as the thing most likely to give you a fake positive.

## How to read the output

- **Brier score** — headline. Lower is better. The thesis is: AI Brier < market Brier.
- **Log loss** — secondary; punishes confident-and-wrong harder.
- **Calibration error / curve** — is a "70%" really a 70%? On-diagonal = calibrated.
- **Bootstrap CI on the Brier gap** — is the AI's win real or noise?
  - CI entirely above 0 → real edge.
  - CI entirely below 0 → no edge as built; fix retrieval/prompt or stop.
  - CI straddles 0 → get more questions (aim for 100–200+).

## Getting real data (Kalshi)

`src/kalshi_fetcher.py` pulls settled Kalshi markets (public read, no auth needed) and writes them straight into the experiment format:

```bash
# Only markets that closed after your model's cutoff — this is the leakage guard.
python src/kalshi_fetcher.py --limit 200 --min-close-date 2026-02-01 --out data/kalshi_resolved.json
python src/run_experiment.py --data data/kalshi_resolved.json --n-ensemble 5
```

Each Kalshi market is already binary (one YES/NO outcome), so it maps cleanly: last trade price → `market_prob` (the crowd baseline), settlement → `outcome`. Confirm the current base URL and terms of service at https://docs.kalshi.com before a big pull, and respect the rate limits.

## Getting real data (Polymarket)

`src/polymarket_fetcher.py` pulls closed, Yes/No Polymarket markets (public read, no auth):

```bash
python src/polymarket_fetcher.py --limit 200 --min-close-date 2026-02-01 --min-volume 20000 --out data/polymarket_resolved.json
python src/run_experiment.py --data data/polymarket_resolved.json --n-ensemble 5
```

Two things make this one trickier than Kalshi, both handled by the script:

- **Multi-outcome markets.** Polymarket has markets with more than two outcomes (e.g. a field of candidates). This fetcher only keeps markets whose two outcomes are literally "Yes"/"No", same shape as Kalshi.
- **`outcomePrices` on a closed market is the final settled price** (≈1/0 for winner/loser) — it's the answer, not the crowd's belief, so it can't be used as `market_prob`. The fetcher instead pulls the last traded price from a window before close (`--lookback-hours`, default 24) via the CLOB price-history endpoint, so `market_prob` reflects genuine pre-resolution uncertainty. By default markets are sorted by most-recently-closed rather than by volume — sorting by volume skews the sample toward blowout "sure thing" markets, which would flatter (or sink) the AI's Brier score for the wrong reason.

Confirm current endpoints/terms at https://docs.polymarket.com before a big pull.

## Files

```
src/scoring.py            Brier, log loss, calibration, bootstrap  (pure stdlib)
src/forecast.py           LLM scaffold + ensemble + MockForecaster
src/data_loader.py        load resolved questions
src/kalshi_fetcher.py     pull settled Kalshi markets
src/polymarket_fetcher.py pull closed Yes/No Polymarket markets
src/run_experiment.py     orchestrates the go/no-go test
schema.sql                 Supabase schema for the v0 "AI vs You" app
data/sample_resolved.json  offline sample so --mock works out of the box
```

## From experiment to v0 app

Once the edge test is positive, `schema.sql` is your Supabase store for the live game: `questions`, one `predictions` row per forecast (AI and each human), `resolutions` for truth, and a `leaderboard` view that computes Brier per predictor — that view is your public "AI vs humans" scoreboard. The forecasting logic in `forecast.py` ports directly to a TypeScript route in your Next.js app; the prompt and median-ensemble idea are identical.

Reminder: keep v0 free-to-play (no money in or out) until the edge is proven and you've had the legal conversation about which real-money structure, if any, to pursue.
