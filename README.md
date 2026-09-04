# Parson Agentic Fuzzing

Agentic grammar-based fuzzing project for the Parson JSON parser.

## Target

- Repository: https://github.com/kgabis/parson
- Commit: 60b2c69
- Commit message: Improved serialization performance (#156)

The project must be tested against this exact commit.

## Step 3 — Baseline Strategy

The intentionally naive baseline is implemented in
`strategies/baseline.py`. It uses Hypothesis's `st.text()` strategy to
generate arbitrary Unicode strings and exercises the complete pipeline:

1. Generate a Python string with Hypothesis.
2. Serialize it to UTF-8 bytes.
3. Send the bytes to the Parson harness through standard input.
4. Classify the result as accepted, rejected, crashed, timed out, a harness
   error, or an unexpected exit.
5. Append the input and result to `logs/baseline.jsonl`.

The baseline was run for 100 examples. All 100 inputs were handled normally
and rejected as invalid JSON; no crashes or timeouts occurred. This confirms
that input generation, serialization, harness execution, result detection,
and logging work end-to-end.

Run the baseline from the repository root with:

```console
python strategies/baseline.py
```

On Windows, the script uses `build/parson_harness_native.exe`; on Linux and
in Docker, it uses `build/parson_harness`.
