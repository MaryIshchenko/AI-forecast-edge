"""Pull closed, binary (Yes/No) Polymarket markets into the
questions/resolutions tables defined by schema.sql.

Same live API-handling as polymarket_fetcher.py (Gamma for discovery, CLOB
for pre-close price, since a closed market's outcomePrices is the settled
answer, not the crowd's belief) -- ported here rather than rewritten. The
difference is the sink: straight into Postgres via src/db.py, not a JSON
file. context is always null (matches manifold_fetcher.py/kalshi_db_fetcher.py).

Unlike Manifold, Polymarket's Gamma /markets response already includes
`category` and `liquidityNum` directly -- no extra per-market call needed to
get them, they're captured in the same page already being fetched for
discovery.

Usage:
    python src/polymarket_db_fetcher.py --limit 400 --min-close-date 2026-02-01
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

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
    sorting by volume skews the sample toward blowout "sure thing" markets,
    which would make any Brier comparison look artificially good or bad.
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
                    "category": m.get("category"),
                    "volume": volume,
                    "liquidity": m.get("liquidityNum"),
                }
            )
            if len(out) >= limit:
                break

        offset += PAGE_SIZE
        if len(page) < PAGE_SIZE:
            break

    return out[:limit]


def pre_close_yes_price(
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


def to_row(c: dict, lookback_hours: int, fidelity_minutes: int, clob_base_url: str) -> dict:
    prob = pre_close_yes_price(c["yes_token_id"], c["close_dt"], lookback_hours, fidelity_minutes, clob_base_url)
    return {
        "external_id": c["id"],
        "question": c["question"],
        "market_prob": None if prob is None else round(min(max(prob, 0.0), 1.0), 4),
        "close_date": c["close_dt"].date(),
        "category": c.get("category"),
        "volume": c.get("volume"),
        "liquidity": c.get("liquidity"),
        "outcome": 1 if c["yes_price_final"] >= 0.5 else 0,
    }


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
        help="Gamma sort field (default closedTime = most recently resolved first)",
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=24,
        help="Sample the crowd price this many hours before market close",
    )
    parser.add_argument("--fidelity-minutes", type=int, default=60, help="CLOB price-history bucket size")
    parser.add_argument("--gamma-base-url", type=str, default=GAMMA_BASE_URL)
    parser.add_argument("--clob-base-url", type=str, default=CLOB_BASE_URL)
    parser.add_argument(
        "--database-url", type=str, default=None,
        help="Postgres connection string. Defaults to $DATABASE_URL or the local dev DB",
    )
    args = parser.parse_args()

    print(f"Fetching up to {args.limit} closed binary Polymarket markets (min_close_date={args.min_close_date})...")
    candidates = fetch_closed_binary_markets(
        limit=args.limit,
        min_close_date=args.min_close_date,
        min_volume=args.min_volume,
        order=args.order,
        gamma_base_url=args.gamma_base_url,
    )
    if not candidates:
        print("No markets survived filtering.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(candidates)} candidate markets, pulling pre-close prices...")
    conn = db.get_connection(args.database_url)
    n_priced = 0
    try:
        for i, c in enumerate(candidates, 1):
            row = to_row(c, args.lookback_hours, args.fidelity_minutes, args.clob_base_url)
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
                print(f"  wrote {i}/{len(candidates)} markets")
    finally:
        conn.close()

    print(f"\nWrote {len(candidates)} resolved markets to questions/resolutions ({n_priced} with a priced market_prob)")


if __name__ == "__main__":
    main()
