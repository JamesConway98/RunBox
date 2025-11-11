from __future__ import annotations

import pytest

from runbox_api import parsing


def parse(text: str, filename: str = "cases.jsonl"):
    return parsing.parse(text.encode("utf-8"), filename)


class TestJSONL:
    def test_objects_with_input_and_expected(self):
        cases = parse(
            '{"input": "2+2?", "expected": "4"}\n'
            '{"input": "capital of France?", "expected": "Paris"}\n'
        )
        assert [c.input for c in cases] == ["2+2?", "capital of France?"]
        assert [c.expected for c in cases] == ["4", "Paris"]
        assert [c.idx for c in cases] == [0, 1]

    def test_bare_strings_are_a_valid_dataset(self):
        # Rejecting a file of plain prompts would be pedantry.
        cases = parse('"first prompt"\n"second prompt"\n')
        assert [c.input for c in cases] == ["first prompt", "second prompt"]

    def test_alternative_field_names(self):
        cases = parse('{"prompt": "hello", "answer": "world"}\n')
        assert cases[0].input == "hello"
        assert cases[0].expected == "world"

    def test_extra_fields_become_metadata(self):
        cases = parse('{"input": "x", "category": "math", "difficulty": 3}\n')
        assert cases[0].metadata == {"category": "math", "difficulty": 3}

    def test_blank_lines_are_skipped_not_errors(self):
        cases = parse('{"input": "a"}\n\n\n{"input": "b"}\n')
        assert len(cases) == 2
        # Indices stay contiguous, so a blank line does not leave a hole.
        assert [c.idx for c in cases] == [0, 1]

    def test_error_names_the_line_number(self):
        with pytest.raises(parsing.ParseError) as exc:
            parse('{"input": "ok"}\n{not json}\n')
        assert "line 2" in str(exc.value)

    def test_missing_input_field_lists_what_was_expected(self):
        with pytest.raises(parsing.ParseError) as exc:
            parse('{"question_text": "x"}\n')
        message = str(exc.value).lower()
        assert "line 1" in message
        assert "input" in message

    def test_empty_file_is_rejected(self):
        with pytest.raises(parsing.ParseError, match="no cases"):
            parse("\n\n")


class TestCSV:
    def test_header_and_rows(self):
        cases = parse("input,expected\n2+2?,4\ncapital of France?,Paris\n", "cases.csv")
        assert [c.input for c in cases] == ["2+2?", "capital of France?"]
        assert [c.expected for c in cases] == ["4", "Paris"]

    def test_bom_from_excel_does_not_corrupt_the_header(self):
        # Excel writes a UTF-8 BOM by default, which would otherwise turn the
        # first column name into "﻿input" and fail to match.
        content = "﻿input,expected\nhello,world\n".encode()
        cases = parsing.parse(content, "cases.csv")
        assert cases[0].input == "hello"

    def test_column_names_are_case_insensitive(self):
        cases = parse("Input,Expected\nhello,world\n", "cases.csv")
        assert cases[0].input == "hello"
        assert cases[0].expected == "world"

    def test_trailing_blank_rows_are_skipped(self):
        cases = parse("input\nhello\n\n\n", "cases.csv")
        assert len(cases) == 1

    def test_missing_input_column_names_what_was_found(self):
        with pytest.raises(parsing.ParseError) as exc:
            parse("foo,bar\n1,2\n", "cases.csv")
        assert "foo" in str(exc.value)

    def test_extra_columns_become_metadata(self):
        cases = parse("input,category\nhello,greeting\n", "cases.csv")
        assert cases[0].metadata == {"category": "greeting"}


class TestLimits:
    def test_unsupported_extension(self):
        with pytest.raises(parsing.ParseError, match="Unsupported file type"):
            parse('{"input": "x"}', "cases.txt")

    def test_invalid_utf8_reports_the_byte_offset(self):
        with pytest.raises(parsing.ParseError, match="byte"):
            parsing.parse(b'{"input": "\xff\xfe bad"}', "cases.jsonl")

    def test_oversized_input_is_rejected(self):
        huge = "x" * (parsing.MAX_INPUT_CHARS + 1)
        with pytest.raises(parsing.ParseError, match="limit"):
            parse(f'{{"input": "{huge}"}}\n')

    def test_too_many_cases_is_rejected(self):
        rows = "\n".join(f'{{"input": "case {i}"}}' for i in range(parsing.MAX_CASES + 2))
        with pytest.raises(parsing.ParseError, match="more than"):
            parse(rows)
