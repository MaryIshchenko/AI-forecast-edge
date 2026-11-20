#!/usr/bin/env bash
# Load out/bigquery/*.csv into BigQuery. Free tier: 10 GB storage + 1 TB queries/month;
# this dataset is well under 1 MB. Uses the BigQuery sandbox if no billing is attached.
#
#   gcloud auth login && gcloud config set project YOUR_PROJECT_ID
#   python src/export_bigquery.py
#   bash bigquery/load.sh            # optional: DATASET=other_name bash bigquery/load.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DATASET="${DATASET:-forecast_edge}"
DIR=out/bigquery
LOAD="bq load --replace --source_format=CSV --skip_leading_rows=1 --allow_quoted_newlines"

bq --location=US mk -f --dataset "$DATASET"

$LOAD "$DATASET.questions" "$DIR/questions.csv" \
  id:STRING,external_id:STRING,source:STRING,question:STRING,context:STRING,market_prob:FLOAT64,close_date:DATE,category:STRING,volume:FLOAT64,liquidity:FLOAT64,created_at:TIMESTAMP
$LOAD "$DATASET.predictions" "$DIR/predictions.csv" \
  id:STRING,question_id:STRING,predictor:STRING,probability:FLOAT64,created_at:TIMESTAMP
$LOAD "$DATASET.resolutions" "$DIR/resolutions.csv" \
  question_id:STRING,outcome:INT64,resolved_at:TIMESTAMP
$LOAD "$DATASET.forecasts" "$DIR/forecasts.csv" \
  question_id:STRING,source:STRING,category:STRING,question:STRING,close_date:DATE,predictor:STRING,forecast_at:TIMESTAMP,ai_prob:FLOAT64,market_prob:FLOAT64,outcome:INT64,ai_brier:FLOAT64,market_brier:FLOAT64,ai_beats_market:INT64,ai_bucket:FLOAT64,volume:FLOAT64

sed "s/forecast_edge\./$DATASET./g" bigquery/views.sql | bq query --use_legacy_sql=false
echo "Loaded. Try: bq query --use_legacy_sql=false 'SELECT * FROM $DATASET.leaderboard'"
