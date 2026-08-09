"""Load resolved questions for the backtest.

Expected record shape (see data/sample_resolved.json):
    {
        "id": str,
        "question": str,
        "market_prob": float,   # crowd baseline, in [0, 1]
        "outcome": int,         # 0 or 1, ground truth
        "close_date": "YYYY-MM-DD",
        "context": str | null   # optional extra context passed to the LLM
    }
"""
from __future__ import annotations

import json
from pathlib import Path

REQUIRED_FIELDS = ("id", "question", "market_prob", "outcome")


def load_resolved(path: str | Path) -> list[dict]:
    path = Path(path)
    with path.open() as f:
        records = json.load(f)

    if not isinstance(records, list):
        raise ValueError(f"{path} must contain a JSON array of question records")

    for i, r in enumerate(records):
        missing = [f for f in REQUIRED_FIELDS if f not in r]
        if missing:
            raise ValueError(f"{path}: record {i} missing required field(s) {missing}: {r}")
        if r["outcome"] not in (0, 1):
            raise ValueError(f"{path}: record {i} outcome must be 0 or 1, got {r['outcome']!r}")
        if not (0.0 <= r["market_prob"] <= 1.0):
            raise ValueError(f"{path}: record {i} market_prob must be in [0, 1], got {r['market_prob']!r}")

    return records


def fetch_polymarket_resolved(limit: int = 200, min_close_date: str | None = None) -> list[dict]:
    """Superseded by src/polymarket_fetcher.py (a standalone CLI, same pattern
    as kalshi_fetcher.py) — run that script and pass its output file to
    load_resolved() instead of calling this function.
    """
    raise NotImplementedError(
        "Use src/polymarket_fetcher.py directly: "
        "python src/polymarket_fetcher.py --limit 200 --out data/polymarket_resolved.json"
    )
