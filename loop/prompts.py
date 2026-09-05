"""Prompt templates for the Step 4 agentic loop.

Three prompts drive the loop:

* ``seed_prompt``       - iteration 1, grammar in / strategy out.
* ``refinement_prompt`` - iterations 2..N, feedback in / revised strategy out.
* ``repair_prompt``     - out-of-band, used when a returned strategy fails
                          validation (bad code, or rejected at the front door).

All three demand the same module contract so the loop can load whatever comes
back without special-casing each iteration.
"""

from __future__ import annotations

import json


MODULE_CONTRACT = '''\
Return ONE fenced ```python block and nothing else. No prose before or after.

The module must satisfy this contract exactly:

* It defines `json_text_strategy()` taking no arguments and returning a
  `hypothesis.strategies.SearchStrategy[str]`. Every drawn value MUST be a
  `str` -- the harness encodes it to UTF-8 itself, so do not return bytes.
* It imports only `hypothesis` (and `hypothesis.strategies as st`) plus the
  Python standard library modules `string`, `math`, and `re`.
* It performs NO file, network, subprocess, or environment access at import
  time or draw time. It must not read or write anything.
* It is self-contained: no imports from this project, no globals mutated
  across draws, no reliance on module state.
* Top-level code must be cheap. The strategy is built once and reused for
  hundreds of draws.
'''

GENERATION_REQUIREMENTS = '''\
Requirements on the strategy itself:

1. Model the grammar's recursive productions with `st.recursive` and/or
   `@st.composite`. Do NOT flatten `value -> obj | arr` into a fixed number of
   hand-written nesting levels, and do not build JSON by calling
   `json.dumps` on a Python object -- generate the text from the grammar's
   productions so that malformed and near-valid forms are reachable.
2. Cover these explicitly, each reachable with non-trivial probability:
   - empty containers: `{}` and `[]`
   - deep nesting (well past the depth an ordinary document reaches)
   - duplicate keys within one object
   - extreme numeric values: exponent overflow/underflow, very long digit
     runs, `-0`, leading-zero forms, huge and tiny magnitudes
   - unicode and escapes: `\\uXXXX`, surrogate pairs, lone surrogates,
     escaped control characters, the full `ESC` set from the grammar
   - near-valid-but-malformed inputs: trailing commas, missing colons,
     unterminated strings, trailing garbage after a complete value,
     mismatched brackets
3. Keep individual generated documents bounded in size. A single input should
   stay well under 100 KB; the run has a 5 second per-input timeout and a
   10 minute wall-clock cap, and a strategy that spends the whole budget
   serializing megabyte inputs tests nothing.
'''


def seed_prompt(grammar: str, adaptations: str) -> str:
    """Iteration 1: ANTLR grammar plus documented adaptations in, strategy out."""
    return f"""You are writing a Hypothesis strategy to fuzz the Parson JSON parser
(a small C library) via a sanitizer-instrumented harness.

Here is the ANTLR grammar for the input format, taken from the
antlr/grammars-v4 repository:

```antlr
{grammar}
```

The target library does not accept exactly the language of that grammar. The
following differences were measured against the pinned build, and the
generator should exploit them -- the gaps between the formal grammar and the
real parser are where parsing bugs tend to live:

```markdown
{adaptations}
```

{GENERATION_REQUIREMENTS}
{MODULE_CONTRACT}"""


def refinement_prompt(
    iteration: int,
    grammar: str,
    adaptations: str,
    current_code: str,
    summary: dict,
    history: list[dict],
) -> str:
    """Iterations 2..N: last run's feedback plus current code in, revision out."""
    history_lines = "\n".join(
        f"- iteration {item['iteration']}: score={item['score']:.1f} "
        f"acceptance={item['acceptance_rate']:.1%} "
        f"productions={item['production_coverage']:.0%} "
        f"depth_entropy={item['depth_entropy']:.2f} "
        f"unique_crashes={item['unique_crashes']}"
        for item in history
    )

    return f"""You are refining a Hypothesis strategy that fuzzes the Parson JSON parser.
This is iteration {iteration}.

There is no coverage instrumentation available. The loop steers on this proxy
signal instead, and your revision is kept only if it raises the score:

    score = 100 * unique_crash_signatures
          +  40 * grammar_production_coverage   (fraction of tracked productions seen)
          +  30 * depth_entropy                 (normalised spread of nesting depths)
          +  30 * acceptance_band               (1.0 when 30% <= acceptance <= 90%,
                                                 falling off sharply outside that band)

The acceptance band matters: a generator the parser rejects at the front door
never reaches the code that crashes, and one that is accepted every time is
only producing textbook-valid JSON. Aim to sit inside the band while pushing
coverage and depth spread up.

Score history so far:
{history_lines or "- (none yet)"}

Results from the most recent run:

```json
{json.dumps(summary, indent=2, ensure_ascii=False)[:12000]}
```

The strategy that produced those results:

```python
{current_code}
```

For reference, the grammar being modelled:

```antlr
{grammar}
```

And the measured grammar-vs-Parson differences:

```markdown
{adaptations}
```

Revise the strategy. Concretely, consider:

* Productions listed in `productions_missing` are never being generated at
  all -- reach them.
* If `acceptance_rate` is below 30%, the malformed branches are crowding out
  the valid ones; rebalance the weights toward grammar-valid text.
* If `acceptance_rate` is above 90%, you are only producing clean JSON;
  push harder on the near-valid boundary and on the documented adaptations.
* If the depth distribution is concentrated in one bucket, widen it.
* Where a crash signature was already found, generate more inputs in that
  neighbourhood -- the first crash of a kind is rarely the only one.
* Read the `parser_message_samples` to see what is being rejected and why.

{GENERATION_REQUIREMENTS}
{MODULE_CONTRACT}"""


def repair_prompt(current_code: str, problem: str, detail: str) -> str:
    """Out-of-band correction when a returned strategy fails validation."""
    return f"""The strategy you just returned failed validation before it could be run
against the target. It was not executed against the parser.

Problem: {problem}

Detail:
```
{detail[:4000]}
```

The strategy that failed:

```python
{current_code}
```

Fix the specific problem above. Keep everything that was working; change only
what is needed.

{MODULE_CONTRACT}"""
