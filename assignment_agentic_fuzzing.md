# Assignment: Agentic Fuzzing with Generative Test Strategies

## Overview

You will build a **test generator (fuzzer)** to find crashing bugs in a
small C library that parses a structured text format. Rather than
generating random bytes, you will start from a formal grammar of the
format and build an **agentic loop**: an LLM that takes the grammar as
input and produces a composable [Hypothesis](https://hypothesis.readthedocs.io/)
strategy that generates strings in the language of that grammar, refining
it based on feedback from previous runs.

You will be assigned one library from the list below, pinned to a specific
commit. Your job is to:

1. Find a formal grammar for the library's input format.
2. Build a harness that runs the library against generated inputs,
   compiled with sanitizers so memory-safety bugs are caught reliably.
3. Build an agentic loop in which an LLM turns that grammar into a
   Hypothesis strategy and iteratively refines it using feedback (crashes
   found so far, parser error patterns, structural diversity of inputs
   tried), within a fixed budget.
4. Report and triage what you find.

This is a **blackbox** approach: no coverage-guided mutation, no
instrumentation beyond sanitizers. You are being evaluated on how well you
can drive an LLM to turn a formal grammar into an effective generator, and
how well you can design the feedback loop around it — not on fuzzing-engine
internals.

## What You're Being Evaluated On

- Whether you can correctly identify and interpret the right grammar for
  your target format, and communicate it clearly enough to seed the
  agentic loop.
- The quality of the agentic loop's output: does the generated Hypothesis
  strategy actually reflect the grammar (recursive structure, escaping,
  numeric edge cases, nesting) rather than a generic "big random string"?
- Engineering rigor: a correct sanitizer build, a correct harness, and
  correct judgment about what counts as a crash versus a legitimate
  rejection.
- The design of your feedback loop: with no coverage signal available, what
  proxy signal did you choose to make the loop actually improve the
  generator across iterations, and why?
- Triage instinct: deduping crashes by root cause, minimizing reproducers,
  distinguishing a new bug from a repeat.
- Judgment under a budget: you have a fixed iteration/time/cost limit —
  prioritize accordingly.

## Your Target Library

You will be assigned one of:

| Library     | Format       |
| ----------- | ------------ |
| inih        | INI          |
| libcsv      | CSV          |
| tomlc99     | TOML         |
| parson      | JSON         |
| json-parser | JSON         |
| mxml        | XML (subset) |

You will be given a pinned commit/tag to build against. Do not build
against the latest upstream version — use exactly the pinned version
provided.

## Steps

### Step 1 — Find the Grammar

Locate a formal grammar for your target format in the
[ANTLR `grammars-v4`](https://github.com/antlr/grammars-v4) repository
(e.g. its JSON, CSV, TOML, or XML grammar). This grammar is your starting
point — you do not need to read the target library's parser source to
derive the grammar yourself. Note any places where the library's actual
accepted format is a subset, superset, or informal variant of what the
ANTLR grammar describes (e.g. a library may not support all of TOML, or
may accept things the formal grammar doesn't) — you may need to adapt the
grammar to match reality, and documenting that gap is part of the
deliverable.

### Step 2 — Harness Construction

Write a small C driver that reads an input (stdin or file) and calls the
library's parse entry point. Write a build script that compiles the
library and harness with `-fsanitize=address,undefined`. Your harness must
let the sanitizer abort on memory-safety/UB violations, and must exit
cleanly (0, or a distinguishable code) on a valid parse or a well-formed
rejection. Before moving on, demonstrate your harness behaves correctly on
a handful of valid and invalid sample inputs.

### Step 3 — Baseline Strategy

Write a first, intentionally naive Hypothesis strategy (e.g. random text)
to validate your full pipeline end-to-end: generate input → serialize →
run harness → detect crash/non-crash → log result. The goal here is
plumbing correctness, not finding bugs yet.
 
### Step 4 — Agentic Loop: Grammar → Generator

Build a loop with the following stages, run repeatedly until your budget
is exhausted:

1. **Seed.** On the first iteration, give the LLM the ANTLR grammar from
   Step 1 (plus your noted adaptations) and ask it to produce a Hypothesis
   strategy generating strings in that grammar's language. Require it to
   use `st.recursive`/`@composite` for recursive productions rather than
   flattening the grammar, and to explicitly cover edge cases: empty
   containers, deep nesting, duplicate keys, extreme numeric values,
   unicode/escaped characters, and near-valid-but-malformed inputs.
2. **Validate the generator itself.** Before running it against the
   target, sanity-check that the strategy actually produces syntactically
   plausible output (e.g. spot-check `.example()` calls, or run it and
   check the parser's own acceptance rate isn't near-zero). A generator
   that's rejected 99% of the time by the parser's front door isn't
   testing anything interesting — decide how you detect and correct for
   this.
3. **Run.** Execute the strategy through your harness for a bounded
   number of examples (Hypothesis's `@given`/`@settings(max_examples=...)`
   is the natural fit here), logging per-input: crash/no-crash, sanitizer
   output if crashed, and parser exit code/error message if not.
4. **Summarize results.** Produce a compact summary for the LLM: unique
   crash signatures found so far (see Step 5), a sample of parser error
   messages (what's being rejected and why), and some structural measure
   of diversity (e.g. distribution of nesting depth, which grammar
   productions have and haven't appeared in generated inputs).
5. **Refine.** Feed that summary back to the LLM along with the current
   strategy code, and ask it to propose a revised version — e.g. steering
   away from productions that are mostly rejected, deepening recursion
   where it hasn't crashed yet, or targeting grammar rules that haven't
   been exercised. Decide explicitly what signal you're optimizing the
   loop toward, since there's no coverage instrumentation to fall back on.
6. **Stop.** Terminate when you hit your iteration cap, wall-clock limit,
   or cost budget (specified in Constraints below), and produce a final
   report of the strategy's evolution.

### Step 5 — Crash Triage

1. **Detect.** A run counts as a crash if the harness process is killed
   by a fatal signal (SIGSEGV, SIGABRT, SIGFPE, etc.) or if sanitizer
   output appears in stderr (`AddressSanitizer`, `UndefinedBehaviorSanitizer`,
   `runtime error:`). Apply a per-run timeout and decide up front whether
   a timeout counts as a crash (see Constraints).
2. **Capture.** For each crash, save the exact input, the full
   stderr/sanitizer report, and the exit signal/code — you'll need all
   three for deduplication and for the final report.
3. **Deduplicate.** Parse each sanitizer report for its stack trace and
   hash a normalized form of the top few frames (e.g. function names,
   ignoring addresses/line-number noise) as a crash signature. Group
   captured crashes by this signature — this is what determines whether
   you found one bug or several, so document your normalization choices.
4. **Minimize.** For each unique signature, use Hypothesis's shrinking
   (it runs automatically when a `@given`-wrapped test fails) to reduce
   the triggering input to a small reproducer. If you're driving the
   harness outside of a `@given` test loop, make sure you're still
   invoking Hypothesis's shrinker rather than just keeping the first
   crashing example you saw.
5. **Verify.** Re-run each minimized reproducer once, standalone, against
   the pinned build to confirm it deterministically reproduces the crash
   before including it in your report.

### Step 6 — Report

Submit two things: your artifacts, and a written report.

**Artifacts:**

- The grammar you used, its source, and any adaptations you made to match
  the target library's real accepted format.
- Your build script and harness source.
- Your baseline strategy and a short note confirming the pipeline works.
- Your agentic loop implementation, including the final generated
  Hypothesis strategy and a log of how it evolved across iterations.
- Deduplicated, minimized crash reports — or, if none were found, a
  documented explanation of why, and what you'd try next with more time.

**Written report (two pages, excluding code/logs/appendices):** explain
the assignment to someone who hasn't read this spec. It should cover:

- **Design.** The grammar you started from and the adaptations you made;
  how your harness and build determine crash vs. valid parse vs.
  well-formed rejection; the structure of your agentic loop and, in
  particular, what proxy signal you chose to steer refinement by (since
  there's no coverage instrumentation) and why you expected it to work.
- **Findings.** What you found: crash reports (or a documented "none
  found" with your best explanation why), how the generated strategy
  evolved across iterations and what drove each change, and which parts
  of the grammar you suspect are still under-tested.
- **Challenges.** What was harder than expected, any judgment calls you
  had to make and document (e.g. crash deduplication/normalization
  choices, timeout-as-crash policy, correcting a generator that was
  mostly being rejected), and what you'd change with more time or with
  coverage feedback available.

Code, logs, and raw crash reports belong in an appendix or linked
repo — they don't count against the two pages, and the report should be
readable without them.

## Deliverables Checklist

- [ ] Grammar source + noted adaptations
- [ ] Build script + harness source
- [ ] Baseline strategy + pipeline demonstration
- [ ] Agentic loop implementation + final generator + iteration log
- [ ] Deduplicated, minimized crash reports (or documented "none found")
- [ ] Two-page written report (design, findings, challenges)

## Constraints

- Maximum Hypothesis examples per run: 500 examples per iteration, with a
  10-minute wall-clock cap on the run as a backstop (a run of 500 inputs
  through a small C library should finish in well under a minute; the cap
  exists to catch a strategy that's gone pathological, e.g. generating
  inputs so large that serialization or process spawning dominates).
- Maximum agentic loop iterations: 5, or roughly $5 of LLM API spend
  (~a few hundred thousand tokens at current small/mid-tier model
  pricing), whichever comes first — log both the iteration count and an
  estimate of tokens/cost spent in your report. 5 iterations was enough in
  a trial run of this assignment (on parson/JSON) to go from a
  grammar-seeded generator with 0 crashes to a refined one finding crashes
  reliably; treat needing many more than that as a sign the feedback
  signal itself needs rethinking, not just more budget.
- Per-run timeout: 5 seconds per input, to bound hangs.
- Timeouts count as crashes for grading purposes. A parser that hangs
  instead of terminating is a real bug (denial-of-service via
  pathological input, e.g. catastrophic backtracking or an unbounded
  loop) and should go through the same triage pipeline as a sanitizer
  abort, not be silently dropped.
