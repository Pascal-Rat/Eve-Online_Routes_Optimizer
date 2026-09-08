"""Small, conservative ESI client for public courier-contract discovery."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from http.client import IncompleteRead
from pathlib import Path
from typing import Final, cast
from urllib.parse import urlencode

from eve_courier_optimizer.domain import (
    PublicCourierContract,
    SystemKillActivity,
    cargo_volume_to_units,
    isk_to_units,
    parse_esi_datetime,
)
from eve_courier_optimizer.eve.http import (
    MAX_RESPONSE_BYTES,
    CacheEntry,
    RequestBudget,
    ResponseCache,
    ResponseLimitError,
    Transport,
    UrllibTransport,
    expiry_epoch,
    retry_delay,
)
from eve_courier_optimizer.jsonio import json_int, json_object, json_string

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


class EsiClient:
    """ESI client intentionally optimized for correctness and low request volume, not bursts."""

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        cache: ResponseCache | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        compatibility_date: str = ESI_COMPATIBILITY_DATE,
        timeout_seconds: float = 30.0,
        max_retries: int = 4,
        operation_timeout_seconds: float = 300.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("ESI timeout must be finite and positive")
        if max_retries < 0:
            raise ValueError("ESI retry count cannot be negative")
        RequestBudget.start(operation_timeout_seconds, monotonic)
        self.operation_timeout_seconds = operation_timeout_seconds
        self.monotonic = monotonic
        self.transport = transport or UrllibTransport()
        self.cache = cache
        self.user_agent = user_agent
        self.compatibility_date = compatibility_date
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.sleep = sleep
        self.now = now

    def _get_json(
        self, path: str, query: Mapping[str, int | str], *, budget: RequestBudget | None = None
    ) -> tuple[object, Mapping[str, str]]:
        budget = budget or RequestBudget.start(self.operation_timeout_seconds, self.monotonic)
        self._wait(budget, 0)
        encoded_query = urlencode(query)
        url = f"{ESI_BASE_URL}{path}"
        if encoded_query:
            url = f"{url}?{encoded_query}"
        cache_key = f"{self.compatibility_date}:{url}"
        cached = self.cache.get(cache_key) if self.cache is not None else None
        now_epoch = self.now()
        if cached is not None and cached.expires_epoch > now_epoch:
            return self._parse_json(cached.body), dict(cached.headers)

        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            "X-Compatibility-Date": self.compatibility_date,
        }
        if cached is not None and cached.etag:
            headers["If-None-Match"] = cached.etag

        for attempt in range(self.max_retries + 1):
            try:
                response = self.transport.get(
                    url, headers, min(self.timeout_seconds, budget.remaining())
                )
                budget.remaining()
            except ResponseLimitError as error:
                raise EsiError(str(error)) from error
            except (OSError, IncompleteRead) as error:
                if attempt < self.max_retries:
                    self._wait(budget, float(min(2**attempt, 8)))
                    continue
                raise EsiError(f"ESI network request failed for {url}") from error
            if response.status == 304 and cached is not None:
                merged_headers = dict(cached.headers)
                merged_headers.update(response.headers)
                refreshed = CacheEntry(
                    key=cache_key,
                    etag=cached.etag,
                    expires_epoch=expiry_epoch(merged_headers, now_epoch=self.now()),
                    body=cached.body,
                    headers=merged_headers,
                )
                if self.cache is not None:
                    self.cache.put(refreshed)
                return self._parse_json(cached.body), merged_headers
            if 200 <= response.status < 300:
                payload = self._parse_json(response.body)
                if self.cache is not None:
                    self.cache.put(
                        CacheEntry(
                            key=cache_key,
                            etag=response.headers.get("etag"),
                            expires_epoch=expiry_epoch(response.headers, now_epoch=self.now()),
                            body=response.body,
                            headers=response.headers,
                        )
                    )
                return payload, response.headers
            if response.status == 429 and attempt < self.max_retries:
                retry_after = retry_delay(
                    response.headers.get("retry-after"), now_epoch=self.now(), default=1.0
                )
                self._wait(budget, retry_after)
                continue
            if response.status == 420 and attempt < self.max_retries:
                # Legacy ESI error-limit responses expose the reset delay under this header.
                reset_after = retry_delay(
                    response.headers.get("x-esi-error-limit-reset"),
                    now_epoch=self.now(),
                    default=60.0,
                )
                self._wait(budget, reset_after)
                continue
            if response.status >= 500 and attempt < self.max_retries:
                self._wait(budget, min(2**attempt, 8))
                continue
            raise EsiHttpError(response.status, url, response.body)
        raise AssertionError("retry loop must return or raise")

    def _wait(self, budget: RequestBudget, delay: float) -> None:
        try:
            budget.wait(delay, self.sleep)
        except TimeoutError as error:
            raise EsiError(str(error)) from error

    @staticmethod
    def _parse_json(body: bytes) -> object:
        if len(body) > MAX_RESPONSE_BYTES:
            raise EsiError("ESI response exceeds the 16 MiB limit")
        try:
            return json.loads(body, parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EsiError("ESI response was not valid JSON") from error

    def public_contract_page(
        self,
        region_id: int,
        page: int,
        *,
        budget: RequestBudget | None = None,
    ) -> tuple[list[dict[str, object]], int]:
        if region_id <= 0 or page <= 0:
            raise ValueError("region_id and page must be positive")
        payload, headers = self._get_json(
            f"/contracts/public/{region_id}/", {"page": page}, budget=budget
        )
        if not isinstance(payload, list):
            raise EsiError("public-contract response was not a list")
        try:
            rows = [json_object(row, "public contract") for row in cast(list[object], payload)]
            pages = int(headers.get("x-pages", "1"))
            if not 1 <= pages <= 100:
                raise ValueError("page count must be between 1 and 100")
        except ValueError as error:
            raise EsiError("invalid public-contract response") from error
        return rows, pages

    def public_couriers(
        self, region_id: int, *, budget: RequestBudget | None = None
    ) -> tuple[PublicCourierContract, ...]:
        budget = budget or RequestBudget.start(self.operation_timeout_seconds, self.monotonic)
        first_page, page_count = self.public_contract_page(region_id, 1, budget=budget)
        rows = list(first_page)
        if len(rows) > 100_000:
            raise EsiError("public-contract observation exceeds 100,000 rows")
        for page in range(2, page_count + 1):
            try:
                page_rows, _ = self.public_contract_page(region_id, page, budget=budget)
            except EsiHttpError as error:
                if error.status == 404:
                    # The live contract set can shrink after page 1. A now-nonexistent trailing
                    # page ends this bounded observation rather than turning normal churn into a
                    # failed scan.
                    break
                raise
            rows.extend(page_rows)
            if len(rows) > 100_000:
                raise EsiError("contract region exceeds the 100,000-row collection limit")
        try:
            contracts = [
                contract for row in rows if (contract := parse_public_courier(row)) is not None
            ]
        except (KeyError, TypeError, ValueError) as error:
            raise EsiError("public-contract response contained an invalid courier") from error
        # Page boundaries may move during a scan; contract IDs make de-duplication deterministic.
        unique = {contract.contract_id: contract for contract in contracts}
        return tuple(unique[key] for key in sorted(unique))

    def system_kills(
        self, *, budget: RequestBudget | None = None
    ) -> tuple[SystemKillActivity, ...]:
        """Fetch CCP's aggregate system-kill activity snapshot.

        ESI does not label suicide ganks. Consumers must treat ``ship_kills`` as a general danger
        proxy rather than attributing the underlying kills to a cause.
        """

        payload, _headers = self._get_json("/universe/system_kills/", {}, budget=budget)
        if not isinstance(payload, list):
            raise EsiError("system-kills response was not a list")
        by_system: dict[int, SystemKillActivity] = {}
        for raw in cast(list[object], payload):
            if not isinstance(raw, dict):
                continue
            row = json_object(cast(dict[object, object], raw), "system kills")
            try:
                item = SystemKillActivity(
                    system_id=json_int(row["system_id"], "system_id"),
                    ship_kills=json_int(row.get("ship_kills", 0), "ship_kills"),
                    pod_kills=json_int(row.get("pod_kills", 0), "pod_kills"),
                    npc_kills=json_int(row.get("npc_kills", 0), "npc_kills"),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise EsiError("system-kills response contained an invalid row") from error
            by_system[item.system_id] = item
        return tuple(by_system[key] for key in sorted(by_system))


def _quantity(value: object) -> Decimal | int | float | str:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise ValueError("courier quantity must be numeric")
    return value


def parse_public_courier(payload: Mapping[str, object]) -> PublicCourierContract | None:
    if payload.get("type") != "courier":
        return None
    date_issued_raw = payload.get("date_issued")
    return PublicCourierContract(
        contract_id=json_int(payload["contract_id"], "contract_id"),
        origin_location_id=json_int(payload["start_location_id"], "start_location_id"),
        destination_location_id=json_int(payload["end_location_id"], "end_location_id"),
        volume_units=cargo_volume_to_units(_quantity(payload["volume"])),
        collateral_units=isk_to_units(_quantity(payload.get("collateral", 0))),
        reward_units=isk_to_units(_quantity(payload.get("reward", 0))),
        date_expired=parse_esi_datetime(json_string(payload["date_expired"], "date_expired")),
        days_to_complete=json_int(payload["days_to_complete"], "days_to_complete"),
        title=json_string(payload.get("title", ""), "title"),
        date_issued=parse_esi_datetime(json_string(date_issued_raw, "date_issued"))
        if date_issued_raw
        else None,
    )


def default_cache_path() -> Path:
    return Path.home() / ".cache" / "eve-courier-route-optimizer" / "esi.sqlite3"


def utc_now() -> datetime:
    return datetime.now(UTC)
