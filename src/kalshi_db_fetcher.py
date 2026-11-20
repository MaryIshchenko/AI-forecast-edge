"""Pull settled Kalshi markets into the questions/resolutions tables defined
by schema.sql.

Same live API-handling as kalshi_fetcher.py (settled-price trap, parlay
filtering, candlestick pre-close pricing) -- ported here rather than
rewritten, since that logic is already proven against the live API. The
difference is the sink: this writes straight into Postgres via src/db.py
(the live "AI vs You" app path), not a JSON file for the offline backtest.

context is always written as null here (unlike kalshi_fetcher.py's
yes_sub_title-as-context, which can be borderline outcome-revealing, e.g.
"Above 2.7 million") -- the DB pipeline stays strict about not storing any
text beyond the question itself, matching manifold_fetcher.py.

Usage:
    python src/kalshi_db_fetcher.py --limit 400 --min-close-date 2026-02-01
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
PAGE_SIZE = 200
MAX_RETRIES = 4

DEFAULT_CATEGORIES = [
    "Economics", "Politics", "Elections", "Science and Technology",
    "Climate and Weather", "World", "Health", "Financials",
]
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


def list_series(client: KalshiClient, categories: list[str]) -> dict[str, str]:
    """Return {ticker: category} for series belonging to any requested category."""
    payload = client.get("/series/", {"limit": 1000})
    series = payload.get("series", [])
    wanted = {c.lower() for c in categories}
    out = {s["ticker"]: s.get("category", "") for s in series if s.get("category", "").lower() in wanted}
    print(f"Found {len(out)} series across {len(categories)} categories (of {len(series)} total)")
    return out


def fetch_settled_markets(
    client: KalshiClient,
    series_categories: dict[str, str],
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
    ordered = list(series_categories.keys())
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
            m["_category"] = series_categories.get(series)
            m["_close_dt"] = close_dt
            collected.append(m)
            taken_from_series += 1

        if i % 50 == 0 or len(collected) >= limit:
            print(f"  scanned {i}/{len(ordered)} series, {len(collected)} candidates")

    print(
        f"Candidates: {len(collected)}  (skipped: {skipped['parlay']} parlay, {skipped['thin']} thin, "
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


def to_row(client: KalshiClient, m: dict, hours_before_close: int, lookback_days: int) -> dict:
    return {
        "external_id": m["ticker"],
        "question": (m.get("title") or "").strip() or m["ticker"],
        "market_prob": pre_close_price(client, m, hours_before_close, lookback_days),
        "close_date": m["_close_dt"].date(),
        "category": m.get("_category"),
        "volume": _f(m.get("volume_fp")),
        "liquidity": _f(m.get("liquidity_dollars")),
        "outcome": 1 if m["result"] == "yes" else 0,
    }


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
        "Without this the crowd's Brier is near-unbeatable",
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
    parser.add_argument(
        "--database-url", type=str, default=None,
        help="Postgres connection string. Defaults to $DATABASE_URL or the local dev DB",
    )
    args = parser.parse_args()

    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    client = KalshiClient(base_url=args.base_url)

    series_categories = list_series(client, categories)
    if not series_categories:
        print("No series matched those categories — check --categories spelling.", file=sys.stderr)
        sys.exit(1)

    print(f"Collecting up to {args.limit} settled markets (min_close_date={args.min_close_date})...")
    markets = fetch_settled_markets(
        client,
        series_categories,
        args.limit,
        args.min_close_date,
        args.min_volume,
        max_per_series=args.max_per_series,
    )
    if not markets:
        print("No markets survived filtering. Loosen --min-volume or --min-close-date.", file=sys.stderr)
        sys.exit(1)

    print(f"Pulling pre-close prices ({args.hours_before_close}h before close)...")
    conn = db.get_connection(args.database_url)
    n_written = 0
    n_priced = 0
    n_too_certain = 0
    try:
        for i, m in enumerate(markets, 1):
            row = to_row(client, m, args.hours_before_close, args.lookback_days)
            if row["market_prob"] is not None:
                n_priced += 1
                if max(row["market_prob"], 1 - row["market_prob"]) > args.max_certainty:
                    n_too_certain += 1
                    continue
            question_id = db.upsert_question(
                conn,
                external_id=row["external_id"],
                question=row["question"],
                market_prob=row["market_prob"],
                close_date=row["close_date"],
                category=row["category"],
                volume=row["volume"],
                liquidity=row["liquidity"],
            )
            db.upsert_resolution(conn, question_id=question_id, outcome=row["outcome"])
            conn.commit()
            n_written += 1
            if i % 25 == 0:
                print(f"  wrote {i}/{len(markets)} markets")
    finally:
        conn.close()

    print(
        f"\nWrote {n_written} resolved markets to questions/resolutions "
        f"({n_priced} priced, {n_too_certain} dropped as too certain)"
    )


if __name__ == "__main__":
    main()
