"""Orchestrates the go/no-go test: does the AI forecast beat the crowd?

    python src/run_experiment.py --mock
    python src/run_experiment.py --data data/your_resolved.json --n-ensemble 5
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from data_loader import load_resolved
from forecast import DEFAULT_MODEL, build_forecaster
from scoring import bootstrap_brier_gap, brier_score, calibration, log_loss, verdict

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mock", action="store_true", help="Use MockForecaster + bundled sample data, no API key needed")
    p.add_argument("--data", type=str, default=None, help="Path to resolved-questions JSON")
    p.add_argument("--n-ensemble", type=int, default=5, help="Number of independent LLM runs per question")
    p.add_argument("--model", type=str, default=DEFAULT_MODEL)
    p.add_argument("--out", type=str, default="out", help="Output directory")
    return p.parse_args()


def run(args):
    data_path = args.data or "data/sample_resolved.json"
    questions = load_resolved(data_path)
    print(f"Loaded {len(questions)} resolved questions from {data_path}")

    if args.mock:
        forecaster = build_forecaster(mock=True)
        print("Forecaster: MockForecaster (no real edge expected — this is the harness sanity check)")
    else:
        forecaster = build_forecaster(mock=False, model=args.model, n_ensemble=args.n_ensemble)
        print(f"Forecaster: LLMForecaster(model={args.model}, n_ensemble={args.n_ensemble})")

    rows = []
    for i, q in enumerate(questions, 1):
        ai_prob = forecaster.forecast(q["question"], context=q.get("context"))
        rows.append(
            {
                "id": q["id"],
                "question": q["question"],
                "market_prob": q["market_prob"],
                "ai_prob": ai_prob,
                "outcome": q["outcome"],
                "close_date": q.get("close_date", ""),
            }
        )
        print(f"  [{i}/{len(questions)}] {q['id']}: market={q['market_prob']:.2f} ai={ai_prob:.2f} outcome={q['outcome']}")

    ai_probs = [r["ai_prob"] for r in rows]
    market_probs = [r["market_prob"] for r in rows]
    outcomes = [r["outcome"] for r in rows]

    ai_brier = brier_score(ai_probs, outcomes)
    market_brier = brier_score(market_probs, outcomes)
    ai_logloss = log_loss(ai_probs, outcomes)
    market_logloss = log_loss(market_probs, outcomes)
    cal = calibration(ai_probs, outcomes)
    point, ci_lo, ci_hi = bootstrap_brier_gap(ai_probs, market_probs, outcomes)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / "results.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "question", "market_prob", "ai_prob", "outcome", "close_date"])
        writer.writeheader()
        writer.writerows(rows)

    _write_calibration_plot(cal, out_dir / "calibration.png")

    print("\n" + "=" * 60)
    print(f"Brier score   AI: {ai_brier:.4f}   Market: {market_brier:.4f}")
    print(f"Log loss      AI: {ai_logloss:.4f}   Market: {market_logloss:.4f}")
    print(f"Calibration error (ECE), AI: {cal.ece:.4f}")
    print(f"Bootstrap Brier gap (market - AI): {point:+.4f}  90% CI [{ci_lo:+.4f}, {ci_hi:+.4f}]")
    print(f"\n{verdict(ci_lo, ci_hi)}")
    print("=" * 60)
    print(f"\nWrote {csv_path} and {out_dir / 'calibration.png'}")


def _write_calibration_plot(cal, path: Path):
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed, skipping calibration.png (pip install -r requirements.txt)", file=sys.stderr)
        return

    fig, ax = plt.subplots(figsize=(5, 5))
    ax.plot([0, 1], [0, 1], linestyle="--", color="gray", label="perfectly calibrated")

    xs = [b.mean_predicted for b in cal.bins if b.mean_predicted is not None]
    ys = [b.mean_actual for b in cal.bins if b.mean_actual is not None]
    sizes = [20 + 200 * (b.n / max(1, sum(bb.n for bb in cal.bins))) for b in cal.bins if b.mean_predicted is not None]
    ax.scatter(xs, ys, s=sizes, color="C0", zorder=3, label="AI forecaster")

    ax.set_xlabel("Mean predicted probability")
    ax.set_ylabel("Actual frequency of YES")
    ax.set_title(f"Calibration (ECE = {cal.ece:.3f})")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    run(parse_args())
