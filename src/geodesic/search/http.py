"""Small HTTP GET client for documented provider APIs (httpx).

Behaviour, all configurable:

* a minimum interval between requests (client-side rate limiting);
* bounded retries with exponential backoff for transport errors and HTTP
  429/500/502/503/504, honouring ``Retry-After`` (seconds) up to a cap;
* a per-request timeout;
* log records and returned request descriptions never contain credentials,
  contact addresses or sensitive headers.

The transport can be replaced (``httpx.MockTransport``) so tests never touch the network.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from ..errors import RetrievalError
from ..logging_utils import redact, redact_headers, redact_params, register_secret

log = logging.getLogger("geodesic.http")

RETRYABLE_STATUS = (429, 500, 502, 503, 504)
MAX_RETRY_AFTER_SECONDS = 120.0


@dataclass
class HttpResponse:
    """A completed GET. ``request_params`` and ``request_url`` are already redacted."""

    status: int
    content: bytes
    request_url: str
    request_params: dict[str, Any]
    attempts: int
    retries: list[dict[str, Any]] = field(default_factory=list)   # one entry per failed attempt
    content_type: str | None = None


class HttpClient:
    """Rate-limited GET client with bounded retries."""

    def __init__(self, *, min_interval_seconds: float = 0.5, max_retries: int = 4, backoff_seconds: float = 1.0,
                 timeout_seconds: float = 30.0, headers: dict[str, str] | None = None,
                 secret_params: dict[str, str] | None = None, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.min_interval = max(0.0, float(min_interval_seconds))
        self.max_retries = max(0, int(max_retries))
        self.backoff = max(0.0, float(backoff_seconds))
        self.headers = dict(headers or {})
        # parameters added to every request but never stored or logged (contact address, API key)
        self.secret_params = {k: v for k, v in (secret_params or {}).items() if v}
        for value in self.secret_params.values():
            register_secret(str(value))
        self._sleep, self._clock = sleep, clock
        self._last_request: float | None = None
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport, headers=self.headers,
                                    follow_redirects=True)
        self.requests_made = 0

    # -------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def described_headers(self) -> dict[str, str]:
        """Request headers with sensitive ones masked (for run records)."""
        return redact_headers(self.headers)

    # -------------------------------------------------------------- requests
    def _wait_turn(self) -> None:
        if self._last_request is not None and self.min_interval:
            wait = self.min_interval - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def get(self, url: str, params: dict[str, Any] | None = None) -> HttpResponse:
        """GET ``url``. Raises :class:`RetrievalError` after the retry budget or on a non-retryable status."""
        public = redact_params(params)
        sent = {**(params or {}), **self.secret_params}
        retries: list[dict[str, Any]] = []
        attempt = 0
        while True:
            attempt += 1
            self._wait_turn()
            self.requests_made += 1
            problem: str
            retry_after: float | None = None
            try:
                resp = self._client.get(url, params=sent)
            except httpx.HTTPError as exc:
                problem = f"{type(exc).__name__}: {redact(str(exc))}"
                status = None
            else:
                status = resp.status_code
                if status < 400:
                    log.info("http_get", extra={"url": url, "params": public, "status": status,
                                                "attempts": attempt, "bytes": len(resp.content)})
                    return HttpResponse(status=status, content=resp.content, request_url=redact(url),
                                        request_params=public, attempts=attempt, retries=retries,
                                        content_type=resp.headers.get("content-type"))
                problem = f"HTTP {status}"
                if status not in RETRYABLE_STATUS:
                    log.warning("http_get_failed", extra={"url": url, "params": public, "status": status})
                    raise RetrievalError(
                        f"{redact(url)} answered HTTP {status}, which is not retried. Check the query syntax and"
                        f" the provider's documentation. Request parameters: {public}")
                retry_after = _retry_after(resp.headers.get("retry-after"))
            retries.append({"attempt": attempt, "problem": problem, "retry_after": retry_after})
            if attempt > self.max_retries:
                log.warning("http_get_gave_up", extra={"url": url, "params": public, "attempts": attempt,
                                                       "problem": problem})
                raise RetrievalError(
                    f"{redact(url)} could not be retrieved after {attempt} attempts (last problem: {problem})."
                    " The run can be resumed later from its stored cursor.")
            delay = retry_after if retry_after is not None else self.backoff * (2 ** (attempt - 1))
            log.info("http_get_retry", extra={"url": url, "params": public, "attempt": attempt, "problem": problem,
                                              "delay_seconds": delay})
            if delay > 0:
                self._sleep(delay)


def _retry_after(value: str | None) -> float | None:
    """``Retry-After`` in seconds, capped; HTTP-date forms are ignored (backoff applies instead)."""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)
