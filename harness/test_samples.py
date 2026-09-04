"""Byte-exact subprocess tests; no shell quoting or text transcoding.

Run after `sh harness/build.sh`. Nonzero runner exit means a failed check.
"""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile


SAMPLES = [
    ("valid-object", b'{"name":"Nabiha"}', 0),
    ("valid-mixed-array", b'[1,"a",true,null,{}]', 0),
    ("valid-nested", b'{"a":{"b":[]}}', 0),
    ("valid-top-level-true", b'true', 0),
    ("reject-object-trailing-comma", b'{"a":1,}', 1),
    ("reject-array-trailing-comma", b'[1,2,]', 1),
    ("reject-missing-colon", b'{"a" 1}', 1),
    ("reject-unterminated-string", b'{"a":"abc', 1),
    ("reject-empty", b'', 1),
    ("reject-whitespace", b'   \n\t', 1),
    ("large-input-buffer-growth", b'{"a":"' + b'a' * 12000 + b'"}', 0),
    ("buffer-boundary-4094-bytes", b'"' + b'a' * 4092 + b'"', 0),
    ("buffer-boundary-4095-bytes", b'"' + b'a' * 4093 + b'"', 0),
    ("buffer-boundary-4096-bytes", b'"' + b'a' * 4094 + b'"', 0),
    # These document target behavior, not strict JSON conformance.
    ("target-trailing-garbage", b'{"a":1}garbage', 0),
    ("target-embedded-NUL", b'{}\x00garbage', 0),
    ("target-invalid-UTF8", b'{"a":"\xc3\x28"}', 0),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path,
                        default=Path(__file__).resolve().parents[1] / "build" / "parson_harness")
    parser.add_argument(
        "--report-dir", type=Path,
        help="save stdout verdicts, stderr sanitizer reports, and a TSV summary",
    )
    args = parser.parse_args()
    binary = str(args.binary.resolve())
    report_dir = args.report_dir.resolve() if args.report_dir else None
    if report_dir:
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "verdicts.txt").write_bytes(b"")
        (report_dir / "sanitizer-reports.txt").write_bytes(b"")
        (report_dir / "summary.tsv").write_text(
            "case\tstatus\texit_code\tverdict_file\tsanitizer_file\n",
            encoding="utf-8",
        )
    env = os.environ.copy()
    env["ASAN_OPTIONS"] = "detect_leaks=1:halt_on_error=1:exitcode=86"
    env["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1:exitcode=87"
    failures = 0
    checks = 0

    def check(name, arguments, data, expected_code, expected_stdout, error_expected=False):
        nonlocal checks, failures
        checks += 1
        try:
            result = subprocess.run([binary, *arguments], input=data,
                                    capture_output=True, env=env, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as error:
            failures += 1
            print(f"FAIL {name}: {error}")
            return
        stderr_ok = (result.stderr.startswith(b"HARNESS_ERROR:") or
                     result.stderr.startswith(b"Usage:")) if error_expected else result.stderr == b""
        # Native Windows smoke tests use CRLF; Linux/Docker uses LF.
        stdout = result.stdout.replace(b"\r\n", b"\n")
        passed = (result.returncode == expected_code and stdout == expected_stdout and stderr_ok)
        if report_dir:
            safe_name = name.replace("/", "__")
            verdict_file = report_dir / f"{safe_name}.verdict.txt"
            sanitizer_file = report_dir / f"{safe_name}.sanitizer.txt"
            verdict_file.write_bytes(result.stdout)
            sanitizer_file.write_bytes(result.stderr)
            with (report_dir / "verdicts.txt").open("ab") as stream:
                stream.write(f"=== {name} (exit {result.returncode}) ===\n".encode())
                stream.write(result.stdout or b"<empty>\n")
            with (report_dir / "sanitizer-reports.txt").open("ab") as stream:
                stream.write(f"=== {name} (exit {result.returncode}) ===\n".encode())
                stream.write(result.stderr or b"<empty>\n")
                if result.stderr and not result.stderr.endswith(b"\n"):
                    stream.write(b"\n")
            with (report_dir / "summary.tsv").open("a", encoding="utf-8", newline="") as stream:
                stream.write(
                    f"{name}\t{'PASS' if passed else 'FAIL'}\t{result.returncode}\t"
                    f"{verdict_file.name}\t{sanitizer_file.name}\n"
                )
        print(f"{'PASS' if passed else 'FAIL'} {name}: exit={result.returncode}, "
              f"stderr={'diagnostic' if result.stderr else 'empty'}")
        if not passed:
            failures += 1
            print(f"  expected exit={expected_code}, stdout={expected_stdout!r}")
            print(f"  actual stdout={result.stdout!r}, stderr={result.stderr!r}")

    with tempfile.TemporaryDirectory(prefix="parson-harness-") as directory:
        root = Path(directory)
        for name, data, code in SAMPLES:
            verdict = b"ACCEPTED\n" if code == 0 else b"REJECTED\n"
            check(name + "/stdin", [], data, code, verdict)
            sample = root / (name + " sample.bin")
            sample.write_bytes(data)
            check(name + "/file", [str(sample)], None, code, verdict)
        check("explicit-stdin-dash", ["-"], b'{}', 0, b"ACCEPTED\n")
        check("missing-file", [str(root / "does-not-exist.json")], None, 2, b"", True)
        check("usage-error", ["one", "two"], None, 2, b"", True)

    print(f"\n{checks - failures}/{checks} checks passed; {failures} failed.")
    if report_dir:
        print(f"Reports saved to: {report_dir}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
