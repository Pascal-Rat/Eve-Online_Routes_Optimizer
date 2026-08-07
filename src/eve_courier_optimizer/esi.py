"""Small, conservative ESI client for public courier-contract discovery."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Final, Protocol, cast
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .domain import (
    PublicCourierContract,
    SystemKillActivity,
    cargo_volume_to_units,
    isk_to_units,
    parse_esi_datetime,
)

ESI_BASE_URL: Final = "https://esi.evetech.net"
ESI_COMPATIBILITY_DATE: Final = "2026-08-05"
DEFAULT_USER_AGENT: Final = "eve-courier-route-optimizer/1.5.0 (+local EVE route planning)"


class EsiError(RuntimeError):
    """Base error for ESI access."""


class EsiHttpError(EsiError):
    def __init__(self, status: int, url: str, body: bytes) -> None:
        preview = body[:300].decode("utf-8", errors="replace")
        super().__init__(f"ESI returned HTTP {status} for {url}: {preview}")
        self.status = status
        self.url = url


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse: ...


class UrllibTransport:
    """stdlib transport; HTTP errors are returned so retry policy stays in ``EsiClient``."""

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
class CacheRecord:
    url: str
    etag: str | None
    expires_epoch: float
    body: bytes
    headers_json: str


class EsiResponseCache:
    """Persistent HTTP cache honoring ESI ``Expires`` and ``ETag`` values."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS response_cache (
                       url TEXT PRIMARY KEY,
                       etag TEXT,
                       expires_epoch REAL NOT NULL,
                       body BLOB NOT NULL,
                       headers_json TEXT NOT NULL
                   )"""
            )

    def get(self, url: str) -> CacheRecord | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT url, etag, expires_epoch, body, headers_json "
                "FROM response_cache WHERE url = ?",
                (url,),
            ).fetchone()
        if row is None:
            return None
        return CacheRecord(str(row[0]), row[1], float(row[2]), bytes(row[3]), str(row[4]))

    def put(self, record: CacheRecord) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """INSERT INTO response_cache(url, etag, expires_epoch, body, headers_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET
                     etag=excluded.etag,
                     expires_epoch=excluded.expires_epoch,
                     body=excluded.body,
                     headers_json=excluded.headers_json""",
                (
                    record.url,
                    record.etag,
                    record.expires_epoch,
                    record.body,
                    record.headers_json,
                ),
            )


def _expiry_epoch(headers: Mapping[str, str], *, now_epoch: float) -> float:
    expires = headers.get("expires")
    if not expires:
        return now_epoch
    try:
        return parsedate_to_datetime(expires).timestamp()
    except (TypeError, ValueError, OverflowError):
        return now_epoch


def _headers_from_cache(record: CacheRecord) -> dict[str, str]:
    decoded = json.loads(record.headers_json)
    if not isinstance(decoded, dict):
        return {}
    return {str(key): str(value) for key, value in decoded.items()}


class EsiClient:
    """ESI client intentionally optimized for correctness and low request volume, not bursts."""

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        cache: EsiResponseCache | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        compatibility_date: str = ESI_COMPATIBILITY_DATE,
        timeout_seconds: float = 30.0,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.transport = transport or UrllibTransport()
        self.cache = cache
        self.user_agent = user_agent
        self.compatibility_date = compatibility_date
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.sleep = sleep
        self.now = now

    def _get_json(self, path: str, query: Mapping[str, int | str]) -> tuple[Any, Mapping[str, str]]:
        encoded_query = urlencode(query)
        url = f"{ESI_BASE_URL}{path}"
        if encoded_query:
            url = f"{url}?{encoded_query}"
        cached = self.cache.get(url) if self.cache is not None else None
        now_epoch = self.now()
        if cached is not None and cached.expires_epoch > now_epoch:
            return json.loads(cached.body, parse_float=Decimal), _headers_from_cache(cached)

        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            "X-Compatibility-Date": self.compatibility_date,
        }
        if cached is not None and cached.etag:
            headers["If-None-Match"] = cached.etag

        for attempt in range(self.max_retries + 1):
            response = self.transport.get(url, headers, self.timeout_seconds)
            if response.status == 304 and cached is not None:
                merged_headers = _headers_from_cache(cached)
                merged_headers.update(response.headers)
                refreshed = CacheRecord(
                    url=url,
                    etag=cached.etag,
                    expires_epoch=_expiry_epoch(merged_headers, now_epoch=self.now()),
                    body=cached.body,
                    headers_json=json.dumps(merged_headers, sort_keys=True),
                )
                if self.cache is not None:
                    self.cache.put(refreshed)
                return json.loads(cached.body, parse_float=Decimal), merged_headers
            if 200 <= response.status < 300:
                if self.cache is not None:
                    self.cache.put(
                        CacheRecord(
                            url=url,
                            etag=response.headers.get("etag"),
                            expires_epoch=_expiry_epoch(response.headers, now_epoch=self.now()),
                            body=response.body,
                            headers_json=json.dumps(dict(response.headers), sort_keys=True),
                        )
                    )
                return json.loads(response.body, parse_float=Decimal), response.headers
            if response.status == 429 and attempt < self.max_retries:
                retry_after = max(1.0, float(response.headers.get("retry-after", "1")))
                self.sleep(retry_after)
                continue
            if response.status == 420 and attempt < self.max_retries:
                # Legacy ESI error-limit responses expose the reset delay under this header.
                reset_after = max(
                    1.0,
                    float(response.headers.get("x-esi-error-limit-reset", "60")),
                )
                self.sleep(reset_after)
                continue
            if response.status >= 500 and attempt < self.max_retries:
                self.sleep(min(2**attempt, 8))
                continue
            raise EsiHttpError(response.status, url, response.body)
        raise AssertionError("retry loop must return or raise")

    def public_contract_page(
        self,
        region_id: int,
        page: int,
    ) -> tuple[list[dict[str, Any]], int]:
        if region_id <= 0 or page <= 0:
            raise ValueError("region_id and page must be positive")
        payload, headers = self._get_json(f"/contracts/public/{region_id}/", {"page": page})
        if not isinstance(payload, list):
            raise EsiError("public-contract response was not a list")
        rows = [cast(dict[str, Any], row) for row in payload if isinstance(row, dict)]
        pages = int(headers.get("x-pages", "1"))
        return rows, pages

    def public_couriers(self, region_id: int) -> tuple[PublicCourierContract, ...]:
        first_page, page_count = self.public_contract_page(region_id, 1)
        rows = list(first_page)
        for page in range(2, page_count + 1):
            try:
                page_rows, observed_page_count = self.public_contract_page(region_id, page)
            except EsiHttpError as error:
                if error.status == 404:
                    # The live contract set can shrink after page 1. A now-nonexistent trailing
                    # page ends this bounded observation rather than turning normal churn into a
                    # failed scan.
                    break
                raise
            if observed_page_count != page_count:
                # The public market is live. Continue over the original page range for a bounded
                # snapshot; a subsequent replan will observe the new shape.
                pass
            rows.extend(page_rows)
        contracts = [
            contract
            for row in rows
            if (contract := parse_public_courier(row)) is not None
        ]
        # Page boundaries may move during a scan; contract IDs make de-duplication deterministic.
        unique = {contract.contract_id: contract for contract in contracts}
        return tuple(unique[key] for key in sorted(unique))

    def system_kills(self) -> tuple[SystemKillActivity, ...]:
        """Fetch CCP's aggregate system-kill activity snapshot.

        ESI does not label suicide ganks. Consumers must treat ``ship_kills`` as a general danger
        proxy rather than attributing the underlying kills to a cause.
        """

        payload, _headers = self._get_json("/universe/system_kills/", {})
        if not isinstance(payload, list):
            raise EsiError("system-kills response was not a list")
        by_system: dict[int, SystemKillActivity] = {}
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            row = cast(dict[str, Any], raw)
            try:
                item = SystemKillActivity(
                    system_id=int(row["system_id"]),
                    ship_kills=int(row.get("ship_kills", 0)),
                    pod_kills=int(row.get("pod_kills", 0)),
                    npc_kills=int(row.get("npc_kills", 0)),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise EsiError("system-kills response contained an invalid row") from error
            by_system[item.system_id] = item
        return tuple(by_system[key] for key in sorted(by_system))


def parse_public_courier(payload: Mapping[str, Any]) -> PublicCourierContract | None:
    if payload.get("type") != "courier":
        return None
    date_issued_raw = payload.get("date_issued")
    return PublicCourierContract(
        contract_id=int(payload["contract_id"]),
        origin_location_id=int(payload["start_location_id"]),
        destination_location_id=int(payload["end_location_id"]),
        volume_units=cargo_volume_to_units(cast(Decimal | int | float | str, payload["volume"])),
        collateral_units=isk_to_units(
            cast(Decimal | int | float | str, payload.get("collateral", 0))
        ),
        reward_units=isk_to_units(cast(Decimal | int | float | str, payload.get("reward", 0))),
        date_expired=parse_esi_datetime(str(payload["date_expired"])),
        days_to_complete=int(payload["days_to_complete"]),
        title=str(payload.get("title", "")),
        date_issued=parse_esi_datetime(str(date_issued_raw)) if date_issued_raw else None,
    )


def default_cache_path() -> Path:
    return Path.home() / ".cache" / "eve-courier-route-optimizer" / "esi.sqlite3"


def utc_now() -> datetime:
    return datetime.now(UTC)
