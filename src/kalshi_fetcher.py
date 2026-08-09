"""Pull settled (binary, resolved) Kalshi markets and write them straight into
this experiment's format.

Kalshi's read endpoints are public and need no auth for settled market data.
Confirm the current base URL, endpoint shape, and terms of service at
https://docs.kalshi.com before a big pull — public APIs move. This targets
the v2 REST API as documented as of this writing.

Usage:
    python src/kalshi_fetcher.py --limit 200 --min-close-date 2026-02-01 \\
        --out data/kalshi_resolved.json

The --min-close-date flag is the leakage guard: only pull markets that
closed after your model's training cutoff, so the LLM can't have seen the
outcome during training.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
MARKETS_ENDPOINT = "/markets"
PAGE_SIZE = 100
MAX_RETRIES = 3


def _parse_close_date(min_close_date: str | None) -> datetime | None:
    if not min_close_date:
        return None
    return datetime.strptime(min_close_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _get(base_url: str, params: dict) -> dict:
    url = f"{base_url}{MARKETS_ENDPOINT}"
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


def fetch_settled_markets(
    limit: int = 200,
    min_close_date: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> list[dict]:
    """Page through Kalshi's /markets endpoint filtering to settled, binary
    markets that closed on/after min_close_date. Returns raw Kalshi records.
    """
    cutoff = _parse_close_date(min_close_date)
    out: list[dict] = []
    cursor = None

    while len(out) < limit:
        params = {"limit": PAGE_SIZE, "status": "settled"}
        if cursor:
            params["cursor"] = cursor

        payload = _get(base_url, params)
        markets = payload.get("markets", [])
        if not markets:
            break

        for m in markets:
            close_time_raw = m.get("close_time") or m.get("expiration_time")
            if not close_time_raw:
                continue
            try:
                close_dt = datetime.fromisoformat(close_time_raw.replace("Z", "+00:00"))
            except ValueError:
                continue

            if cutoff and close_dt < cutoff:
                continue
            if m.get("result") not in ("yes", "no"):
                continue  # unresolved or non-binary result

            out.append(m)
            if len(out) >= limit:
                break

        cursor = payload.get("cursor")
        if not cursor:
            break

    return out[:limit]


def to_experiment_format(markets: list[dict]) -> list[dict]:
    records = []
    for m in markets:
        last_price = m.get("last_price")
        if last_price is None:
            continue  # no trade data, can't establish a crowd baseline
        close_time_raw = m.get("close_time") or m.get("expiration_time") or ""
        records.append(
            {
                "id": m.get("ticker", ""),
                "question": m.get("title") or m.get("subtitle") or m.get("ticker", ""),
                "market_prob": round(last_price / 100.0, 4),
                "outcome": 1 if m.get("result") == "yes" else 0,
                "close_date": close_time_raw[:10],
                "context": None,
            }
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
    parser.add_argument("--base-url", type=str, default=DEFAULT_BASE_URL)
    parser.add_argument("--out", type=str, required=True, help="Output JSON path")
    args = parser.parse_args()

    print(f"Fetching up to {args.limit} settled Kalshi markets (min_close_date={args.min_close_date})...")
    raw = fetch_settled_markets(limit=args.limit, min_close_date=args.min_close_date, base_url=args.base_url)
    records = to_experiment_format(raw)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(records, f, indent=2)

    print(f"Wrote {len(records)} resolved binary markets to {out_path}")


if __name__ == "__main__":
    main()
