"""Dataset file parsing.

JSONL and CSV. Both are parsed with a hard cap on rows and bytes, because an
upload endpoint that will happily read a 4GB file into memory is a denial of
service with a friendly name.

Errors name the line number. "Invalid JSON on line 1" is a bug report someone
can act on; "invalid file" is a support ticket.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Any

MAX_CASES = 10_000
MAX_INPUT_CHARS = 20_000

# Column names accepted for the input field, in order of preference. Being
# permissive here costs one tuple and saves every user from renaming a column
# before they can try the product.
INPUT_KEYS = ("input", "prompt", "question", "text")
EXPECTED_KEYS = ("expected", "answer", "output", "label")


class ParseError(ValueError):
    pass


@dataclass
class Case:
    idx: int
    input: str
    expected: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def parse(content: bytes, filename: str) -> list[Case]:
    text = _decode(content)
    if filename.lower().endswith(".csv"):
        return _parse_csv(text)
    if filename.lower().endswith((".jsonl", ".ndjson")):
        return _parse_jsonl(text)
    raise ParseError("Unsupported file type. Upload a .jsonl or .csv file.")


def _decode(content: bytes) -> str:
    try:
        # A BOM from Excel is extremely common in uploaded CSVs and would
        # otherwise turn the first column name into "﻿input".
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError(f"File is not valid UTF-8 (byte {exc.start}).") from exc


def _parse_jsonl(text: str) -> list[Case]:
    cases: list[Case] = []

    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue  # blank lines are not an error, they are a text file

        try:
            record = json.loads(line)
        except ValueError as exc:
            raise ParseError(f"Invalid JSON on line {line_number}: {exc.msg}") from exc

        if isinstance(record, str):
            # A file of bare strings is a perfectly reasonable dataset and
            # rejecting it would be pedantry.
            cases.append(_make_case(len(cases), record, None, {}, line_number))
            continue

        if not isinstance(record, dict):
            raise ParseError(
                f"Line {line_number} is a {type(record).__name__}; expected an object or a string."
            )

        value = _first_present(record, INPUT_KEYS)
        if value is None:
            raise ParseError(
                f"Line {line_number} has no input field. "
                f"Expected one of: {', '.join(INPUT_KEYS)}."
            )

        expected = _first_present(record, EXPECTED_KEYS)
        metadata = {k: v for k, v in record.items() if k not in INPUT_KEYS + EXPECTED_KEYS}
        cases.append(_make_case(len(cases), value, expected, metadata, line_number))

        _check_limit(cases)

    if not cases:
        raise ParseError("File contains no cases.")
    return cases


def _parse_csv(text: str) -> list[Case]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ParseError("CSV has no header row.")

    headers = {name.strip().lower(): name for name in reader.fieldnames if name}
    input_column = next((headers[k] for k in INPUT_KEYS if k in headers), None)
    if input_column is None:
        raise ParseError(
            f"CSV has no input column. Expected one of: {', '.join(INPUT_KEYS)}. "
            f"Found: {', '.join(reader.fieldnames)}."
        )
    expected_column = next((headers[k] for k in EXPECTED_KEYS if k in headers), None)

    cases: list[Case] = []
    for line_number, row in enumerate(reader, start=2):  # line 1 is the header
        value = (row.get(input_column) or "").strip()
        if not value:
            continue  # a trailing blank row is normal in a spreadsheet export

        expected = (row.get(expected_column) or "").strip() if expected_column else None
        metadata = {
            k: v
            for k, v in row.items()
            if k not in (input_column, expected_column) and v not in (None, "")
        }
        cases.append(_make_case(len(cases), value, expected or None, metadata, line_number))
        _check_limit(cases)

    if not cases:
        raise ParseError("CSV contains no rows with a non-empty input.")
    return cases


def _make_case(
    idx: int, value: Any, expected: Any, metadata: dict, line_number: int
) -> Case:
    text = value if isinstance(value, str) else json.dumps(value)
    if not text.strip():
        raise ParseError(f"Line {line_number} has an empty input.")
    if len(text) > MAX_INPUT_CHARS:
        raise ParseError(
            f"Line {line_number} input is {len(text)} characters; the limit is {MAX_INPUT_CHARS}."
        )

    return Case(
        idx=idx,
        input=text,
        expected=str(expected) if expected is not None else None,
        metadata=metadata,
    )


def _first_present(record: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _check_limit(cases: list[Case]) -> None:
    if len(cases) > MAX_CASES:
        raise ParseError(f"File has more than {MAX_CASES:,} cases.")
