"""Summarize Step 4 JSONL run logs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SANITIZER_MARKERS = ("AddressSanitizer", "UndefinedBehaviorSanitizer", "runtime error:", "LeakSanitizer")

TRACKED_PRODUCTIONS = ("obj", "arr", "STRING", "NUMBER", "true", "false", "null", "UNICODE", "ESC")
DEPTH_BUCKETS = ("0", "1-2", "3-5", "6+")

# Weights for the proxy signal the agentic loop optimizes toward, in the
# absence of coverage instrumentation. See loop/prompts.py for the rationale.
SCORE_WEIGHTS = {"crashes": 100.0, "coverage": 40.0, "depth_entropy": 30.0, "acceptance_band": 30.0}


def _normalized_entropy(counts: dict, buckets: tuple[str, ...]) -> float:
    total = sum(counts.get(bucket, 0) for bucket in buckets)
    if total == 0:
        return 0.0
    entropy = 0.0
    for bucket in buckets:
        count = counts.get(bucket, 0)
        if count:
            probability = count / total
            entropy -= probability * math.log2(probability)
    max_entropy = math.log2(len(buckets)) if len(buckets) > 1 else 1.0
    return entropy / max_entropy if max_entropy else 0.0


def _acceptance_band(rate: float) -> float:
    """1.0 inside [0.30, 0.90], falling off toward the edges outside it.

    Below the band the generator is mostly being rejected at the front door
    (Step 4.2's "not testing anything interesting"); above it, it is only
    producing textbook-valid documents and is not probing the parser boundary.
    """
    if 0.30 <= rate <= 0.90:
        return 1.0
    if rate < 0.30:
        return rate / 0.30
    return max(0.0, 1.0 - (rate - 0.90) / 0.10)


def score_run(summary: dict) -> dict:
    depth_entropy = _normalized_entropy(summary["depth_distribution"], DEPTH_BUCKETS)
    coverage = len(summary["productions_seen"]) / len(TRACKED_PRODUCTIONS)
    acceptance_band = _acceptance_band(summary["acceptance_rate"])
    unique_crashes = len(summary["unique_crashes"])
    score = (
        SCORE_WEIGHTS["crashes"] * unique_crashes
        + SCORE_WEIGHTS["coverage"] * coverage
        + SCORE_WEIGHTS["depth_entropy"] * depth_entropy
        + SCORE_WEIGHTS["acceptance_band"] * acceptance_band
    )
    return {
        "depth_entropy": round(depth_entropy, 4),
        "production_coverage": round(coverage, 4),
        "acceptance_band": round(acceptance_band, 4),
        "score": round(score, 4),
    }


def nesting_depth(text: str) -> int:
    depth = 0
    highest = 0
    in_string = False
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "[{":
            depth += 1
            highest = max(highest, depth)
        elif char in "]}":
            depth = max(0, depth - 1)
    return highest


def productions_seen(text: str) -> set[str]:
    seen = set()
    if "{" in text:
        seen.add("obj")
    if "[" in text:
        seen.add("arr")
    if '"' in text:
        seen.add("STRING")
    if re.search(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([Ee][+-]?[0-9]+)?", text):
        seen.add("NUMBER")
    for literal in ("true", "false", "null"):
        if literal in text:
            seen.add(literal)
    if "\\u" in text:
        seen.add("UNICODE")
    if re.search(r"\\[\"\\/bfnrt]", text):
        seen.add("ESC")
    return seen


def crash_signature(stderr: str, returncode: int | None) -> str:
    frames = []
    for line in stderr.splitlines():
        if "AddressSanitizer" in line or "UndefinedBehaviorSanitizer" in line or "runtime error:" in line:
            frames.append(line.strip())
        elif re.search(r"#\d+\s+0x[0-9a-fA-F]+", line):
            normalized = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", line.strip())
            normalized = re.sub(r":\d+(:\d+)?", ":LINE", normalized)
            frames.append(normalized)
        if len(frames) >= 6:
            break
    if not frames:
        frames = [f"returncode={returncode}", stderr.splitlines()[0] if stderr else ""]
    return hashlib.sha256("\n".join(frames).encode("utf-8", errors="replace")).hexdigest()[:16]


def summarize(log_path: Path) -> dict:
    rows = []
    # Split on a literal "\n" only: str.splitlines() also breaks on U+0085,
    # U+2028, U+2029 and others, which the raw-byte-text generator can embed
    # inside a JSON string value (json.dumps only escapes chars below 0x20),
    # splitting one record across two lines and corrupting the parse.
    for line in log_path.read_text(encoding="utf-8").split("\n"):
        if line.strip():
            rows.append(json.loads(line))

    statuses = Counter(row["status"] for row in rows)
    depths = Counter()
    productions = Counter()
    parser_messages = []
    crashes = {}

    for row in rows:
        text = row.get("input_text", "")
        depth = nesting_depth(text)
        bucket = "0" if depth == 0 else "1-2" if depth <= 2 else "3-5" if depth <= 5 else "6+"
        depths[bucket] += 1
        productions.update(productions_seen(text))

        stderr = row.get("stderr", "")
        if row["status"] in {"rejected", "harness_error", "unexpected_exit"} and len(parser_messages) < 8:
            parser_messages.append(
                {
                    "status": row["status"],
                    "returncode": row.get("returncode"),
                    "stdout": row.get("stdout", "").strip(),
                    "stderr": stderr.strip()[:300],
                    "input_preview": text[:120],
                }
            )

        if row["status"] == "crash" or any(marker in stderr for marker in SANITIZER_MARKERS):
            signature = crash_signature(stderr, row.get("returncode"))
            crashes.setdefault(signature, {"count": 0, "example": row})
            crashes[signature]["count"] += 1

    total = len(rows)
    accepted = statuses.get("accepted", 0)
    rejected = statuses.get("rejected", 0)
    summary = {
        "total": total,
        "statuses": dict(statuses),
        "acceptance_rate": accepted / total if total else 0.0,
        "rejection_rate": rejected / total if total else 0.0,
        "depth_distribution": dict(depths),
        "productions_seen": sorted(productions),
        "productions_missing": sorted(set(TRACKED_PRODUCTIONS) - set(productions)),
        "parser_message_samples": parser_messages,
        "unique_crashes": {
            signature: {
                "count": value["count"],
                "returncode": value["example"].get("returncode"),
                "input_preview": value["example"].get("input_text", "")[:120],
                "stderr_preview": value["example"].get("stderr", "")[:500],
            }
            for signature, value in crashes.items()
        },
    }
    summary.update(score_run(summary))
    return summary


def write_markdown(summary: dict, output_path: Path) -> None:
    lines = [
        f"# Agentic Loop Summary",
        "",
        f"- Total inputs: {summary['total']}",
        f"- Statuses: `{summary['statuses']}`",
        f"- Acceptance rate: {summary['acceptance_rate']:.1%}",
        f"- Depth distribution: `{summary['depth_distribution']}`",
        f"- Productions seen: `{', '.join(summary['productions_seen']) or 'none'}`",
        f"- Productions missing: `{', '.join(summary['productions_missing']) or 'none'}`",
        f"- Unique crash signatures: {len(summary['unique_crashes'])}",
        f"- Score: {summary['score']:.1f} (coverage={summary['production_coverage']:.0%}, "
        f"depth_entropy={summary['depth_entropy']:.2f}, acceptance_band={summary['acceptance_band']:.2f})",
        "",
        "## Parser Message Samples",
    ]
    for sample in summary["parser_message_samples"]:
        lines.append(f"- {sample['status']} rc={sample['returncode']} stdout={sample['stdout']!r} input={sample['input_preview']!r}")
        if sample["stderr"]:
            lines.append(f"  stderr: `{sample['stderr']}`")
    if not summary["parser_message_samples"]:
        lines.append("- None captured.")
    lines.extend(["", "## Unique Crashes"])
    if summary["unique_crashes"]:
        for signature, value in summary["unique_crashes"].items():
            lines.append(f"- `{signature}` count={value['count']} rc={value['returncode']} input={value['input_preview']!r}")
    else:
        lines.append("- None.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    summary = summarize(args.log)
    print(json.dumps(summary, indent=2))
    if args.markdown:
        write_markdown(summary, args.markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
