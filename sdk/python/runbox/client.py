"""The client.

Sync by default. Most people reaching for this are writing a script, and
`async` in a script is a tax on everyone who did not need it. `AsyncRunbox` is
there for those who do.
"""

from __future__ import annotations

import os
import random
import time
from typing import Any, Callable, Iterator

import httpx

from . import errors
from .streaming import MAX_ATTEMPTS, EventStream
from .types import Event, RunData, Usage

# Localhost until the control plane is deployed. Override with
# RUNBOX_BASE_URL or base_url= once it is.
DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 30.0
USER_AGENT = "runbox-python/0.1.0"

# Only these are retried, and only on idempotent requests. Retrying a POST that
# may have already created a run is how you get two runs.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class Run:
    """A handle to one run.

    Returned by `create`. Streaming, waiting and cancelling all hang off it, so
    the object a caller holds is the object they act on.
    """

    def __init__(self, client: Runbox, data: RunData) -> None:
        self._client = client
        self._data = data

    # Delegating properties rather than exposing `.data`. It keeps the happy
    # path (`run.result`) short, which is the whole point of the SDK.
    @property
    def id(self) -> str:
        return self._data.id

    @property
    def status(self) -> str:
        return self._data.status

    @property
    def result(self) -> str | None:
        return self._data.result

    @property
    def error(self) -> str | None:
        return self._data.error

    @property
    def usage(self) -> Usage:
        return self._data.usage

    @property
    def duration_ms(self) -> int | None:
        return self._data.duration_ms

    @property
    def data(self) -> RunData:
        return self._data

    def refresh(self) -> Run:
        self._data = self._client.get(self.id).data
        return self

    def stream(
        self,
        after: int = 0,
        *,
        max_attempts: int = MAX_ATTEMPTS,
        on_reconnect: Callable[[int, int], None] | None = None,
    ) -> Iterator[Event]:
        """Iterate the trace as it happens.

        Reconnects on its own from the last seq seen. On the final event the
        local state is refreshed, so `run.result` is populated the moment the
        loop ends rather than requiring a separate call.

        `on_reconnect(attempt, last_seq)` fires before each retry, for callers
        who want to log a blip rather than have it be invisible.
        """
        stream = EventStream(
            self._client._http,
            f"{self._client.base_url}/v1/runs/{self.id}/stream",
            self._client._headers(accept="text/event-stream"),
            after=after,
            max_attempts=max_attempts,
            on_reconnect=on_reconnect,
        )
        for event in stream:
            yield event
            if event.is_final:
                self.refresh()

    def wait(self, timeout: float = 300.0, poll_interval: float = 1.0) -> Run:
        """Block until the run reaches a terminal state.

        Polls rather than streams. A caller who only wants the answer should not
        pay for the trace, and this keeps `wait()` usable where a long-lived
        connection is awkward — behind a proxy, in a lambda.
        """
        deadline = time.monotonic() + timeout
        while True:
            self.refresh()
            if self._data.is_terminal:
                return self
            if time.monotonic() >= deadline:
                raise errors.TimeoutError(
                    f"run {self.id} was still {self._data.status} after {timeout}s"
                )
            time.sleep(poll_interval)

    def cancel(self) -> Run:
        self._data = self._client.cancel(self.id).data
        return self

    def events(self, after: int = 0) -> list[Event]:
        """The full trace, as a list. For a finished run."""
        return self._client.events(self.id, after=after)

    def __repr__(self) -> str:
        return f"<Run {self.id} {self.status}>"


