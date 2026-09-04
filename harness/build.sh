#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CC=${CC:-clang}
OUT_DIR=${OUT_DIR:-"$ROOT/build"}
mkdir -p "$OUT_DIR"

"$CC" -std=c99 -Wall -Wextra -Wpedantic \
    -fsanitize=address,undefined -fno-sanitize-recover=all \
    -fno-omit-frame-pointer -g -O0 \
    "$ROOT/harness/harness.c" "$ROOT/parson/parson.c" \
    -o "$OUT_DIR/parson_harness" -lm

printf 'Built sanitizer-instrumented harness: %s/parson_harness\n' "$OUT_DIR"
