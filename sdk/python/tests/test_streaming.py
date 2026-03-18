from __future__ import annotations

import httpx
import pytest
import respx

from runbox import Runbox, StreamError
from runbox.streaming import parse_sse


def sse(*frames: str) -> str:
    return "".join(frames)


def _frames(*pairs: tuple[str, str]) -> list[str]:
    """Build SSE lines for (id, text) pairs, one frame each."""
    lines: list[str] = []
    for seq, text in pairs:
        lines += [f"id: {seq}", "event: token", f'data: {{"text":"{text}"}}', ""]
    return lines


class TestParseSSE:
    def test_single_frame(self):
        events = list(parse_sse(iter(["id: 1", "event: token", 'data: {"text":"hi"}', ""])))
        assert events == [(1, "token", {"text": "hi"})]

    def test_multiple_frames(self):
        lines = _frames(("1", "a"), ("2", "b"))
        assert [e[0] for e in parse_sse(iter(lines))] == [1, 2]

    def test_comments_are_heartbeats_and_are_discarded(self):
        lines = [": heartbeat", "", "id: 1", "event: token", 'data: {"text":"x"}', ""]
        assert len(list(parse_sse(iter(lines)))) == 1

    def test_data_spanning_multiple_lines_is_joined(self):
        # The SSE spec allows it, so the parser has to handle it even though our
        # server never emits it.
        lines = ["id: 1", "event: token", 'data: {"text":', 'data: "split"}', ""]
        assert list(parse_sse(iter(lines))) == [(1, "token", {"text": "split"})]

    def test_undecodable_frame_is_skipped_not_fatal(self):
        lines = ["id: 1", "event: token", "data: {not json", "", *_frames(("2", "ok"))]
        events = list(parse_sse(iter(lines)))
        assert events == [(2, "token", {"text": "ok"})]

    def test_leading_space_after_colon_is_stripped_once_only(self):
        # "data:  x" means a value of " x", not "x".
        events = list(parse_sse(iter(["id: 1", "event: token", 'data:  {"text":"x"}', ""])))
        assert events == [(1, "token", {"text": "x"})]

    def test_frame_without_terminating_blank_line_is_not_emitted(self):
        # An unterminated frame is a truncated response, not a valid event.
        assert list(parse_sse(iter(["id: 1", "event: token", 'data: {"text":"x"}']))) == []


FINAL = 'data: {"status":"succeeded","result":"42","usage":{"input_tokens":10}}'


@respx.mock
def test_stream_yields_events_and_stops_at_final():
    body = sse(
        'id: 1\nevent: token\ndata: {"text":"4"}\n\n',
        'id: 2\nevent: token\ndata: {"text":"2"}\n\n',
        f"id: 3\nevent: final\n{FINAL}\n\n",
    )
    respx.get(url__regex=r".*/v1/runs/r1/stream.*").mock(
        return_value=httpx.Response(200, text=body)
    )
    respx.get(url__regex=r".*/v1/runs/r1$").mock(
        return_value=httpx.Response(200, json={"id": "r1", "status": "succeeded", "result": "42"})
    )

    client = Runbox(api_key="rb_live_test", base_url="http://api.test")
    run = client.get("r1")
    events = list(run.stream())

    assert [e.seq for e in events] == [1, 2, 3]
    assert events[-1].is_final
    # The final event refreshes local state, so result is there without a
    # separate call.
    assert run.result == "42"


@respx.mock
def test_stream_resumes_from_last_seq_after_a_drop():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            # Drops after seq 2 with no final event.
            return httpx.Response(
                200,
                text=sse(
                    'id: 1\nevent: token\ndata: {"text":"a"}\n\n',
                    'id: 2\nevent: token\ndata: {"text":"b"}\n\n',
                ),
            )
        return httpx.Response(200, text=sse(f"id: 3\nevent: final\n{FINAL}\n\n"))

    respx.get(url__regex=r".*/v1/runs/r1/stream.*").mock(side_effect=handler)
    respx.get(url__regex=r".*/v1/runs/r1$").mock(
        return_value=httpx.Response(200, json={"id": "r1", "status": "succeeded"})
    )

    client = Runbox(api_key="rb_live_test", base_url="http://api.test")
    events = list(client.get("r1").stream())

    assert [e.seq for e in events] == [1, 2, 3]
    # The reconnect must carry the cursor, or the server replays from the top
    # and the caller sees every event twice.
    assert "after=2" in calls[1]


@respx.mock
def test_replayed_events_are_not_yielded_twice():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(200, text=sse('id: 1\nevent: token\ndata: {"text":"a"}\n\n'))
        # A server replaying from the cursor legitimately re-sends seq 1.
        return httpx.Response(
            200,
            text=sse(
                'id: 1\nevent: token\ndata: {"text":"a"}\n\n',
                f"id: 2\nevent: final\n{FINAL}\n\n",
            ),
        )

    respx.get(url__regex=r".*/v1/runs/r1/stream.*").mock(side_effect=handler)
    respx.get(url__regex=r".*/v1/runs/r1$").mock(
        return_value=httpx.Response(200, json={"id": "r1", "status": "succeeded"})
    )

    client = Runbox(api_key="rb_live_test", base_url="http://api.test")
    events = list(client.get("r1").stream())

    assert [e.seq for e in events] == [1, 2]


@respx.mock
def test_stream_gives_up_after_max_attempts():
    respx.get(url__regex=r".*/v1/runs/r1/stream.*").mock(side_effect=httpx.ConnectError("refused"))
    respx.get(url__regex=r".*/v1/runs/r1$").mock(
        return_value=httpx.Response(200, json={"id": "r1", "status": "running"})
    )

    client = Runbox(api_key="rb_live_test", base_url="http://api.test")
    # Two attempts rather than the default six: this asserts that it gives up,
    # and sleeping through the full backoff to prove it would add 15s to CI.
    with pytest.raises(StreamError, match="failed after"):
        list(client.get("r1").stream(max_attempts=2))
