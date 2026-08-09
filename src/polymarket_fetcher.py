"""Pull closed, binary (Yes/No) Polymarket markets and write them into this
experiment's format.

Two public endpoints, no auth:
  - Gamma API (https://gamma-api.polymarket.com) for market discovery/metadata.
  - CLOB API (https://clob.polymarket.com) for historical prices.

Why this needs two endpoints, unlike Kalshi: Gamma's `outcomePrices` on a
CLOSED market is the final settled price (~"1"/"0" for the winner/loser) —
it's the answer, not the crowd's belief. Using it as `market_prob` would
silently hand the market baseline the resolved outcome. So this fetcher
pulls the last trade price from CLOB's price-history for the winning
outcome's token from a window *before* close (see --lookback-hours) as the
actual crowd baseline.

Only markets whose two outcomes are literally "Yes"/"No" are kept — that
matches Kalshi's structure and the yes/no prompt in forecast.py. Polymarket
also lists non-binary groups (e.g. a field of election candidates modeled as
several linked markets); those are skipped by construction since each
sub-market here is judged independently on its own outcome pair.

Usage:
    python src/polymarket_fetcher.py --limit 200 --min-close-date 2026-02-01 \\
        --out data/polymarket_resolved.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"
PAGE_SIZE = 100
MAX_RETRIES = 3
PRICE_HISTORY_WINDOW_BUFFER_S = 300  # stay 5 min clear of the settlement print


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    if "T" not in s:
        s = s.replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    elif s.endswith("+00"):
        s = s + ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _get(url: str, params: dict) -> dict | list:
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 429:
            wait = 2 ** attempt
            print(f"  rate limited, backing off {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Gave up after {MAX_RETRIES} retries against {url}")


def fetch_closed_binary_markets(
    limit: int = 200,
    min_close_date: str | None = None,
    min_volume: float = 1000.0,
    order: str = "closedTime",
    gamma_base_url: str = GAMMA_BASE_URL,
) -> list[dict]:
    """Page through Gamma's /markets, keeping closed, resolved, Yes/No markets
    that closed on/after min_close_date with at least min_volume traded.

    order defaults to most-recently-closed rather than highest-volume:
    sorting by volume skews the sample toward blowout "sure thing" markets
    (huge, lopsided bets that resolve as expected), which would make any
    Brier comparison look artificially good or bad. min_volume alone is
    enough to screen out illiquid noise.
    """
    cutoff = _parse_dt(min_close_date) if min_close_date else None
    out: list[dict] = []
    offset = 0

    while len(out) < limit:
        params = {
            "limit": PAGE_SIZE,
            "offset": offset,
            "closed": "true",
            "order": order,
            "ascending": "false",
        }
        try:
            page = _get(f"{gamma_base_url}/markets", params)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 422:
                print(
                    f"  Gamma API pagination limit hit at offset={offset} "
                    "(deep offset pagination is capped) — stopping with what was found. "
                    "Loosen --min-volume/--min-close-date to find more within range.",
                    file=sys.stderr,
                )
                break
            raise
        if not page:
            break

        for m in page:
            if m.get("umaResolutionStatus") not in (None, "resolved"):
                continue
            try:
                volume = float(m.get("volumeNum") or m.get("volume") or 0)
            except (TypeError, ValueError):
                volume = 0.0
            if volume < min_volume:
                continue

            close_dt = _parse_dt(m.get("closedTime") or m.get("endDate"))
            if not close_dt:
                continue
            if cutoff and close_dt < cutoff:
                continue

            try:
                outcomes = json.loads(m.get("outcomes") or "[]")
                outcome_prices = json.loads(m.get("outcomePrices") or "[]")
                clob_token_ids = json.loads(m.get("clobTokenIds") or "[]")
            except json.JSONDecodeError:
                continue
            if len(outcomes) != 2 or len(outcome_prices) != 2 or len(clob_token_ids) != 2:
                continue

            labels = [str(o).strip().lower() for o in outcomes]
            if "yes" not in labels or "no" not in labels:
                continue  # not a plain Yes/No market — skip (multi-outcome groups etc.)
            yes_idx = labels.index("yes")

            out.append(
                {
                    "id": m.get("id", ""),
                    "question": m.get("question", ""),
                    "close_dt": close_dt,
                    "yes_price_final": float(outcome_prices[yes_idx]),
                    "yes_token_id": clob_token_ids[yes_idx],
                }
            )
            if len(out) >= limit:
                break

        offset += PAGE_SIZE
        if len(page) < PAGE_SIZE:
            break

    return out[:limit]


def _pre_close_yes_price(
    yes_token_id: str,
    close_dt: datetime,
    lookback_hours: int,
    fidelity_minutes: int,
    clob_base_url: str = CLOB_BASE_URL,
) -> float | None:
    """Last traded price of the Yes token in a window ending shortly before
    close — the crowd's belief before the outcome was locked in, not after.
    """
    end_ts = int(close_dt.timestamp()) - PRICE_HISTORY_WINDOW_BUFFER_S
    start_ts = end_ts - lookback_hours * 3600

    try:
        payload = _get(
            f"{clob_base_url}/prices-history",
            {"market": yes_token_id, "startTs": start_ts, "endTs": end_ts, "fidelity": fidelity_minutes},
        )
    except requests.exceptions.HTTPError:
        return None  # token no longer indexed for price history (delisted/archived) — treat as unavailable
    history = payload.get("history", []) if isinstance(payload, dict) else []
    if not history:
        return None
    return float(history[-1]["p"])


def to_experiment_format(
    candidates: list[dict],
    lookback_hours: int,
    fidelity_minutes: int,
    clob_base_url: str = CLOB_BASE_URL,
) -> list[dict]:
    records = []
    skipped_no_history = 0

    for c in candidates:
        market_prob = _pre_close_yes_price(
            c["yes_token_id"], c["close_dt"], lookback_hours, fidelity_minutes, clob_base_url
        )
        if market_prob is None:
            skipped_no_history += 1
            continue

        records.append(
            {
                "id": c["id"],
                "question": c["question"],
                "market_prob": round(min(max(market_prob, 0.0), 1.0), 4),
                "outcome": 1 if c["yes_price_final"] >= 0.5 else 0,
                "close_date": c["close_dt"].date().isoformat(),
                "context": None,
            }
        )

    if skipped_no_history:
        print(
            f"  skipped {skipped_no_history} market(s) with no price history in the lookback window "
            "(illiquid or too old for CLOB's indexed range)",
            file=sys.stderr,
        )
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=200, help="Max number of markets to fetch")
    parser.add_argument(
        "--min-close-date",
        type=str,
        default=None,
        help="YYYY-MM-DD leakage guard: only markets that closed on/after this date",
    )
    parser.add_argument("--min-volume", type=float, default=1000.0, help="Minimum USD volume traded")
    parser.add_argument(
        "--order",
        type=str,
        default="closedTime",
        help="Gamma sort field (default closedTime = most recently resolved first; 'volume' skews toward lopsided blowout markets)",
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=24,
        help="Sample the crowd price this many hours before market close (avoids the settlement print itself)",
    )
    parser.add_argument("--fidelity-minutes", type=int, default=60, help="CLOB price-history bucket size")
    parser.add_argument("--gamma-base-url", type=str, default=GAMMA_BASE_URL)
    parser.add_argument("--clob-base-url", type=str, default=CLOB_BASE_URL)
    parser.add_argument("--out", type=str, required=True, help="Output JSON path")
    args = parser.parse_args()

    print(f"Fetching up to {args.limit} closed binary Polymarket markets (min_close_date={args.min_close_date})...")
    candidates = fetch_closed_binary_markets(
        limit=args.limit,
        min_close_date=args.min_close_date,
        min_volume=args.min_volume,
        order=args.order,
        gamma_base_url=args.gamma_base_url,
    )
    print(f"Found {len(candidates)} candidate markets, pulling pre-close prices...")
    records = to_experiment_format(
        candidates, args.lookback_hours, args.fidelity_minutes, clob_base_url=args.clob_base_url
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(records, f, indent=2)

    print(f"Wrote {len(records)} resolved binary markets to {out_path}")


if __name__ == "__main__":
    main()
