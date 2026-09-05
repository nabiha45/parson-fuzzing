"""Step 4 agentic loop: an LLM turns the ANTLR grammar into a Hypothesis
strategy for fuzzing Parson, and refines it using feedback from each run.

Stages per iteration, matching the assignment's Step 4:

  1. seed / refine - ask the LLM for a strategy (iteration 1: from the
                      grammar and adaptations; iterations 2..N: from the
                      previous run's summary and current code).
  2. validate      - sandbox-check the returned module (loop/validate_strategy.py),
                      then spot-check its parser-acceptance rate before
                      spending the full example budget on it.
  3. run           - execute through the sanitizer harness for up to
                      --examples inputs, each under a --timeout second cap.
  4. summarize     - unique crash signatures, parser error samples, and
                      structural diversity (loop/summarize_results.py).
  5. refine        - feed the summary and current code back to the LLM.
  6. stop          - iteration cap or LLM budget, whichever comes first.

Optimization signal: with no coverage instrumentation available, each run is
scored as
    100 * unique_crash_signatures
  +  40 * grammar_production_coverage
  +  30 * normalised nesting-depth entropy
  +  30 * acceptance_band (peaks across 30-90% parser acceptance)
(see loop/summarize_results.py:score_run). A revision is kept only if its
score is >= the best score seen so far; otherwise the loop reverts to the
best-known strategy code before the next refinement request, so one bad
revision can't ratchet the generator backward for the rest of the budget.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from hypothesis import HealthCheck, given, settings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loop.llm_client import DEFAULT_MODEL, BudgetExhausted, build_client  # noqa: E402
from loop.prompts import refinement_prompt, repair_prompt, seed_prompt  # noqa: E402
from loop.summarize_results import summarize, write_markdown  # noqa: E402
from loop.validate_strategy import validate  # noqa: E402


SANITIZER_MARKERS = (
    b"AddressSanitizer",
    b"UndefinedBehaviorSanitizer",
    b"runtime error:",
    b"LeakSanitizer",
)


def default_harness() -> Path:
    if os.name == "nt":
        return ROOT / "build" / "parson_harness_native.exe"
    return ROOT / "build" / "parson_harness"


def classify(returncode: int | None, stderr: bytes, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    if any(marker in stderr for marker in SANITIZER_MARKERS):
        return "crash"
    if returncode in (86, 87):
        return "crash"
    if returncode is not None and returncode < 0:
        return "crash"
    if returncode == 0:
        return "accepted"
    if returncode == 1:
        return "rejected"
    if returncode == 2:
        return "harness_error"
    return "unexpected_exit"


def harness_env() -> dict[str, str]:
    env = os.environ.copy()
    env["ASAN_OPTIONS"] = "detect_leaks=1:halt_on_error=1:exitcode=86"
    env["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1:exitcode=87"
    return env


def run_harness(binary: Path, text: str, timeout: float) -> dict:
    data = text.encode("utf-8", errors="surrogatepass")
    started = time.perf_counter()
    try:
        result = subprocess.run(
            [str(binary)],
            input=data,
            capture_output=True,
            timeout=timeout,
            env=harness_env(),
        )
        timed_out = False
        stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as error:
        timed_out = True
        stdout, stderr, returncode = error.stdout or b"", error.stderr or b"", None

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "input_text": text,
        "input_hex": data.hex(),
        "input_length": len(data),
        "status": classify(returncode, stderr, timed_out),
        "returncode": returncode,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


def validate_acceptance(strategy, binary: Path, examples: int, timeout: float, output_dir: Path) -> dict:
    """Step 4.2: spot-check that the generator isn't rejected at the front door."""
    rows = [run_harness(binary, strategy.example(), timeout) for _ in range(examples)]
    accepted = sum(1 for row in rows if row["status"] == "accepted")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "validation.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    return {
        "examples": examples,
        "accepted": accepted,
        "acceptance_rate": accepted / examples if examples else 0.0,
        "sample_inputs": [row["input_text"] for row in rows[:8]],
    }


