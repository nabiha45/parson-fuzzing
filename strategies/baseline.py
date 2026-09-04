import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from hypothesis import given, settings, strategies as st


ROOT = Path(__file__).resolve().parents[1]

# Linux/Docker:
HARNESS = ROOT / "build" / "parson_harness"

# Change to parson_harness_native.exe when running the native Windows build.
if os.name == "nt":
    HARNESS = ROOT / "build" / "parson_harness_native.exe"

LOG_FILE = ROOT / "logs" / "baseline.jsonl"
CRASH_DIR = ROOT / "crashes" / "raw"

SANITIZER_MARKERS = (
    b"AddressSanitizer",
    b"UndefinedBehaviorSanitizer",
    b"runtime error:",
    b"LeakSanitizer",
)


def random_text():
    """Intentionally naive input generator."""
    return st.text(
        alphabet=st.characters(),
        min_size=0,
        max_size=1_000,
    )


def classify(result: subprocess.CompletedProcess) -> str:
    if result.returncode == 0:
        return "accepted"

    if result.returncode == 1:
        return "rejected"

    if result.returncode == 2:
        return "harness_error"

    if (
        result.returncode in (86, 87)
        or result.returncode < 0
        or any(marker in result.stderr for marker in SANITIZER_MARKERS)
    ):
        return "crash"

    return "unexpected_exit"


def run_case(text: str) -> str:
    # This is the serialization boundary for the generated Python value.
    data = text.encode("utf-8")

    env = os.environ.copy()
    env["ASAN_OPTIONS"] = "detect_leaks=1:halt_on_error=1:exitcode=86"
    env["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1:exitcode=87"

    try:
        result = subprocess.run(
            [str(HARNESS)],
            input=data,
            capture_output=True,
            timeout=2,
            env=env,
        )
        status = classify(result)
    except subprocess.TimeoutExpired:
        result = None
        status = "timeout"

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as log:
        log.write(
            json.dumps(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "input_hex": data.hex(),
                    "input_length": len(data),
                    "status": status,
                    "returncode": None if result is None else result.returncode,
                    "stdout": "" if result is None
                    else result.stdout.decode("utf-8", errors="replace"),
                    "stderr": "" if result is None
                    else result.stderr.decode("utf-8", errors="replace"),
                }
            )
            + "\n"
        )

    if status == "crash":
        CRASH_DIR.mkdir(parents=True, exist_ok=True)
        crash_file = CRASH_DIR / f"crash-{data.hex()[:40] or 'empty'}.bin"
        crash_file.write_bytes(data)

    return status


@given(random_text())
@settings(
    max_examples=100,
    deadline=None,       # Subprocess startup can exceed Hypothesis's deadline.
    print_blob=True,     # Prints replay information on a failure.
)
def test_baseline(text: str):
    status = run_case(text)

    # Accepted and rejected inputs are both normal parser outcomes.
    # Raise only for behavior worth investigation.
    assert status not in {"crash", "timeout", "unexpected_exit"}


if __name__ == "__main__":
    test_baseline()