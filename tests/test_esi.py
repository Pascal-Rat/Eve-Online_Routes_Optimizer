from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from eve_courier_optimizer.esi import (
    EsiClient,
    EsiError,
    EsiHttpError,
    EsiResponseCache,
    HttpResponse,
)


class QueueTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        del timeout_seconds
        self.calls.append((url, headers))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


def response(
    payload: object,
    *,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={key.lower(): value for key, value in (headers or {}).items()},
        body=json.dumps(payload).encode(),
    )


def courier(contract_id: int) -> dict[str, object]:
    return {
        "contract_id": contract_id,
        "start_location_id": 101,
        "end_location_id": 102,
        "volume": 12.345,
        "collateral": 1_000_000.0,
        "reward": 50_000.0,
        "date_expired": "2026-08-06T12:00:00Z",
        "date_issued": "2026-08-05T12:00:00Z",
        "days_to_complete": 1,
        "type": "courier",
    }


def test_public_courier_pagination_and_filtering() -> None:
    transport = QueueTransport(
        [
            response(
                [{"type": "item_exchange"}, courier(1)],
                headers={"X-Pages": "2"},
            ),
            response([courier(2)], headers={"X-Pages": "2"}),
        ]
    )
    client = EsiClient(transport=transport, max_retries=0)
    contracts = client.public_couriers(10)
    assert [item.contract_id for item in contracts] == [1, 2]
    assert contracts[0].volume_units == 12_345
    assert contracts[0].reward_units == 5_000_000
    assert len(transport.calls) == 2


def test_cache_avoids_repeated_request(tmp_path: Path) -> None:
    transport = QueueTransport(
        [
            response(
                [courier(1)],
                headers={"X-Pages": "1", "Expires": "Fri, 01 Jan 2100 00:00:00 GMT"},
            )
        ]
    )
    client = EsiClient(
        transport=transport,
        cache=EsiResponseCache(tmp_path / "cache.sqlite3"),
        now=lambda: 1.0,
    )
    assert len(client.public_couriers(10)) == 1
    assert len(client.public_couriers(10)) == 1
    assert len(transport.calls) == 1


def test_429_respects_retry_after() -> None:
    sleeps: list[float] = []
    transport = QueueTransport(
        [
            response({}, status=429, headers={"Retry-After": "2"}),
            response([courier(1)], headers={"X-Pages": "1"}),
        ]
    )
    client = EsiClient(transport=transport, sleep=sleeps.append, max_retries=1)
    assert len(client.public_couriers(10)) == 1
    assert sleeps == [2.0]


def test_legacy_420_respects_error_limit_reset() -> None:
    sleeps: list[float] = []
    transport = QueueTransport(
        [
            response({}, status=420, headers={"X-Esi-Error-Limit-Reset": "3"}),
            response([courier(1)], headers={"X-Pages": "1"}),
        ]
    )
    client = EsiClient(transport=transport, sleep=sleeps.append, max_retries=1)
    assert len(client.public_couriers(10)) == 1
    assert sleeps == [3.0]


def test_conditional_304_preserves_cached_pagination_headers(tmp_path: Path) -> None:
    first_transport = QueueTransport(
        [
            response(
                [courier(1)],
                headers={
                    "X-Pages": "2",
                    "ETag": '"one"',
                    "Expires": "Thu, 01 Jan 1970 00:00:01 GMT",
                },
            ),
            response(
                [courier(2)],
                headers={
                    "X-Pages": "2",
                    "ETag": '"two"',
                    "Expires": "Thu, 01 Jan 1970 00:00:01 GMT",
                },
            ),
        ]
    )
    cache = EsiResponseCache(tmp_path / "cache.sqlite3")
    EsiClient(transport=first_transport, cache=cache, now=lambda: 0.0).public_couriers(10)

    second_transport = QueueTransport(
        [
            HttpResponse(304, {"expires": "Fri, 01 Jan 2100 00:00:00 GMT"}, b""),
            HttpResponse(304, {"expires": "Fri, 01 Jan 2100 00:00:00 GMT"}, b""),
        ]
    )
    contracts = EsiClient(
        transport=second_transport,
        cache=cache,
        now=lambda: 2.0,
    ).public_couriers(10)
    assert [item.contract_id for item in contracts] == [1, 2]
    assert second_transport.calls[0][1]["If-None-Match"] == '"one"'


def test_disappearing_trailing_page_ends_bounded_scan() -> None:
    transport = QueueTransport(
        [
            response([courier(1)], headers={"X-Pages": "2"}),
            response({"error": "Requested page does not exist!"}, status=404),
        ]
    )
    assert [item.contract_id for item in EsiClient(transport=transport).public_couriers(10)] == [1]


def test_non_retryable_http_error_is_raised() -> None:
    transport = QueueTransport([response({"error": "forbidden"}, status=403)])
    client = EsiClient(transport=transport, max_retries=0)
    with pytest.raises(EsiHttpError, match="HTTP 403"):
        client.public_couriers(10)


def test_invalid_page_arguments_are_rejected() -> None:
    client = EsiClient(transport=QueueTransport([]))
    with pytest.raises(ValueError):
        client.public_contract_page(0, 1)
    with pytest.raises(ValueError):
        client.public_contract_page(1, 0)


def test_system_kills_are_parsed_deduplicated_and_sorted() -> None:
    transport = QueueTransport(
        [
            response(
                [
                    {"system_id": 20, "ship_kills": 4, "pod_kills": 1, "npc_kills": 9},
                    "ignored",
                    {"system_id": 10, "ship_kills": 2},
                    {"system_id": 20, "ship_kills": 5, "pod_kills": 2, "npc_kills": 10},
                ]
            )
        ]
    )
    activity = EsiClient(transport=transport).system_kills()
    assert [item.system_id for item in activity] == [10, 20]
    assert activity[0].pod_kills == 0
    assert activity[1].ship_kills == 5
    assert transport.calls[0][0].endswith("/universe/system_kills/")


@pytest.mark.parametrize(
    "payload",
    [
        {"system_id": 1},
        [{"ship_kills": 1}],
        [{"system_id": 1, "ship_kills": -1}],
    ],
)
def test_system_kills_reject_malformed_payloads(payload: object) -> None:
    client = EsiClient(transport=QueueTransport([response(payload)]))
    with pytest.raises(EsiError, match="system-kills"):
        client.system_kills()
