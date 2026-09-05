"""LLM clients for the Step 4 agentic loop, with budget accounting.

Two implementations behind one interface:

``AnthropicClient``
    Real Claude API calls. Every response's token usage is recorded and priced,
    and the client refuses to start a call that would exceed the configured
    dollar budget. This is the path the assignment describes.

``OfflineClient``
    No network. Replays the hand-written profile strategies from
    ``strategies/grammar_strategy.py`` so the surrounding pipeline (validate ->
    run -> summarize -> refine) can be exercised without an API key. Its
    "refinement" is the old heuristic profile switch, which is a much weaker
    signal than a real model -- runs made with it are labelled as such in the
    logs so they are never mistaken for a real agentic run.

Pricing note: ``PRICING`` holds USD per million tokens and is only used to
estimate spend against the assignment's budget cap. Verify the numbers against
current Anthropic pricing, or override them with ``--price-in``/``--price-out``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_MODEL = "claude-sonnet-5"

# USD per million tokens (input, output). Estimates for budget tracking only.
PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (15.00, 75.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}
FALLBACK_PRICE = (3.00, 15.00)


class BudgetExhausted(RuntimeError):
    """Raised when a call would push spend past the configured cap."""


@dataclass
class Ledger:
    """Running token/cost total across the whole loop."""

    price_in: float
    price_out: float
    budget_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    per_call: list[dict] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens * self.price_in + self.output_tokens * self.price_out
        ) / 1_000_000

    def record(self, label: str, input_tokens: int, output_tokens: int) -> dict:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.calls += 1
        entry = {
            "label": label,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cumulative_cost_usd": round(self.cost_usd, 4),
        }
        self.per_call.append(entry)
        return entry

    def check(self) -> None:
        if self.cost_usd >= self.budget_usd:
            raise BudgetExhausted(
                f"spend ${self.cost_usd:.2f} has reached the ${self.budget_usd:.2f} cap"
            )

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "estimated_cost_usd": round(self.cost_usd, 4),
            "budget_usd": self.budget_usd,
            "price_per_mtok_input": self.price_in,
            "price_per_mtok_output": self.price_out,
            "per_call": self.per_call,
        }


class AnthropicClient:
    """Claude API client that prices every call against a hard budget."""

    is_live = True

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 8000,
        budget_usd: float = 5.0,
        price_in: float | None = None,
        price_out: float | None = None,
        api_key: str | None = None,
    ) -> None:
        try:
            import anthropic
        except ImportError as error:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "the `anthropic` package is required for a live run; "
                "install it with `pip install anthropic`, or pass --offline"
            ) from error

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set; export it for a live run, "
                "or pass --offline to exercise the pipeline without the API"
            )

        default_in, default_out = PRICING.get(model, FALLBACK_PRICE)
        self.model = model
        self.max_tokens = max_tokens
        self.client = anthropic.Anthropic(api_key=key)
        self.ledger = Ledger(
            price_in=price_in if price_in is not None else default_in,
            price_out=price_out if price_out is not None else default_out,
            budget_usd=budget_usd,
        )

    def complete(self, prompt: str, label: str) -> str:
        self.ledger.check()
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=(
                "You write Hypothesis strategies for grammar-based fuzzing. "
                "You reply with exactly one fenced Python code block and no "
                "other text."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        self.ledger.record(
            label, response.usage.input_tokens, response.usage.output_tokens
        )
        return "".join(
            block.text for block in response.content if block.type == "text"
        )


class OfflineClient:
    """Deterministic stand-in that replays the hand-written profile strategies."""

    is_live = False
    model = "offline-profiles"

    #: Ordered fallbacks used when the heuristic has nothing better to say.
    ROTATION = ["seed", "valid-heavy", "deep-heavy", "adaptation-heavy", "malformed-heavy"]

    def __init__(self, budget_usd: float = 5.0, **_ignored) -> None:
        self.ledger = Ledger(price_in=0.0, price_out=0.0, budget_usd=budget_usd)
        self._index = 0
        self._last_summary: dict | None = None

    def observe(self, summary: dict) -> None:
        """Let the loop hand back the run summary that drives the next profile."""
        self._last_summary = summary

    def _next_profile(self) -> str:
        summary = self._last_summary
        if summary is None:
            return "seed"
        if summary.get("unique_crashes"):
            return "deep-heavy"
        if summary.get("acceptance_rate", 0.0) < 0.30:
            return "valid-heavy"
        if summary.get("productions_missing"):
            return "adaptation-heavy"
        self._index = (self._index + 1) % len(self.ROTATION)
        return self.ROTATION[self._index]

    def complete(self, prompt: str, label: str) -> str:
        if label == "repair":
            # The profile modules are known-good, so a repair request means the
            # loop hit a bug of its own. Return the safest profile.
            profile = "valid-heavy"
        else:
            profile = self._next_profile()
        self.ledger.record(label, 0, 0)
        return _offline_module(profile)


def _offline_module(profile: str) -> str:
    """Emit a contract-compliant module that delegates to a hand-written profile.

    The profile source is inlined rather than imported so that the generated
    module obeys the same "self-contained, no project imports" contract the
    live model is held to, and so the iteration log records the exact code that
    ran.
    """
    source = (
        Path(__file__).resolve().parents[1] / "strategies" / "grammar_strategy.py"
    ).read_text(encoding="utf-8")
    body = source.replace(
        'def json_text_strategy(profile: str = "seed")',
        "def _profile_strategy(profile: str)",
    )
    return f"""```python
# OFFLINE FALLBACK -- not model-generated. Profile: {profile}
{body}

def json_text_strategy():
    return _profile_strategy({profile!r})
```"""


def build_client(offline: bool, **kwargs):
    """Pick a client: the live Claude API, or the offline profile replay."""
    if offline:
        return OfflineClient(**kwargs)
    return AnthropicClient(**kwargs)
