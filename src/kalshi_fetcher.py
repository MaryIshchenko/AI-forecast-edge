"""Pull settled Kalshi markets into this experiment's format.

Kalshi's read endpoints are public — no auth needed. Verified live against
the v2 REST API; confirm the base URL and terms at https://docs.kalshi.com
before a big pull, and respect the rate limits.

Two things about Kalshi's data make the naive approach produce garbage, and
both are handled here:

1. THE SETTLED-PRICE TRAP. A settled market's `last_price_dollars` has already
   converged to the answer (~0.99 for YES, ~0.01 for NO). Using it as
   `market_prob` hands the market a near-perfect Brier score that no
   forecaster can ever beat — a guaranteed fake negative. Instead we pull the
   market's price from the candlesticks endpoint at `--hours-before-close`,
   which is the crowd's genuine belief while the outcome was still unknown.

2. PARLAY SPAM. Listing `status=settled` unfiltered returns ~100% auto-generated
   multi-leg parlay markets ("yes Player A,yes Player B,yes Player C") that
   settle constantly and drown out real questions. We enumerate series by
   category instead, and skip anything with an `mve_collection_ticker`.

Usage:
    python src/kalshi_fetcher.py --limit 200 --min-close-date 2026-06-01 \\
        --out data/kalshi_resolved.json

--min-close-date is the leakage guard: only pull markets that closed after
your model's training cutoff, so the LLM can neither recall nor look up the
outcome.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
PAGE_SIZE = 200
MAX_RETRIES = 4

# Categories that tend to hold genuine forecasting questions. "Exotics" is
# excluded on purpose — that's where the parlay markets live.
DEFAULT_CATEGORIES = [
    "Economics",
    "Politics",
    "Elections",
    "Science and Technology",
    "Climate and Weather",
    "World",
    "Health",
    "Financials",
]

# Candlestick bucket size in minutes. Kalshi accepts 1, 60, and 1440.
PERIOD_INTERVAL = 60


class KalshiClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, pause: float = 0.1):
        self.base_url = base_url
        self.pause = pause
        self.session = requests.Session()

    def get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as e:
                if attempt == MAX_RETRIES:
                    raise
                print(f"  network error ({e}), retry {attempt}/{MAX_RETRIES}", file=sys.stderr)
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 429:
                wait = 2 ** attempt
                print(f"  rate limited, backing off {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            time.sleep(self.pause)
            return resp.json()
        raise RuntimeError(f"Gave up after {MAX_RETRIES} retries against {url}")


def _f(value, default: float | None = None) -> float | None:
    """Kalshi returns numbers as strings ('0.0700'). Parse defensively."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def list_series(client: KalshiClient, categories: list[str]) -> list[str]:
    """Return series tickers belonging to any of the requested categories."""
    payload = client.get("/series/", {"limit": 1000})
    series = payload.get("series", [])
    wanted = {c.lower() for c in categories}
    out = [s["ticker"] for s in series if s.get("category", "").lower() in wanted]
    print(f"Found {len(out)} series across {len(categories)} categories (of {len(series)} total)")
    return out


