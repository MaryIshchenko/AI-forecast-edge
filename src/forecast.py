"""The forecasting scaffold.

The "model" here is not trained weights — it's a scaffold around Claude:
ensemble of independent runs at temperature, median aggregation to cancel
out per-run noise, and a prompt that forces a calibrated 0-100 estimate
instead of a vague hedge. The edge (if any) lives in this scaffold.
"""
from __future__ import annotations

import os
import random
import re
import statistics
import sys
import time
from abc import ABC, abstractmethod

DEFAULT_MODEL = "claude-sonnet-5"
MAX_RETRIES = 5

PROMPT_TEMPLATE = """You are a professional forecaster. Estimate the probability that the \
following question resolves YES.

Question: {question}
{context_block}
Think briefly about base rates and the strongest evidence on each side, then \
commit to a single number. Do not hedge with a range.

Respond with EXACTLY one line in this format, nothing else:
PROBABILITY: <a number from 0 to 100>
"""

PROB_RE = re.compile(r"PROBABILITY:\s*([0-9]{1,3}(?:\.[0-9]+)?)", re.IGNORECASE)


class Forecaster(ABC):
    @abstractmethod
    def forecast(self, question: str, context: str | None = None) -> float:
        """Return a probability in [0, 1] that the question resolves YES."""


class MockForecaster(Forecaster):
    """Deterministic-ish fake forecaster with no real edge.

    Used by `--mock` so the pipeline is runnable with no API key. It should
    NOT beat the market — if it does, the test harness itself is broken.
    """

    def __init__(self, seed: int = 0, noise: float = 0.15):
        self._rng = random.Random(seed)
        self._noise = noise

    def forecast(self, question: str, context: str | None = None) -> float:
        base = 0.5
        jitter = self._rng.uniform(-self._noise, self._noise)
        return min(max(base + jitter, 0.01), 0.99)


class LLMForecaster(Forecaster):
    """Ensemble of independent Claude runs, aggregated by median.

    Median (not mean) so a single wild outlier run doesn't drag the estimate
    around — that's the calibration half of the scaffold.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        n_ensemble: int = 5,
        temperature: float = 1.0,
        api_key: str | None = None,
        retrieval: bool = False,
        # Must cover extended thinking too: at 300, a run hit max_tokens mid-thought
        # with zero output text (verified live) -- this needs real headroom.
        max_tokens: int = 2048,
    ):
        try:
            import anthropic
        except ImportError as e:
            raise ImportError(
                "The 'anthropic' package is required for LLMForecaster. "
                "Install with: pip install -r requirements.txt"
            ) from e

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError(
                "No ANTHROPIC_API_KEY found. Set it in .env or pass api_key=..."
            )

        self._client = anthropic.Anthropic(api_key=key)
        self._anthropic = anthropic  # kept for exception types in _single_run
        self.model = model
        self.n_ensemble = n_ensemble
        self.temperature = temperature
        self.retrieval = retrieval
        self.max_tokens = max_tokens

        if retrieval:
            # Retrieval is only honest for open questions where the future
            # genuinely isn't known. On a resolved-question backtest it's a
            # leakage channel: today's internet already knows the answer.
            raise NotImplementedError(
                "Retrieval/web-search is not wired up in this scaffold. "
                "See README: backtests must run with retrieval off."
            )

    def forecast(self, question: str, context: str | None = None) -> float:
        runs = [self._single_run(question, context) for _ in range(self.n_ensemble)]
        runs = [r for r in runs if r is not None]
        if not runs:
            raise RuntimeError(f"All {self.n_ensemble} ensemble runs failed to parse for: {question!r}")
        return statistics.median(runs)

    def _single_run(self, question: str, context: str | None) -> float | None:
        context_block = f"\nRelevant context: {context}\n" if context else ""
        prompt = PROMPT_TEMPLATE.format(question=question, context_block=context_block)

        response = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    messages=[{"role": "user", "content": prompt}],
                )
                break
            except self._anthropic.RateLimitError:
                wait = 2 ** attempt
                print(f"  rate limited, backing off {wait}s...", file=sys.stderr)
                time.sleep(wait)
            except self._anthropic.APIStatusError as e:
                if e.status_code < 500 or attempt == MAX_RETRIES:
                    raise
                time.sleep(2 ** attempt)
        if response is None:
            return None

        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        match = PROB_RE.search(text)
        if not match:
            return None
        pct = float(match.group(1))
        pct = min(max(pct, 0.0), 100.0)
        return pct / 100.0


def build_forecaster(mock: bool, **kwargs) -> Forecaster:
    if mock:
        return MockForecaster()
    return LLMForecaster(**kwargs)
