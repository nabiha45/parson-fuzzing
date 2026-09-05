"""Grammar-shaped Hypothesis strategies for the Step 4 agentic loop.

The public entry point is ``json_text_strategy(profile=...)``.  Profiles are
used by the loop as its offline refinement mechanism: each iteration changes
the mix of valid JSON, Parson adaptations, and malformed edge cases.
"""

from __future__ import annotations

import string

from hypothesis import strategies as st


SAFE_CHARS = st.characters(
    blacklist_categories=("Cs",),
    blacklist_characters='"\\\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\r\x0b\x0c'
    "\x0e\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f",
)

ESCAPES = st.sampled_from(['\\"', "\\\\", "\\/", "\\b", "\\f", "\\n", "\\r", "\\t"])
UNICODE_ESCAPES = st.sampled_from(
    [
        "\\u0000",
        "\\u001f",
        "\\u007f",
        "\\ud83d\\ude00",
        "\\ud83d",
        "\\ude00\\ud83d",
        "\\uffff",
    ]
)


def json_string() -> st.SearchStrategy[str]:
    plain = st.text(SAFE_CHARS, min_size=0, max_size=24)
    chunk = st.one_of(plain, ESCAPES, UNICODE_ESCAPES)
    return st.lists(chunk, min_size=0, max_size=6).map(lambda parts: '"' + "".join(parts) + '"')


def json_number() -> st.SearchStrategy[str]:
    integer = st.one_of(
        st.just("0"),
        st.integers(min_value=1, max_value=10**18).map(str),
        st.integers(min_value=1, max_value=10**18).map(lambda n: "-" + str(n)),
    )
    fraction = st.one_of(st.just(""), st.integers(min_value=0, max_value=999999).map(lambda n: f".{n}"))
    exponent = st.one_of(
        st.just(""),
        st.integers(min_value=-308, max_value=308).map(lambda n: f"e{n}"),
        st.integers(min_value=0, max_value=999).map(lambda n: f"E+{n}"),
    )
    extremes = st.sampled_from(["1e309", "-1e309", "1e-999", "-0", "0.0", "999999999999999999999999"])
    return st.one_of(st.builds(lambda i, f, e: i + f + e, integer, fraction, exponent), extremes)


def _object_strategy(value_strategy: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    pair = st.builds(lambda key, value: f"{key}:{value}", json_string(), value_strategy)
    normal = st.lists(pair, min_size=0, max_size=5).map(lambda pairs: "{" + ",".join(pairs) + "}")
    duplicate_key = st.builds(
        lambda value1, value2: '{"dup":' + value1 + ',"dup":' + value2 + "}",
        value_strategy,
        value_strategy,
    )
    return st.one_of(normal, duplicate_key)


def _array_strategy(value_strategy: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    return st.lists(value_strategy, min_size=0, max_size=6).map(lambda values: "[" + ",".join(values) + "]")


def valid_json_strategy() -> st.SearchStrategy[str]:
    scalar = st.one_of(json_string(), json_number(), st.sampled_from(["true", "false", "null"]))
    return st.recursive(
        scalar,
        lambda children: st.one_of(_array_strategy(children), _object_strategy(children)),
        max_leaves=30,
    )


def malformed_json_strategy() -> st.SearchStrategy[str]:
    valid = valid_json_strategy()
    return st.one_of(
        st.just(""),
        st.just("   \n\t"),
        st.builds(lambda value: value + " garbage", valid),
        st.builds(lambda value: "\ufeff" + value, valid),
        st.builds(lambda value: "[" + value + ",]", valid),
        st.builds(lambda value: '{"a":' + value + ",}", valid),
        st.sampled_from(['{"a":1.}', '{"a":01}', '{"a":"unterminated}', '{"a" 1}', "[1,,2]"]),
    )


def deep_nesting_strategy() -> st.SearchStrategy[str]:
    return st.integers(min_value=8, max_value=80).map(lambda depth: "[" * depth + "0" + "]" * depth)


def raw_byte_text_strategy() -> st.SearchStrategy[str]:
    alphabet = string.printable + "\x00"
    return st.binary(min_size=0, max_size=128).map(lambda data: data.decode("latin1"))


def json_text_strategy(profile: str = "seed") -> st.SearchStrategy[str]:
    valid = valid_json_strategy()
    malformed = malformed_json_strategy()
    deep = deep_nesting_strategy()
    raw = raw_byte_text_strategy()

    if profile == "valid-heavy":
        return st.one_of(valid, valid, valid, deep, malformed)
    if profile == "deep-heavy":
        return st.one_of(valid, deep, deep, malformed)
    if profile == "malformed-heavy":
        return st.one_of(valid, malformed, malformed, raw)
    if profile == "adaptation-heavy":
        adaptations = st.sampled_from(
            [
                '{"a":1.}',
                '{"\\u0000":"x"}',
                '{"a":"\\ud83d"}',
                '{"a":"\\ude00\\ud83d"}',
                '{"a":1}garbage',
                "\ufeff" + '{"a":1}',
                '{"a":1,"a":2}',
            ]
        )
        return st.one_of(valid, adaptations, adaptations, malformed, deep)
    return st.one_of(valid, valid, malformed, deep)


def example_strategy():
    """Default name used by generated/refined loop code."""
    return json_text_strategy("seed")