def run_batch(
    strategy,
    binary: Path,
    examples: int,
    timeout: float,
    wall_clock_seconds: float,
    log_path: Path,
    crash_dir: Path,
    iteration: int,
) -> dict:
    """Step 4.3: execute the strategy through the harness for up to `examples` inputs.

    `wall_clock_seconds` is the Constraints section's per-run backstop (10
    minutes for a run of up to 500 examples) against a strategy gone
    pathological, e.g. one generating inputs so large that serialization
    dominates: once exceeded, remaining draws are skipped rather than run, so
    a stuck run still terminates instead of consuming the rest of the budget.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    crash_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    state = {"truncated_at": None}

    @given(strategy)
    @settings(
        max_examples=examples,
        deadline=None,
        database=None,
        suppress_health_check=[
            HealthCheck.too_slow,
            HealthCheck.data_too_large,
            HealthCheck.large_base_example,
            HealthCheck.filter_too_much,
        ],
    )
    def property_test(text: str) -> None:
        elapsed = time.monotonic() - started
        if elapsed > wall_clock_seconds:
            if state["truncated_at"] is None:
                state["truncated_at"] = elapsed
            return
        row = run_harness(binary, text, timeout)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        if row["status"] in {"crash", "timeout"}:
            digest = hashlib.sha256(bytes.fromhex(row["input_hex"])).hexdigest()[:16]
            crash_path = crash_dir / f"iter{iteration:02d}-{row['status']}-{digest}.bin"
            crash_path.write_bytes(bytes.fromhex(row["input_hex"]))

    property_test()
    return {"wall_clock_seconds": round(time.monotonic() - started, 3), "truncated_at": state["truncated_at"]}


def request_strategy(client, prompt: str, label: str, log_dir: Path, repair_attempts: int = 2):
    """Call the LLM and validate the result, retrying with a repair prompt on failure.

    Returns the first ValidationResult with ok=True; raises RuntimeError if
    every attempt (the original plus `repair_attempts` corrections) fails
    validation.
    """
    current_prompt = prompt
    result = None
    for attempt in range(repair_attempts + 1):
        call_label = label if attempt == 0 else f"{label}-repair{attempt}"
        response = client.complete(current_prompt, call_label)
        (log_dir / f"llm_response_{call_label}.txt").write_text(response, encoding="utf-8")
        result = validate(response)
        if result.ok:
            return result
        current_prompt = repair_prompt(result.code, result.problem, result.detail)
        (log_dir / f"repair_prompt_{call_label}.md").write_text(current_prompt, encoding="utf-8")
    raise RuntimeError(f"{result.problem}: {result.detail}")


def build_report_markdown(evolution: list[dict], ledger: dict, client, best_iteration: int, best_score: float) -> str:
    lines = [
        "# Step 4 Agentic Loop Final Report",
        "",
        f"- LLM: {'live ' + client.model if getattr(client, 'is_live', False) else 'offline fallback (' + client.model + ')'}",
        f"- Iterations completed: {len(evolution)}",
        f"- Estimated spend: ${ledger['estimated_cost_usd']:.4f} of ${ledger['budget_usd']:.2f} budget "
        f"({ledger['total_tokens']} tokens across {ledger['calls']} calls)",
        f"- Best iteration: {best_iteration} (score {best_score:.1f})",
        "",
    ]
    for item in evolution:
        if item["status"] != "ok":
            lines.append(f"## Iteration {item['iteration']}: FAILED ({item.get('error')})")
            lines.append("")
            continue
        summary = item["summary"]
        heading = (
            f"## Iteration {item['iteration']} (kept)"
            if item["kept"]
            else f"## Iteration {item['iteration']} (reverted to iteration {item['reverted_to_iteration']})"
        )
        acceptance_line = f"- Validation acceptance (spot-check): {item['validation']['acceptance_rate']:.1%}"
        if item.get("corrective_note"):
            acceptance_line += f" -- {item['corrective_note']}"
        lines.extend(
            [
                heading,
                "",
                f"- Score: {summary['score']:.1f} (crashes={len(summary['unique_crashes'])}, "
                f"coverage={summary['production_coverage']:.0%}, depth_entropy={summary['depth_entropy']:.2f}, "
                f"acceptance_band={summary['acceptance_band']:.2f})",
                acceptance_line,
                f"- Run statuses: `{summary['statuses']}`",
                f"- Run acceptance: {summary['acceptance_rate']:.1%}",
                f"- Depth distribution: `{summary['depth_distribution']}`",
                f"- Productions missing: `{summary['productions_missing']}`",
                f"- Unique crash signatures: {len(summary['unique_crashes'])}",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", type=Path, default=default_harness())
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--examples", type=int, default=500, help="Hypothesis examples per run (Constraints cap: 500).")
    parser.add_argument("--validation-examples", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=5.0, help="Per-input timeout in seconds (Constraints: 5s).")
    parser.add_argument(
        "--run-wall-clock-seconds",
        type=float,
        default=600.0,
        help="Per-run backstop in seconds (Constraints: 10 minutes for a run of up to 500 examples).",
    )
    parser.add_argument("--budget-usd", type=float, default=5.0, help="LLM spend cap (Constraints: ~$5).")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip the Anthropic API; replay the hand-written profile strategies instead.",
    )
    parser.add_argument("--output-dir", type=Path, default=ROOT / "logs" / "agentic-loop")
    args = parser.parse_args()

    binary = args.binary.resolve()
    if not binary.exists():
        raise SystemExit(f"Harness binary not found: {binary}")

    grammar = (ROOT / "grammar" / "JSON.g4").read_text(encoding="utf-8")
    adaptations = (ROOT / "grammar" / "adaptations.md").read_text(encoding="utf-8")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    generated_dir = ROOT / "strategies" / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    crash_dir = ROOT / "crashes" / "raw"

    try:
        client = build_client(args.offline, model=args.model, budget_usd=args.budget_usd)
    except RuntimeError as error:
        print(f"Falling back to --offline: {error}", file=sys.stderr)
        client = build_client(True, budget_usd=args.budget_usd)

    history: list[dict] = []
    evolution: list[dict] = []
    best_score = -1.0
    best_code: str | None = None
    best_iteration = 0
    current_code: str | None = None

    for iteration in range(1, args.iterations + 1):
        iteration_dir = args.output_dir / f"iteration-{iteration:02d}"
        iteration_dir.mkdir(parents=True, exist_ok=True)

        prompt = (
            seed_prompt(grammar, adaptations)
            if iteration == 1
            else refinement_prompt(iteration, grammar, adaptations, current_code, history[-1]["summary"], history)
        )
        (iteration_dir / "prompt.md").write_text(prompt, encoding="utf-8")

        try:
            result = request_strategy(client, prompt, f"iteration{iteration:02d}", iteration_dir)
        except BudgetExhausted as error:
            print(f"Stopping at iteration {iteration}: {error}", file=sys.stderr)
            break
        except RuntimeError as error:
            print(f"Iteration {iteration}: no usable strategy ({error}); skipping.", file=sys.stderr)
            evolution.append({"iteration": iteration, "status": "validation_failed", "error": str(error)})
            continue

        candidate_code = result.code
        (generated_dir / f"iteration-{iteration:02d}.py").write_text(candidate_code, encoding="utf-8")

        validation = validate_acceptance(result.strategy, binary, args.validation_examples, args.timeout, iteration_dir)
        corrective_note = None
        if validation["acceptance_rate"] < 0.02:
            corrective_note = "spot-check acceptance was near zero; requested a repair before the full run"
            fix_prompt = repair_prompt(
                candidate_code,
                "near-zero parser acceptance",
                f"{validation['accepted']}/{validation['examples']} spot-checked examples were accepted by the "
                f"parser -- a generator rejected this often at the front door isn't testing anything interesting. "
                f"Sample rejected inputs: {validation['sample_inputs']}",
            )
            try:
                fixed = request_strategy(client, fix_prompt, f"iteration{iteration:02d}-acceptfix", iteration_dir, repair_attempts=1)
                candidate_code = fixed.code
                (generated_dir / f"iteration-{iteration:02d}.py").write_text(candidate_code, encoding="utf-8")
                validation = validate_acceptance(fixed.strategy, binary, args.validation_examples, args.timeout, iteration_dir)
                result = fixed
            except BudgetExhausted as error:
                print(f"Stopping at iteration {iteration}: {error}", file=sys.stderr)
                break
            except RuntimeError as error:
                corrective_note += f"; repair attempt failed ({error}), running with the low-acceptance strategy anyway"

        run_log = iteration_dir / "run.jsonl"
        run_meta = run_batch(
            result.strategy, binary, args.examples, args.timeout, args.run_wall_clock_seconds, run_log, crash_dir, iteration
        )
        summary = summarize(run_log)
        write_markdown(summary, iteration_dir / "summary.md")

        if hasattr(client, "observe"):
            client.observe(summary)

        history.append(
            {
                "iteration": iteration,
                "score": summary["score"],
                "acceptance_rate": summary["acceptance_rate"],
                "production_coverage": summary["production_coverage"],
                "depth_entropy": summary["depth_entropy"],
                "unique_crashes": len(summary["unique_crashes"]),
                "summary": summary,
            }
        )

        kept = summary["score"] >= best_score
        if kept:
            best_score, best_code, best_iteration = summary["score"], candidate_code, iteration
        current_code = candidate_code if kept else best_code

        evolution.append(
            {
                "iteration": iteration,
                "status": "ok",
                "corrective_note": corrective_note,
                "validation": validation,
                "run_meta": run_meta,
                "summary": summary,
                "kept": kept,
                "reverted_to_iteration": None if kept else best_iteration,
            }
        )

        print(
            f"iteration {iteration}: score={summary['score']:.1f} acceptance={summary['acceptance_rate']:.1%} "
            f"crashes={len(summary['unique_crashes'])} kept={kept}",
            file=sys.stderr,
        )

    ledger = client.ledger.as_dict()
    final = {
        "iterations_completed": len(evolution),
        "examples_per_iteration": args.examples,
        "validation_examples_per_iteration": args.validation_examples,
        "harness": str(binary),
        "model": getattr(client, "model", "offline"),
        "live_llm": getattr(client, "is_live", False),
        "budget": ledger,
        "best_iteration": best_iteration,
        "best_score": best_score,
        "evolution": evolution,
    }
    (args.output_dir / "final_report.json").write_text(json.dumps(final, indent=2, ensure_ascii=False), encoding="utf-8")
    if best_code:
        (ROOT / "strategies" / "final_strategy.py").write_text(best_code, encoding="utf-8")

    (args.output_dir / "final_report.md").write_text(
        build_report_markdown(evolution, ledger, client, best_iteration, best_score), encoding="utf-8"
    )
    print(f"Wrote Step 4 logs and report to {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
