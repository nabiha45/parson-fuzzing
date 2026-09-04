FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends clang python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
COPY parson/parson.c parson/parson.h ./parson/
COPY harness/ ./harness/

ENV ASAN_OPTIONS=detect_leaks=1:halt_on_error=1:exitcode=86
ENV UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1:exitcode=87

RUN sh harness/build.sh
RUN python3 harness/test_samples.py

CMD ["./build/parson_harness"]
