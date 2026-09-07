"""Standard-library HTTP transport, persistent response cache, and retry header parsing."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen


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
        try:
            with closing(urlopen(request, timeout=timeout_seconds)) as response:  # noqa: S310
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                return HttpResponse(int(response.status), response_headers, response.read())
        except HTTPError as error:
            with closing(error):
                response_headers = {key.lower(): value for key, value in error.headers.items()}
                return HttpResponse(int(error.code), response_headers, error.read())


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
