"""Standard-library HTTP transport, persistent response cache, and retry header parsing."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class ResponseLimitError(OSError):
    """A response exceeds the permitted resource budget; retrying cannot repair it."""


@dataclass(frozen=True, slots=True)
class RequestBudget:
    """One monotonic deadline shared by retries and sequential or concurrent requests."""

    deadline: float
    monotonic: Callable[[], float] = time.monotonic

    @classmethod
    def start(
        cls, seconds: float, monotonic: Callable[[], float] = time.monotonic
    ) -> RequestBudget:
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("network budget must be finite and positive")
        return cls(monotonic() + seconds, monotonic)

    def remaining(self) -> float:
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise TimeoutError("network operation exceeded its overall time budget")
        return remaining

    def wait(self, delay: float, sleep: Callable[[float], None]) -> None:
        if delay >= self.remaining():
            raise TimeoutError("provider retry delay exceeds the time budget; try again later")
        if delay > 0:
            sleep(delay)
        self.remaining()


@runtime_checkable
class _ResponseBody(Protocol):
    def read1(self, size: int, /) -> bytes: ...


def read_response_body(
    response: _ResponseBody, headers: Mapping[str, str], budget: RequestBudget
) -> bytes:
    expected = headers.get("content-length")
    length = int(expected) if expected is not None and expected.isdecimal() else None
    if length is not None and length > MAX_RESPONSE_BYTES:
        raise ResponseLimitError("HTTP response exceeds the 16 MiB body limit")
    body = bytearray()
    try:
        while True:
            budget.remaining()
            chunk = response.read1(min(64 * 1024, MAX_RESPONSE_BYTES + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ResponseLimitError("HTTP response exceeds the 16 MiB body limit")
    except IncompleteRead as error:
        raise OSError("HTTP response ended before its body was complete") from error
    if length is not None and len(body) != length:
        raise OSError("HTTP response ended before its declared Content-Length")
    return bytes(body)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse: ...


class UrllibTransport:
    """stdlib transport; HTTP errors are returned so retry policy stays with the caller."""

    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        request = Request(url, headers=dict(headers), method="GET")
        budget = RequestBudget.start(timeout_seconds)
        try:
            with closing(urlopen(request, timeout=timeout_seconds)) as response:  # noqa: S310
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                body = read_response_body(response, response_headers, budget)
                return HttpResponse(int(response.status), response_headers, body)
        except HTTPError as error:
            with closing(error):
                response_headers = {key.lower(): value for key, value in error.headers.items()}
                if not isinstance(error.fp, _ResponseBody):
                    raise OSError("HTTP error response has no readable body") from error
                body = read_response_body(error.fp, response_headers, budget)
                return HttpResponse(int(error.code), response_headers, body)


@dataclass(frozen=True, slots=True)
class CacheEntry:
    key: str
    etag: str | None
    expires_epoch: float
    body: bytes
    headers: Mapping[str, str]


class ResponseCache:
    """Persistent HTTP cache storing an opaque request key and its response metadata."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS response_cache (
                       url TEXT PRIMARY KEY,
                       etag TEXT,
                       expires_epoch REAL NOT NULL,
                       body BLOB NOT NULL,
                       headers_json TEXT NOT NULL
                   )"""
            )

    def get(self, key: str) -> CacheEntry | None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            row = connection.execute(
                "SELECT url, etag, expires_epoch, body, headers_json "
                "FROM response_cache WHERE url = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return CacheEntry(
            str(row[0]), row[1], float(row[2]), bytes(row[3]), json.loads(str(row[4]))
        )

    def put(self, record: CacheEntry) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                """INSERT INTO response_cache(url, etag, expires_epoch, body, headers_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET
                     etag=excluded.etag,
                     expires_epoch=excluded.expires_epoch,
                     body=excluded.body,
                     headers_json=excluded.headers_json""",
                (
                    record.key,
                    record.etag,
                    record.expires_epoch,
                    record.body,
                    json.dumps(dict(record.headers), sort_keys=True),
                ),
            )


def expiry_epoch(headers: Mapping[str, str], *, now_epoch: float) -> float:
    expires = headers.get("expires")
    if not expires:
        return now_epoch
    try:
        return parsedate_to_datetime(expires).timestamp()
    except (TypeError, ValueError, OverflowError):
        return now_epoch


def retry_delay(value: str | None, *, now_epoch: float, default: float) -> float:
    if value is None:
        return default
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - now_epoch
        except (TypeError, ValueError, OverflowError):
            return default
    return max(1.0, delay) if math.isfinite(delay) else default
