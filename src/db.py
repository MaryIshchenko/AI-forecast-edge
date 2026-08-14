"""Shared Postgres helpers for market-ingestion scripts (schema.sql).

Every *-to-db ingestion script (Manifold now; Kalshi/Polymarket later) should
import upsert_question/upsert_resolution from here rather than writing its
own SQL, so every source stays consistent with schema.sql and each other.
"""
from __future__ import annotations

import datetime
import os

try:
    import psycopg2
except ImportError as e:
    raise ImportError(
        "The 'psycopg2-binary' package is required for DB ingestion scripts. "
        "Install with: pip install -r requirements.txt"
    ) from e

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/ai_forecast_edge"


def get_connection(database_url: str | None = None):
    return psycopg2.connect(database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))


def upsert_question(
    conn,
    *,
    external_id: str,
    question: str,
    market_prob: float | None,
    close_date: datetime.date,
    category: str | None = None,
    volume: float | None = None,
    liquidity: float | None = None,
    context: str | None = None,
) -> str:
    """Insert or update a question by external_id. Returns its uuid."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into questions (external_id, question, context, market_prob, close_date, category, volume, liquidity)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (external_id) do update set
                question = excluded.question,
                context = excluded.context,
                market_prob = excluded.market_prob,
                close_date = excluded.close_date,
                category = excluded.category,
                volume = excluded.volume,
                liquidity = excluded.liquidity
            returning id
            """,
            (external_id, question, context, market_prob, close_date, category, volume, liquidity),
        )
        return cur.fetchone()[0]


def upsert_resolution(conn, *, question_id: str, outcome: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into resolutions (question_id, outcome)
            values (%s, %s)
            on conflict (question_id) do update set
                outcome = excluded.outcome,
                resolved_at = now()
            """,
            (question_id, outcome),
        )


def fetch_unpredicted_questions(
    conn, *, predictor: str, limit: int | None = None, min_close_date: str | None = None
) -> list[dict]:
    """Priced questions this predictor hasn't forecast yet, oldest close_date first.

    min_close_date (YYYY-MM-DD) is a leakage guard: only questions that close
    on/after this date, same semantics as kalshi_fetcher.py/manifold_fetcher.py's
    --min-close-date.
    """
    where = [
        "market_prob is not null",
        "id not in (select question_id from predictions where predictor = %s)",
    ]
    params: list = [predictor]
    if min_close_date:
        where.append("close_date >= %s")
        params.append(min_close_date)
    params.append(limit)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select id, question, close_date, market_prob
            from questions
            where {' and '.join(where)}
            order by close_date
            limit %s
            """,
            params,
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def insert_prediction(conn, *, question_id: str, predictor: str, probability: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into predictions (question_id, predictor, probability)
            values (%s, %s, %s)
            on conflict (question_id, predictor) do nothing
            """,
            (question_id, predictor, probability),
        )
