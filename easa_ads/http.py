"""Polite HTTP GET for EASA's public files.

One request at a time, a pause between calls, and a few retries for the
errors that are usually transient. This is not a general-purpose client.
"""

from __future__ import annotations

import email.message
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from easa_ads.const import USER_AGENT

# Archive JSON is about 10 MB. Anything larger is refused so a bad response
# cannot exhaust memory.
MAX_BODY_BYTES = 100 * 1024 * 1024
RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class FetchResult:
    url: str
    body: bytes
    headers: email.message.Message


class FetchError(RuntimeError):
    """EASA could not be fetched after retries."""


class HttpClient:
    """GET helper that waits between requests and retries transient failures."""

    def __init__(
        self,
        *,
        user_agent: str = USER_AGENT,
        min_interval: float = 1.0,
        timeout: float = 180.0,
        attempts: int = 4,
    ) -> None:
        if min_interval < 0:
            raise ValueError("min_interval must be >= 0")
        if attempts < 1:
            raise ValueError("attempts must be >= 1")
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.timeout = timeout
        self.attempts = attempts
        self._last_request = 0.0

    def get(self, url: str) -> FetchResult:
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            self._wait()
            try:
                result = self._once(url)
            except urllib.error.HTTPError as exc:
                last_error = exc
                detail = f"HTTP {exc.code} for {url}"
                if exc.code not in RETRY_STATUS or attempt == self.attempts:
                    raise FetchError(detail) from exc
                self._backoff(attempt, exc.headers.get("Retry-After"))
                print(f"{detail}; retry {attempt}/{self.attempts - 1}")
                continue
            except urllib.error.URLError as exc:
                last_error = exc
                detail = f"request failed for {url}: {exc.reason}"
                if attempt == self.attempts:
                    raise FetchError(detail) from exc
                self._backoff(attempt, None)
                print(f"{detail}; retry {attempt}/{self.attempts - 1}")
                continue
            self._last_request = time.monotonic()
            return result
        raise FetchError(f"request failed for {url}: {last_error}")

    def _once(self, url: str) -> FetchResult:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": self.user_agent, "Accept": "*/*"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                raise FetchError(f"response from {url} exceeds {MAX_BODY_BYTES} bytes")
            return FetchResult(url=response.geturl(), body=body, headers=response.headers)

    def _wait(self) -> None:
        if self._last_request == 0.0:
            return
        remaining = self.min_interval - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> None:
        delay = 2.0 * attempt
        if retry_after:
            try:
                delay = min(float(retry_after), 60.0)
            except ValueError:
                pass
        time.sleep(delay)