class _Runs:
    """The `client.runs` namespace."""

    def __init__(self, client: Runbox) -> None:
        self._client = client

    def create(
        self,
        task: str,
        *,
        model: str = "claude-sonnet-5",
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        timeout_s: int = 120,
        max_tokens: int = 20_000,
    ) -> Run:
        body: dict[str, Any] = {
            "task": task,
            "model": model,
            "tools": tools or [],
            "timeout_s": timeout_s,
            "max_tokens": max_tokens,
        }
        if system_prompt is not None:
            body["system_prompt"] = system_prompt
        if temperature is not None:
            body["temperature"] = temperature

        created = self._client._request("POST", "/v1/runs", json=body)
        # The create response is minimal by design (202 with an id). Fetching
        # gives the caller a fully populated object, which is what they expect
        # from something called `create`.
        return self._client.get(created["id"])

    def get(self, run_id: str) -> Run:
        return self._client.get(run_id)

    def list(
        self,
        *,
        limit: int = 25,
        cursor: str | None = None,
        status: str | None = None,
        model: str | None = None,
    ) -> list[Run]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if status:
            params["status"] = status
        if model:
            params["model"] = model

        page = self._client._request("GET", "/v1/runs", params=params)
        return [Run(self._client, RunData.from_api(item)) for item in page["data"]]

    def iterate(self, *, status: str | None = None, model: str | None = None) -> Iterator[Run]:
        """Every run, following cursors.

        A generator so a caller can stop early without having fetched the rest.
        """
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            if status:
                params["status"] = status
            if model:
                params["model"] = model

            page = self._client._request("GET", "/v1/runs", params=params)
            for item in page["data"]:
                yield Run(self._client, RunData.from_api(item))

            if not page.get("has_more") or not page.get("next_cursor"):
                return
            cursor = page["next_cursor"]


class Runbox:
    """Runbox API client."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 3,
    ) -> None:
        self.api_key = api_key or os.environ.get("RUNBOX_API_KEY", "")
        if not self.api_key:
            raise errors.RunboxError(
                "No API key. Pass api_key= or set the RUNBOX_API_KEY environment variable."
            )

        self.base_url = (base_url or os.environ.get("RUNBOX_BASE_URL") or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self.max_retries = max_retries
        self._http = httpx.Client(timeout=timeout, follow_redirects=True)
        self.runs = _Runs(self)

    def __enter__(self) -> Runbox:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": accept,
            "User-Agent": USER_AGENT,
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        # Only idempotent methods are retried. A retried POST that already
        # succeeded creates a second run and bills for it.
        retryable = method in ("GET", "HEAD")
        attempts = self.max_retries if retryable else 1

        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self._http.request(method, url, headers=self._headers(), **kwargs)
            except httpx.HTTPError as exc:
                last = errors.ConnectionError(f"could not reach {url}: {exc}")
                if attempt == attempts:
                    raise last from exc
                self._sleep(attempt)
                continue

            if response.status_code < 400:
                return None if response.status_code == 204 else response.json()

            body = self._safe_json(response)
            retry_after = self._retry_after(response)
            error = errors.from_response(response.status_code, body, retry_after)

            if retryable and response.status_code in RETRY_STATUSES and attempt < attempts:
                # Honour Retry-After when the server sent one. Guessing when we
                # have been told is rude and usually wrong.
                self._sleep(attempt, retry_after)
                last = error
                continue

            raise error

        raise last or errors.RunboxError("request failed")

    def _sleep(self, attempt: int, retry_after: float | None = None) -> None:
        if retry_after is not None:
            time.sleep(min(retry_after, 30.0))
            return
        # Jittered, so a fleet of clients retrying after an outage does not
        # arrive in lockstep and knock the service over again.
        delay = min(0.5 * (2 ** (attempt - 1)), 8.0)
        time.sleep(delay + random.uniform(0, delay * 0.25))

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict:
        try:
            body = response.json()
            return body if isinstance(body, dict) else {"message": str(body)}
        except ValueError:
            return {"message": response.text[:200] or response.reason_phrase}

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    # --- convenience -------------------------------------------------------

    def get(self, run_id: str) -> Run:
        return Run(self, RunData.from_api(self._request("GET", f"/v1/runs/{run_id}")))

    def cancel(self, run_id: str) -> Run:
        return Run(self, RunData.from_api(self._request("POST", f"/v1/runs/{run_id}/cancel")))

    def events(self, run_id: str, after: int = 0) -> list[Event]:
        collected: list[Event] = []
        cursor = after
        while True:
            page = self._request(
                "GET", f"/v1/runs/{run_id}/events", params={"after": cursor, "limit": 500}
            )
            for item in page["data"]:
                collected.append(
                    Event(seq=item["seq"], type=item["type"], payload=item.get("payload", {}))
                )
            if not page.get("has_more") or not collected:
                return collected
            cursor = collected[-1].seq

    def usage(
        self, *, start: str | None = None, end: str | None = None, group_by: str = "day"
    ) -> dict:
        params: dict[str, Any] = {"group_by": group_by}
        if start:
            params["from"] = start
        if end:
            params["to"] = end
        return self._request("GET", "/v1/usage", params=params)
