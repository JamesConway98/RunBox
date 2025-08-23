from __future__ import annotations

import json

from runbox_api.sse import _payload_of, format_comment, format_event


def test_format_event_frame_shape():
    frame = format_event(14, "tool_call", {"seq": 14, "tool": "http_get"})

    lines = frame.split("\n")
    assert lines[0] == "id: 14"
    assert lines[1] == "event: tool_call"
    assert lines[2].startswith("data: ")
    # A frame is terminated by a blank line. Without it the client buffers
    # forever and the stream looks dead.
    assert frame.endswith("\n\n")

    assert json.loads(lines[2][6:]) == {"seq": 14, "tool": "http_get"}


def test_format_event_keeps_data_on_one_line():
    # A newline inside data: would be read as a field separator and split the
    # event in two. json.dumps escapes it, but the guarantee is worth a test.
    frame = format_event(1, "token", {"text": "line one\nline two"})

    data_lines = [ln for ln in frame.split("\n") if ln.startswith("data: ")]
    assert len(data_lines) == 1
    assert json.loads(data_lines[0][6:])["text"] == "line one\nline two"


def test_format_event_handles_unicode():
    frame = format_event(2, "token", {"text": "café — 日本語"})
    payload = json.loads([ln for ln in frame.split("\n") if ln.startswith("data: ")][0][6:])
    assert payload["text"] == "café — 日本語"


def test_format_comment_is_ignored_by_clients():
    assert format_comment("heartbeat") == ": heartbeat\n\n"


class TestPayloadNormalisation:
    def test_dict_passes_through(self):
        assert _payload_of({"payload": {"text": "hi"}}) == {"text": "hi"}

    def test_json_string_is_decoded(self):
        assert _payload_of({"payload": '{"text":"hi"}'}) == {"text": "hi"}

    def test_undecodable_string_is_preserved_not_dropped(self):
        # Losing the content entirely would be worse than showing it raw.
        assert _payload_of({"payload": "not json"}) == {"raw": "not json"}

    def test_missing_payload_is_empty(self):
        assert _payload_of({}) == {}
