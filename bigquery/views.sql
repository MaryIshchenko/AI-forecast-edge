-- BigQuery views over the exported tables. Run after load.sh:
--   bq query --use_legacy_sql=false < bigquery/views.sql
-- Replace `forecast_edge` if you used a different dataset name.

-- Port of schema.sql's Postgres `leaderboard` view, plus the crowd as a "predictor"
-- so AI and market sit side by side on one scoreboard.
CREATE OR REPLACE VIEW forecast_edge.leaderboard AS
SELECT predictor, COUNT(*) AS n_forecasts, AVG(ai_brier) AS brier_score
FROM forecast_edge.forecasts GROUP BY predictor
UNION ALL
SELECT 'market (crowd)', COUNT(*), AVG(market_brier)
FROM forecast_edge.forecasts WHERE market_brier IS NOT NULL;

-- Calibration: is a "70%" really a 70%? On-diagonal = calibrated.
CREATE OR REPLACE VIEW forecast_edge.calibration AS
SELECT ai_bucket, COUNT(*) AS n, AVG(ai_prob) AS mean_predicted, AVG(outcome) AS observed_rate
FROM forecast_edge.forecasts GROUP BY ai_bucket;

-- Brier gap by category (positive = AI beat the crowd). Small-n categories are noise.
CREATE OR REPLACE VIEW forecast_edge.edge_by_category AS
SELECT COALESCE(category, '(none)') AS category, COUNT(*) AS n,
       AVG(market_brier) - AVG(ai_brier) AS brier_gap,
       AVG(ai_beats_market) AS ai_win_rate
FROM forecast_edge.forecasts WHERE market_brier IS NOT NULL
GROUP BY category;

-- Leakage check: questions that closed before the model's assumed training cutoff
-- (2026-02-01, the same guard the fetchers use) may be recalled, not forecast.
CREATE OR REPLACE VIEW forecast_edge.edge_by_leakage_window AS
SELECT IF(close_date < DATE '2026-02-01', 'before cutoff (leak risk)', 'after cutoff') AS close_window,
       COUNT(*) AS n, AVG(ai_brier) AS ai_brier, AVG(market_brier) AS market_brier
FROM forecast_edge.forecasts WHERE market_brier IS NOT NULL
GROUP BY close_window;
