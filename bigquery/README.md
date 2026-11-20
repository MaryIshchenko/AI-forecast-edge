# BigQuery + Looker Studio

This puts the forecasting results in BigQuery and puts a Looker Studio dashboard on top. Both run on free tiers: the BigQuery sandbox needs no credit card, and Looker Studio is free.

## What gets loaded

`src/export_bigquery.py` reads `backups/db_backup.sql` directly, so you don't need Postgres or Docker running.

| Table | Rows | What it is |
|---|---|---|
| `questions` | 800 | Manifold questions with crowd price, category, volume |
| `predictions` | 300 | `claude-sonnet-5` forecasts |
| `resolutions` | 800 | outcomes |
| `forecasts` | 300 | denormalized: ai_prob, market_prob, outcome, both Brier scores. **Point Looker Studio here.** |

Views (`views.sql`): `leaderboard` (a port of the Postgres view, with the crowd added as a row), `calibration`, `edge_by_category`, `edge_by_leakage_window`.

## 1. One-time setup (~15 min)

```bash
brew install --cask google-cloud-sdk
gcloud auth login
```

Next, go to https://console.cloud.google.com, create a project (e.g. `forecast-edge`), and open BigQuery once so the sandbox gets enabled. Then:

```bash
gcloud config set project YOUR_PROJECT_ID
```

## 2. Load

```bash
python src/export_bigquery.py
bash bigquery/load.sh
bq query --use_legacy_sql=false 'SELECT * FROM forecast_edge.leaderboard'
```

## 3. Looker Studio dashboard (~1 hour)

Go to https://lookerstudio.google.com, then **Create → Report → BigQuery**, pick your project, then `forecast_edge` → `forecasts`. Add the views as extra data sources with **Resource → Manage added data sources**.

Suggested one-page layout:

1. **Scorecards:** AVG `ai_brier`, AVG `market_brier`, COUNT of questions, AVG `ai_beats_market` (win rate).
2. **Bar chart** from `leaderboard`: predictor vs brier_score (lower is better).
3. **Scatter** from `calibration`: x = `mean_predicted`, y = `observed_rate`, bubble size = `n`. Add a y=x reference line.
4. **Bar chart** from `edge_by_category`: `brier_gap` by category. Filter to `n >= 10`, since smaller categories are noise.
5. **Table** from `edge_by_leakage_window`: the leakage check (see below).
6. **Controls:** a date-range control on `close_date` and a drop-down on `category`.
7. **Table** of the worst misses: question, ai_prob, market_prob, outcome, sorted by `ai_brier` descending.

Set sharing to "Anyone with the link can view" and put the link on your resume or GitHub.

### What the data says right now

Be ready to explain this in an interview:

- The AI is **losing**. Its Brier score is 0.319 against the crowd's 0.127 over 300 questions.
- Leakage shows up in the numbers. On the 16 questions that closed **before** the assumed 2026-02-01 cutoff, the AI scores 0.168. On the 284 that closed after, it scores 0.328. The AI looking good only on questions it might remember is exactly the false positive this project is built to catch.

## Resume line (true once steps 1–3 are done)

> Built an evaluation pipeline that benchmarks LLM forecasts against prediction-market crowds on 300+ resolved questions; loaded results into **BigQuery** (SQL views for Brier leaderboard, calibration, leakage checks) and published a **Looker Studio** dashboard.

## Associate Cloud Engineer study plan (~4 weeks, ~1 hr/day)

The exam costs $125, has 50–60 questions, runs 2 hours, and can be taken online with a proctor. Check the current details on the official exam guide page.

| Week | Focus | Do |
|---|---|---|
| 1 | Resource hierarchy, IAM, billing, `gcloud` | Google Cloud Skills Boost "Associate Cloud Engineer" learning path; set a budget alert on your project |
| 2 | Compute: Compute Engine, GKE, Cloud Run, App Engine; VPC, firewall rules, load balancers | Labs: deploy a VM, a Cloud Run service, a small GKE cluster (delete after) |
| 3 | Storage and data: Cloud Storage classes, Cloud SQL, Spanner, Bigtable, Firestore, **BigQuery**; logging and monitoring | Reuse this project: `bq` CLI, scheduled query, IAM on a dataset |
| 4 | Practice exams, weak spots | Google's official sample questions plus one paid practice set; book the exam when you score 80%+ |

Hands-on labs cost money beyond the free tier. Use the $300 new-account trial credit, and delete clusters and VMs the same day.
