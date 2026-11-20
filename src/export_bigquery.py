"""Export the Postgres backup to CSVs ready for BigQuery (and Looker Studio).

Reads backups/db_backup.sql (the pg_dump written by backup_db.py) directly,
so no running database is needed. Writes to out/bigquery/:

    questions.csv     one row per question (mirrors the Postgres table + a `source` column)
    predictions.csv   one row per forecast
    resolutions.csv   one row per resolved question
    forecasts.csv     denormalized: one row per (AI-forecast, resolved) question with
                      ai_prob, market_prob, outcome and per-row Brier scores.
                      This is the table to point Looker Studio at.

Usage:
    python src/export_bigquery.py
    python src/export_bigquery.py --dump backups/db_backup.sql --out-dir out/bigquery
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

COPY_RE = re.compile(r"^COPY public\.(\w+) \(([^)]*)\) FROM stdin;$")
ESCAPES = {"\\t": "\t", "\\n": "\n", "\\r": "\r", "\\\\": "\\"}


def _unescape(field: str):
    if field == "\\N":
        return None
    return re.sub(r"\\[tnr\\]", lambda m: ESCAPES[m.group(0)], field)


def parse_dump(path: Path) -> dict[str, list[dict]]:
    """Return {table_name: [row_dict, ...]} for every COPY block in a plain pg_dump."""
    tables: dict[str, list[dict]] = {}
    current, cols = None, []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if current is None:
                m = COPY_RE.match(line)
                if m:
                    current = m.group(1)
                    cols = [c.strip() for c in m.group(2).split(",")]
                    tables[current] = []
                continue
            if line == "\\.":
                current = None
                continue
            values = [_unescape(v) for v in line.split("\t")]
            tables[current].append(dict(zip(cols, values)))
    return tables


def infer_source(external_id: str | None) -> str:
    """Which platform a question came from, based on its external_id shape."""
    if not external_id:
        return "user"
    if external_id.startswith("KX") or "-" in external_id:
        return "kalshi"
    if external_id.isdigit():
        return "polymarket"
    return "manifold"


def write_csv(path: Path, rows: list[dict], fields: list[str]):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r[k]) for k in fields})


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dump", default="backups/db_backup.sql")
    p.add_argument("--out-dir", default="out/bigquery")
    args = p.parse_args()

    tables = parse_dump(Path(args.dump))
    questions = tables.get("questions", [])
    predictions = tables.get("predictions", [])
    resolutions = tables.get("resolutions", [])

    for q in questions:
        q["source"] = infer_source(q.get("external_id"))
        # Postgres timestamptz text ("2026-08-09 18:58:39.10111+00") -> BigQuery-friendly
        if q.get("created_at"):
            q["created_at"] = q["created_at"].replace("+00", "+00:00")
    for r in predictions + resolutions:
        for k in ("created_at", "resolved_at"):
            if r.get(k):
                r[k] = r[k].replace("+00", "+00:00")

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    q_fields = ["id", "external_id", "source", "question", "context", "market_prob",
                "close_date", "category", "volume", "liquidity", "created_at"]
    write_csv(out / "questions.csv", questions, q_fields)
    write_csv(out / "predictions.csv", predictions, ["id", "question_id", "predictor", "probability", "created_at"])
    write_csv(out / "resolutions.csv", resolutions, ["question_id", "outcome", "resolved_at"])

    # Denormalized table for dashboards.
    q_by_id = {q["id"]: q for q in questions}
    res_by_id = {r["question_id"]: r for r in resolutions}
    flat = []
    for pr in predictions:
        q, res = q_by_id.get(pr["question_id"]), res_by_id.get(pr["question_id"])
        if not q or not res:
            continue
        ai, y = float(pr["probability"]), int(res["outcome"])
        mp = float(q["market_prob"]) if q.get("market_prob") is not None else None
        flat.append({
            "question_id": q["id"], "source": q["source"], "category": q.get("category"),
            "question": q["question"], "close_date": q["close_date"], "predictor": pr["predictor"],
            "forecast_at": pr["created_at"], "ai_prob": ai, "market_prob": mp, "outcome": y,
            "ai_brier": round((ai - y) ** 2, 6),
            "market_brier": None if mp is None else round((mp - y) ** 2, 6),
            "ai_beats_market": None if mp is None else int((ai - y) ** 2 < (mp - y) ** 2),
            "ai_bucket": min(int(ai * 10), 9) / 10,  # 0.0..0.9, for calibration charts
            "volume": q.get("volume"),
        })
    flat_fields = ["question_id", "source", "category", "question", "close_date", "predictor",
                   "forecast_at", "ai_prob", "market_prob", "outcome", "ai_brier", "market_brier",
                   "ai_beats_market", "ai_bucket", "volume"]
    write_csv(out / "forecasts.csv", flat, flat_fields)

    print(f"questions={len(questions)} predictions={len(predictions)} "
          f"resolutions={len(resolutions)} forecasts(flat)={len(flat)} -> {out}/")


if __name__ == "__main__":
    main()
