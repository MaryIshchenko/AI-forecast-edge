"""Phase 2 forecasting harness: ask an AI model for a probability on every DB
question that has a priced market_prob, and record it in predictions.

Reuses the exact scaffold from forecast.py (ensemble of independent Claude
runs, median aggregation) -- the "model" is that scaffold, not new prompt
logic, so results here are comparable to run_experiment.py's local backtest.

LEAKAGE NOTE: unlike run_experiment.py's --data path (typically fed
leakage-guarded JSON), this reads directly from the live questions table,
which currently includes markets that closed before the model's training
cutoff (see CLAUDE.md / ROADMAP.md's anti-leakage rules -- rule 2 isn't
enforced by this script). Treat early results as a mechanics check, not a
real edge measurement, until the dataset is filtered to close_date-after-
cutoff questions.

Sends ONLY the question text and close_date to the model -- never the
outcome (this script never selects it) and never questions.context.

Resumable: re-running skips any (question, predictor) pair already in
predictions, so an interrupted run picks back up where it left off.

Usage:
    python src/predict.py --mock --limit 5       # no API key, plumbing check only
    python src/predict.py --limit 10              # small real test batch
    python src/predict.py                          # everything left to predict
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402
from forecast import DEFAULT_MODEL, build_forecaster  # noqa: E402

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="Max questions to predict this run (default: all remaining)")
    parser.add_argument(
        "--min-close-date", type=str, default=None,
        help="YYYY-MM-DD leakage guard: only questions that close on/after this date",
    )
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL)
    parser.add_argument("--n-ensemble", type=int, default=5, help="Independent LLM runs per question, aggregated by median")
    parser.add_argument("--predictor", type=str, default=None, help="predictions.predictor value (default: the model name)")
    parser.add_argument("--pause", type=float, default=0.5, help="Seconds to sleep between questions")
    parser.add_argument("--mock", action="store_true", help="Use MockForecaster -- no API key, plumbing check only")
    parser.add_argument("--database-url", type=str, default=None)
    args = parser.parse_args()

    predictor = args.predictor or args.model

    if args.mock:
        forecaster = build_forecaster(mock=True)
        print("Forecaster: MockForecaster (plumbing check only, not a real forecast)")
    else:
        forecaster = build_forecaster(mock=False, model=args.model, n_ensemble=args.n_ensemble)
        print(f"Forecaster: LLMForecaster(model={args.model}, n_ensemble={args.n_ensemble})")

    conn = db.get_connection(args.database_url)
    questions = db.fetch_unpredicted_questions(
        conn, predictor=predictor, limit=args.limit, min_close_date=args.min_close_date
    )
    print(f"{len(questions)} question(s) to predict as predictor={predictor!r}")
    if not questions:
        print("Nothing to do -- every priced question already has a prediction from this predictor.")
        conn.close()
        return

    failures = []
    print(f"{'question':<70} {'market':>8} {'ai':>8}")
    for i, q in enumerate(questions, 1):
        try:
            ai_prob = forecaster.forecast(
                q["question"], context=f"This question closes/resolves on {q['close_date']}."
            )
        except Exception as e:
            failures.append({"question": q["question"], "error": str(e)})
            print(f"[{i}/{len(questions)}] FAILED: {q['question'][:70]!r} -- {e}", file=sys.stderr)
        else:
            db.insert_prediction(conn, question_id=q["id"], predictor=predictor, probability=ai_prob)
            conn.commit()
            print(f"[{i}/{len(questions)}] {q['question'][:70]:<70} {q['market_prob']:>8.4f} {ai_prob:>8.4f}")
        if not args.mock and i < len(questions):
            time.sleep(args.pause)

    conn.close()
    n_written = len(questions) - len(failures)
    print(f"\nWrote {n_written} predictions as predictor={predictor!r}")
    if failures:
        print(f"{len(failures)} question(s) failed (still unpredicted -- a re-run will retry them):")
        for f in failures:
            print(f"  - {f['question'][:70]!r}: {f['error']}")


if __name__ == "__main__":
    main()
