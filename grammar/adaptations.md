# Grammar Adaptations for Parson

## Grammar Source

- Format: JSON
- Grammar source: ANTLR grammars-v4 JSON grammar
- Target library: Parson
- Pinned commit: `60b2c69f171f0a66e3ff5afa56de74660032f3d0`

The ANTLR JSON grammar is used as the formal starting point. The following
differences were observed by testing inputs against Parson.

## Observed Differences

| Test case                                      | Example input          | Grammar  | Parson   |
| ---------------------------------------------- | ---------------------- | -------- | -------- |
| Trailing decimal point with no digits after it | `{"a":1.}`             | Rejected | Accepted |
| `\u0000` inside a well-formed object key       | `{"\u0000":"x"}`       | Accepted | Rejected |
| Lone high surrogate with no low surrogate      | `{"a":"\ud83d"}`       | Accepted | Rejected |
| Low surrogate before high surrogate            | `{"a":"\ude00\ud83d"}` | Accepted | Rejected |
| Invalid UTF-8 byte sequence                    | [raw-byte test input]  | Rejected | Accepted |
| Duplicate object key                           | `{"a":1,"a":2}`        | Accepted | Rejected |
| Trailing garbage after JSON value              | `{"a":1}garbage`       | Rejected | Accepted |
| UTF-8 BOM before JSON value                    | `[UTF-8 BOM]{"a":1}`   | Rejected | Accepted |

## Adaptations for the Generator

The ANTLR grammar remains the starting point for generation. The generator
should account for the observed differences between the formal grammar and
Parson's actual behavior. In particular, some inputs rejected by the grammar
are accepted by Parson, while some string and Unicode forms permitted by the
grammar are rejected by Parson.
