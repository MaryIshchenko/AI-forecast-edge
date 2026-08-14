-- Supabase schema for the v0 "AI vs You" app.
-- Free-to-play only (no money in or out) until the edge is proven and the
-- legal conversation about real-money structure has happened.

create extension if not exists "pgcrypto";

create table if not exists questions (
    id            uuid primary key default gen_random_uuid(),
    external_id   text unique,               -- e.g. Kalshi ticker, or null for user-authored questions
    question      text not null,
    context       text,
    market_prob   numeric(5, 4),             -- crowd baseline at time of question creation, null if none
    close_date    date not null,
    category      text,                      -- source-provided topic/category (e.g. Manifold group slug)
    volume        numeric,                   -- trading volume in the source's native unit
    liquidity     numeric,                   -- source-provided liquidity measure, null if not applicable
    created_at    timestamptz not null default now()
);

-- Additive migration: safe to re-run against a DB created before these columns existed.
alter table questions add column if not exists category text;
alter table questions add column if not exists volume numeric;
alter table questions add column if not exists liquidity numeric;

create table if not exists predictions (
    id            uuid primary key default gen_random_uuid(),
    question_id   uuid not null references questions(id) on delete cascade,
    predictor     text not null,             -- 'ai' or a human user id
    probability   numeric(5, 4) not null check (probability >= 0 and probability <= 1),
    created_at    timestamptz not null default now(),
    unique (question_id, predictor)
);

create table if not exists resolutions (
    question_id   uuid primary key references questions(id) on delete cascade,
    outcome       smallint not null check (outcome in (0, 1)),
    resolved_at   timestamptz not null default now()
);

-- Per-predictor Brier score across every resolved question they forecast.
-- This view is the public "AI vs humans" scoreboard.
create or replace view leaderboard as
select
    p.predictor,
    count(*)                                              as n_forecasts,
    avg((p.probability - r.outcome) ^ 2)                  as brier_score,
    avg(
        - (r.outcome * ln(greatest(p.probability, 1e-15))
           + (1 - r.outcome) * ln(greatest(1 - p.probability, 1e-15)))
    )                                                       as log_loss
from predictions p
join resolutions r on r.question_id = p.question_id
group by p.predictor
order by brier_score asc;
