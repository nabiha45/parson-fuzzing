"""Extract, sandbox-check, and load a Hypothesis strategy returned by the LLM.

This is Step 4.2 ("Validate the generator itself") applied to the code itself,
before the acceptance-rate spot-check in ``agent_loop.py`` runs it against the
parser: a response that isn't one clean code block, imports something outside
the allowed set, or doesn't produce a string-valued strategy is rejected here
so it never reaches a subprocess.
"""

from __future__ import annotations

import ast
import re
import types
from dataclasses import dataclass

from hypothesis import strategies as st


CODE_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)
ALLOWED_TOP_LEVEL_IMPORTS = {"hypothesis", "string", "math", "re", "__future__"}


class ValidationError(RuntimeError):
    pass


@dataclass
class ValidationResult:
    ok: bool
    code: str
    strategy: object | None
    problem: str | None
    detail: str


def extract_code(response: str) -> str:
    match = CODE_FENCE.search(response)
    if not match:
        raise ValidationError("no fenced ```python code block found in the response")
    return match.group(1).strip()


def check_imports(code: str) -> None:
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module] if node.module else []
        else:
            continue
        for name in names:
            top = (name or "").split(".")[0]
            if top not in ALLOWED_TOP_LEVEL_IMPORTS:
                raise ValidationError(f"disallowed import: {name!r}")


def load_module(code: str) -> types.ModuleType:
    module = types.ModuleType("generated_strategy")
    exec(compile(code, "<generated_strategy>", "exec"), module.__dict__)
    return module


def validate(response: str, spot_checks: int = 20) -> ValidationResult:
    try:
        code = extract_code(response)
    except ValidationError as error:
        return ValidationResult(False, "", None, "no_code_block", str(error))

    try:
        check_imports(code)
    except (ValidationError, SyntaxError) as error:
        return ValidationResult(False, code, None, "disallowed_or_invalid_syntax", str(error))

    try:
        module = load_module(code)
    except Exception as error:  # noqa: BLE001 - surfaced to the repair prompt, not swallowed
        return ValidationResult(False, code, None, "import_error", f"{type(error).__name__}: {error}")

    if not hasattr(module, "json_text_strategy"):
        return ValidationResult(
            False, code, None, "missing_entry_point", "module has no json_text_strategy()"
        )

    try:
        strategy = module.json_text_strategy()
    except Exception as error:  # noqa: BLE001
        return ValidationResult(False, code, None, "entry_point_raised", f"{type(error).__name__}: {error}")

    if not isinstance(strategy, st.SearchStrategy):
        return ValidationResult(
            False,
            code,
            None,
            "wrong_return_type",
            f"json_text_strategy() returned {type(strategy).__name__}, expected a SearchStrategy",
        )

    samples = []
    try:
        for _ in range(spot_checks):
            example = strategy.example()
            if not isinstance(example, str):
                return ValidationResult(
                    False, code, None, "non_string_example", f"got {type(example).__name__}: {example!r}"
                )
            samples.append(example)
    except Exception as error:  # noqa: BLE001
        return ValidationResult(False, code, None, "example_raised", f"{type(error).__name__}: {error}")

    preview = "; ".join(repr(sample)[:80] for sample in samples[:5])
    return ValidationResult(True, code, strategy, None, preview)