def fetch_settled_markets(
    client: KalshiClient,
    series_tickers: list[str],
    limit: int,
    min_close_date: str | None,
    min_volume: float,
    max_per_series: int = 3,
    seed: int = 42,
) -> list[dict]:
    """Collect settled, binary, non-parlay markets that closed on/after the cutoff.

    `max_per_series` matters more than it looks. A single series can hold
    dozens of near-identical contracts (every candidate in one primary, every
    strike on one CPI print). Taking them all gives you a big-looking dataset
    of highly correlated questions — the bootstrap CI would then treat one
    event as many independent draws and badly understate the uncertainty.
    """
    cutoff = None
    if min_close_date:
        cutoff = datetime.strptime(min_close_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    collected: list[dict] = []
    skipped = {"parlay": 0, "not_binary": 0, "unresolved": 0, "too_early": 0, "thin": 0, "series_cap": 0}

    # Shuffle so a pull isn't dominated by whatever sorts first alphabetically.
    ordered = list(series_tickers)
    random.Random(seed).shuffle(ordered)

    for i, series in enumerate(ordered, 1):
        if len(collected) >= limit:
            break
        payload = client.get("/markets", {"limit": PAGE_SIZE, "status": "settled", "series_ticker": series})
        markets = payload.get("markets", [])
        if not markets:
            continue

        taken_from_series = 0
        for m in markets:
            if len(collected) >= limit:
                break
            if taken_from_series >= max_per_series:
                skipped["series_cap"] += 1
                continue
            if m.get("mve_collection_ticker"):
                skipped["parlay"] += 1
                continue
            if m.get("market_type") != "binary":
                skipped["not_binary"] += 1
                continue
            if m.get("result") not in ("yes", "no"):
                skipped["unresolved"] += 1
                continue

            close_dt = _parse_ts(m.get("close_time"))
            if close_dt is None:
                skipped["unresolved"] += 1
                continue
            if cutoff and close_dt < cutoff:
                skipped["too_early"] += 1
                continue
            if (_f(m.get("volume_fp"), 0.0) or 0.0) < min_volume:
                skipped["thin"] += 1
                continue

            m["_series_ticker"] = series
            m["_close_dt"] = close_dt
            collected.append(m)
            taken_from_series += 1

        if i % 50 == 0 or len(collected) >= limit:
            print(f"  scanned {i}/{len(ordered)} series, {len(collected)} candidates")

    n_series = len({m["_series_ticker"] for m in collected})
    print(
        f"Candidates: {len(collected)} from {n_series} distinct series  "
        f"(skipped: {skipped['parlay']} parlay, {skipped['thin']} thin, "
        f"{skipped['series_cap']} over per-series cap, {skipped['too_early']} before cutoff, "
        f"{skipped['not_binary']} non-binary, {skipped['unresolved']} unresolved)"
    )
    return collected


def pre_close_price(
    client: KalshiClient,
    market: dict,
    hours_before_close: int,
    lookback_days: int,
) -> float | None:
    """Crowd's YES probability `hours_before_close` hours before the market closed.

    This is the whole point of the fetcher: the settled price is the answer,
    but the price while the question was still open is a real forecast.
    """
    close_dt: datetime = market["_close_dt"]
    end = close_dt - timedelta(hours=hours_before_close)
    start = end - timedelta(days=lookback_days)

    payload = client.get(
        f"/series/{market['_series_ticker']}/markets/{market['ticker']}/candlesticks",
        {
            "start_ts": int(start.timestamp()),
            "end_ts": int(end.timestamp()),
            "period_interval": PERIOD_INTERVAL,
        },
    )
    candles = payload.get("candlesticks", [])
    if not candles:
        return None

    # Walk backwards to the most recent candle that actually has a close price.
    for candle in reversed(candles):
        price = _f((candle.get("price") or {}).get("close_dollars"))
        if price is not None and 0.0 < price < 1.0:
            return price
    return None


def to_experiment_format(
    client: KalshiClient,
    markets: list[dict],
    hours_before_close: int,
    lookback_days: int,
    max_certainty: float = 1.0,
) -> list[dict]:
    """Convert to experiment format, optionally dropping questions the crowd
    had already decided.

    `max_certainty` exists because of a measurement problem: a pull dominated
    by markets sitting at 0.99/0.01 gives the crowd a Brier near 0.05, which
    nothing can beat, so the experiment returns "no edge" no matter how good
    the forecaster is. Setting it to e.g. 0.90 keeps only questions that were
    still live. That makes the result a conditional claim — "the AI beats the
    crowd on questions the crowd found genuinely uncertain" — which is the
    honest and more interesting question anyway. Report the threshold you used.
    """
    records = []
    no_history = 0
    too_certain = 0

    for i, m in enumerate(markets, 1):
        prob = pre_close_price(client, m, hours_before_close, lookback_days)
        if prob is None:
            no_history += 1
            continue
        if max(prob, 1 - prob) > max_certainty:
            too_certain += 1
            continue

        records.append(
            {
                "id": m["ticker"],
                "question": (m.get("title") or "").strip() or m["ticker"],
                "market_prob": round(prob, 4),
                "outcome": 1 if m["result"] == "yes" else 0,
                "close_date": m["_close_dt"].date().isoformat(),
                "context": (m.get("yes_sub_title") or "").strip() or None,
            }
        )
        if i % 25 == 0:
            print(f"  priced {i}/{len(markets)} markets, {len(records)} usable")

    if no_history:
        print(
            f"  skipped {no_history} market(s) with no usable price history "
            f"{hours_before_close}h before close (illiquid or already converged)"
        )
    if too_certain:
        print(f"  skipped {too_certain} market(s) the crowd had already decided (--max-certainty)")
    return records


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--limit", type=int, default=200, help="Max markets to collect")
    parser.add_argument(
        "--min-close-date",
        type=str,
        default=None,
        help="YYYY-MM-DD leakage guard: only markets that closed on/after this date",
    )
    parser.add_argument(
        "--hours-before-close",
        type=int,
        default=24,
        help="Sample the crowd price this many hours before close (avoids the settled price)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=7,
        help="How far back to search for a price if the exact hour has no trades",
    )
    parser.add_argument(
        "--min-volume",
        type=float,
        default=1000.0,
        help="Skip thin markets — a market with no trading has no meaningful crowd price",
    )
    parser.add_argument(
        "--max-certainty",
        type=float,
        default=1.0,
        help="Drop questions the crowd had already decided, e.g. 0.90 keeps only live ones. "
        "Without this the crowd's Brier is near-unbeatable and the test always says 'no edge'",
    )
    parser.add_argument(
        "--max-per-series",
        type=int,
        default=3,
        help="Cap markets taken from any one series, so one event can't dominate the sample",
    )
    parser.add_argument(
        "--categories",
        type=str,
        default=",".join(DEFAULT_CATEGORIES),
        help="Comma-separated Kalshi categories to pull from",
    )
    parser.add_argument("--base-url", type=str, default=DEFAULT_BASE_URL)
    parser.add_argument("--out", type=str, required=True, help="Output JSON path")
    args = parser.parse_args()

    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    client = KalshiClient(base_url=args.base_url)

    series_tickers = list_series(client, categories)
    if not series_tickers:
        print("No series matched those categories — check --categories spelling.", file=sys.stderr)
        sys.exit(1)

    print(f"Collecting up to {args.limit} settled markets (min_close_date={args.min_close_date})...")
    markets = fetch_settled_markets(
        client,
        series_tickers,
        args.limit,
        args.min_close_date,
        args.min_volume,
        max_per_series=args.max_per_series,
    )
    if not markets:
        print("No markets survived filtering. Loosen --min-volume or --min-close-date.", file=sys.stderr)
        sys.exit(1)

    print(f"Pulling pre-close prices ({args.hours_before_close}h before close)...")
    records = to_experiment_format(
        client, markets, args.hours_before_close, args.lookback_days, args.max_certainty
    )

    if not records:
        print("No markets had usable price history.", file=sys.stderr)
        sys.exit(1)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(records, f, indent=2)

    base_rate = sum(r["outcome"] for r in records) / len(records)
    print(f"\nWrote {len(records)} resolved markets to {out_path}")
    print(f"  YES base rate: {base_rate:.1%}")
    print("  First 3 rows — eyeball these before trusting the pull:")
    for r in records[:3]:
        print(f"    {r['market_prob']:.2f} -> {r['outcome']}  {r['question'][:64]!r}")


if __name__ == "__main__":
    main()
