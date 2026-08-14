"""Pull resolved, binary (Yes/No) Manifold Markets questions into the
questions/resolutions tables defined by schema.sql.

Manifold's read endpoints are public, no auth needed. Docs:
https://docs.manifold.markets/api

Unlike kalshi_fetcher.py / polymarket_fetcher.py (which write a JSON file for
the local backtest in data_loader.py), this script writes straight into
Postgres via src/db.py -- it's the ingestion path for the live "AI vs You"
app, not the offline experiment. Future Kalshi/Polymarket DB ingestion should
follow the same shape: fetch -> price lookup -> db.upsert_question/upsert_resolution.

THE SETTLED-PRICE TRAP still applies here: Manifold's `probability` field on a
resolved market has already converged to the answer. There's no dedicated
historical-price endpoint, so this fetcher instead asks for the last bet
before a target timestamp (--days-before-close back from close_time) via
`GET /v0/bets?beforeTime=...` and uses that bet's probAfter as the crowd's
belief at that point -- the same "walk back to a price before close" idea as
the other two fetchers, just built from discrete bet events instead of
candlesticks/CLOB history.

QUALITY FILTER: joke/test markets are common on a play-money site, so a
market only counts if --min-traders distinct people bet on it (Manifold
exposes this directly as `uniqueBettorCount`) and it cleared --min-volume.

CATEGORY: despite being documented as a LiteMarket field, /search-markets
does NOT actually return `groupSlugs` (verified live) -- only the full
`GET /v0/market/{id}` detail object does. So category costs one extra API
call per market that clears the quality bar. That call's response is used
for exactly one field (`groupSlugs`); `description`/`textDescription` on
that same response are never read or stored, per the no-leakage rule below.
Markets are capped at --max-per-category so a pull doesn't end up dominated
by whichever few categories happen to have the most resolved markets.

Do NOT store any outcome-revealing text: `context` is always written as
null. This script never stores a market's description/comments.

Note: /search-markets' documented sort=close-date silently returns zero
results (verified live, not a docs typo on our end). It also hard-caps
`offset` at 1000 (400 Bad Request past that, verified live), and its own
error message says to page with sort=newest + a beforeTime cursor instead --
that's what this script does, walking backward by createdTime. That means
markets are visited newest-created-first, not most-recently-resolved-first;
with no depth limit, that's an acceptable trade for actually being able to
page past the first 1000 results.

Usage:
    python src/manifold_fetcher.py --limit 400 --min-traders 50
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

DEFAULT_BASE_URL = "https://api.manifold.markets/v0"
PAGE_SIZE = 1000
MAX_RETRIES = 4
MS_PER_DAY = 24 * 60 * 60 * 1000
UNCATEGORIZED = "uncategorized"


class ManifoldClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, pause: float = 0.1):
        self.base_url = base_url
        self.pause = pause
        self.session = requests.Session()

    def get(self, path: str, params: dict | None = None):
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
            resp.raise_for_status()
            time.sleep(self.pause)
            return resp.json()
        raise RuntimeError(f"Gave up after {MAX_RETRIES} retries against {url}")


def market_category(client: ManifoldClient, market_id: str) -> str | None:
    """First group slug for a market, or None if it belongs to no group.

    Only reads groupSlugs from the full market-detail response -- the same
    response also has `description`/`textDescription`, which are never
    touched here (leakage guard).
    """
    detail = client.get(f"/market/{market_id}") or {}
    groups = detail.get("groupSlugs") or []
    return groups[0] if groups else None


def fetch_resolved_binary_markets(
    client: ManifoldClient,
    limit: int,
    min_close_date: str | None,
    min_traders: int,
    min_volume: float,
    max_per_category: int,
    max_scan: int,
    categories: list[str] | None,
) -> tuple[list[dict], dict[str, int], Counter]:
    """Page through /search-markets for resolved YES/NO markets that closed
    on/after the cutoff, meet the quality bar, and fit under the per-category
    cap. Stops once `limit` is collected, `max_scan` raw markets have been
    looked at, or the API runs out of pages -- whichever comes first.
    """
    cutoff_ms = None
    if min_close_date:
        cutoff_ms = int(
            datetime.strptime(min_close_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000
        )
    wanted = {c.lower() for c in categories} if categories else None

    collected: list[dict] = []
    category_counts: Counter = Counter()
    skipped = {"not_yes_no": 0, "too_early": 0, "low_quality": 0, "wrong_category": 0, "category_cap": 0}
    scanned = 0
    cursor = None  # createdTime to page backward from; None = start at newest

    while len(collected) < limit and scanned < max_scan:
        params = {
            "contractType": "BINARY",
            "filter": "resolved",
            "sort": "newest",
            "limit": PAGE_SIZE,
        }
        if cursor is not None:
            params["beforeTime"] = cursor
        page = client.get("/search-markets", params)
        if not page:
            break

        for m in page:
            if len(collected) >= limit or scanned >= max_scan:
                break
            scanned += 1

            if m.get("resolution") not in ("YES", "NO"):
                skipped["not_yes_no"] += 1
                continue
            close_ms = m.get("closeTime")
            if close_ms is None:
                skipped["not_yes_no"] += 1
                continue
            if cutoff_ms and close_ms < cutoff_ms:
                skipped["too_early"] += 1
                continue
            if (m.get("uniqueBettorCount") or 0) < min_traders or (m.get("volume") or 0) < min_volume:
                skipped["low_quality"] += 1
                continue

            category = market_category(client, m["id"])
            if wanted and (category is None or category.lower() not in wanted):
                skipped["wrong_category"] += 1
                continue
            bucket = category or UNCATEGORIZED
            if category_counts[bucket] >= max_per_category:
                skipped["category_cap"] += 1
                continue

            m["_category"] = category
            category_counts[bucket] += 1
            collected.append(m)

        cursor = page[-1]["createdTime"]
        if len(page) < PAGE_SIZE:
            break
        if scanned % 500 == 0:
            print(f"  scanned {scanned} markets, {len(collected)} accepted so far")

    print(
        f"Scanned {scanned} markets, accepted {len(collected)}  (skipped: "
        f"{skipped['low_quality']} low-quality, {skipped['category_cap']} over per-category cap, "
        f"{skipped['wrong_category']} wrong category, {skipped['too_early']} before cutoff, "
        f"{skipped['not_yes_no']} not YES/NO)"
    )
    return collected, skipped, category_counts


def pre_close_price(client: ManifoldClient, market: dict, days_before_close: int) -> float | None:
    """Crowd's YES probability `days_before_close` days before the market closed.

    Manifold has no candlestick/price-history endpoint, so this asks for the
    last bet placed before that timestamp and reads its probAfter -- the
    freshest belief still strictly before the target time.
    """
    target_ms = market["closeTime"] - days_before_close * MS_PER_DAY
    bets = client.get(
        "/bets",
        {"contractId": market["id"], "beforeTime": target_ms, "limit": 1, "order": "desc"},
    )
    if not bets:
        return None
    prob = bets[0].get("probAfter")
    if prob is None:
        return None
    return min(max(prob, 0.0), 1.0)


def to_row(client: ManifoldClient, market: dict, days_before_close: int) -> dict:
    close_dt = datetime.fromtimestamp(market["closeTime"] / 1000, tz=timezone.utc).date()
    return {
        "external_id": market["id"],
        "question": (market.get("question") or "").strip() or market["id"],
        "market_prob": pre_close_price(client, market, days_before_close),
        "close_date": close_dt,
        "category": market.get("_category"),
        "volume": market.get("volume"),
        "liquidity": market.get("totalLiquidity"),
        "outcome": 1 if market["resolution"] == "YES" else 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=200, help="Max markets to collect")
    parser.add_argument(
        "--min-close-date", type=str, default=None,
        help="YYYY-MM-DD leakage guard: only markets that closed on/after this date",
    )
    parser.add_argument(
        "--days-before-close", type=int, default=7,
        help="Sample the crowd probability this many days before market close",
    )
    parser.add_argument(
        "--min-traders", type=int, default=50,
        help="Quality filter: minimum distinct bettors (Manifold's uniqueBettorCount)",
    )
    parser.add_argument(
        "--min-volume", type=float, default=0.0,
        help="Additional quality filter: minimum volume in play-money mana (M$), not USD",
    )
    parser.add_argument(
        "--max-per-category", type=int, default=25,
        help="Cap markets taken from any one category so the pull spreads across topics",
    )
    parser.add_argument(
        "--max-scan", type=int, default=5000,
        help="Safety cap on how many raw search-markets results to look at before giving up",
    )
    parser.add_argument(
        "--categories", type=str, default=None,
        help="Comma-separated Manifold group slugs to keep (default: no filter, keep all topics)",
    )
    parser.add_argument("--base-url", type=str, default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--database-url", type=str, default=None,
        help="Postgres connection string. Defaults to $DATABASE_URL or the local dev DB",
    )
    args = parser.parse_args()

    categories = [c.strip() for c in args.categories.split(",")] if args.categories else None
    client = ManifoldClient(base_url=args.base_url)

    print(f"Collecting up to {args.limit} resolved binary markets (min_traders={args.min_traders})...")
    markets, skipped, category_counts = fetch_resolved_binary_markets(
        client, args.limit, args.min_close_date, args.min_traders, args.min_volume,
        args.max_per_category, args.max_scan, categories,
    )
    if not markets:
        print("No markets survived filtering. Loosen --min-traders/--min-volume/--min-close-date.", file=sys.stderr)
        sys.exit(1)

    conn = db.get_connection(args.database_url)
    n_priced = 0
    try:
        for i, m in enumerate(markets, 1):
            row = to_row(client, m, args.days_before_close)
            if row["market_prob"] is not None:
                n_priced += 1
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
            if i % 25 == 0:
                print(f"  wrote {i}/{len(markets)} markets")
    finally:
        conn.close()

    print(f"\nWrote {len(markets)} resolved markets to questions/resolutions ({n_priced} with a priced market_prob)")
    print(f"Dropped by quality filter (< {args.min_traders} traders or < {args.min_volume} volume): {skipped['low_quality']}")
    print("Count per category:")
    for cat, n in category_counts.most_common():
        print(f"  {cat}: {n}")
    print("First 3 rows -- eyeball these before trusting the pull:")
    for m in markets[:3]:
        print(f"  {m.get('question', '')[:64]!r}  resolution={m.get('resolution')}  category={m.get('_category')}")


if __name__ == "__main__":
    main()
