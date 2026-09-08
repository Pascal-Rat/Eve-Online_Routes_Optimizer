from __future__ import annotations

import gzip
import io
import socket
from collections.abc import Mapping
from datetime import datetime
from http.client import HTTPResponse, IncompleteRead
from typing import cast

import pytest

from eve_courier_optimizer.eve.contract_scan import scan_public_couriers
from eve_courier_optimizer.eve.esi import EsiClient, EsiError
from eve_courier_optimizer.eve.http import (
    MAX_RESPONSE_BYTES,
    HttpResponse,
    RequestBudget,
    ResponseLimitError,
    read_response_body,
)
from eve_courier_optimizer.eve.zkill import ZkillClient, ZkillError, collect_gate_threat_intel
from eve_courier_optimizer.routing.universe import UniverseGraph


class _Socket:
    def makefile(self, mode: str) -> io.BytesIO:
        assert mode == "rb"
        return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n[]")


class _InterruptedTransport:
    def __init__(self, *, advisory_only: bool = False) -> None:
        self.calls = 0
        self.advisory_only = advisory_only

    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        self.calls += 1
        if self.advisory_only and "/contracts/" in url:
            return HttpResponse(200, {}, b"[]")
        raise IncompleteRead(b"[", 12)


class _ResponseTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls = 0

    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        self.calls += 1
        return self.response


def test_real_http_response_with_truncated_content_length_is_a_network_error() -> None:
    # HTTPResponse only needs makefile from the socket; no loopback privileges are required.
    response = HTTPResponse(cast(socket.socket, _Socket()))
    response.begin()
    with response, pytest.raises(OSError, match="Content-Length"):
        read_response_body(response, {"content-length": "10"}, RequestBudget.start(1))


@pytest.mark.parametrize(
    "client_type, error_type", [(EsiClient, EsiError), (ZkillClient, ZkillError)]
)
def test_incomplete_response_obeys_retry_budget(
    client_type: type[EsiClient] | type[ZkillClient],
    error_type: type[RuntimeError],
) -> None:
    transport = _InterruptedTransport()
    client = client_type(transport=transport, max_retries=1, sleep=lambda _: None)
    with pytest.raises(error_type) as error:
        if isinstance(client, EsiClient):
            client.public_couriers(10)
        else:
            client.region_losses(10)
    assert isinstance(error.value.__cause__, IncompleteRead)
    assert transport.calls == 2


def test_interrupted_advisory_feed_preserves_contract_observation(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    transport = _InterruptedTransport(advisory_only=True)
    snapshot = scan_public_couriers(
        EsiClient(transport=transport, max_retries=1, sleep=lambda _: None),
        tiny_graph,
        (10,),
        clock=lambda: now,
        include_system_kills=True,
    )
    assert snapshot.contracts == ()
    assert snapshot.system_kills_fetched_at is None
    assert transport.calls == 3


def test_truncated_gzip_marks_threat_coverage_incomplete(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    transport = _ResponseTransport(
        HttpResponse(200, {"content-encoding": "gzip"}, gzip.compress(b"[]")[:-4])
    )
    result = collect_gate_threat_intel(
        ZkillClient(transport=transport, max_retries=0), tiny_graph, (10,), clock=lambda: now
    )
    assert result.incomplete_region_ids == (10,)
    assert result.coverage_region_ids == ()


@pytest.mark.parametrize(
    "client_type, error_type", [(EsiClient, EsiError), (ZkillClient, ZkillError)]
)
def test_retry_after_cannot_extend_collection_into_the_next_day(
    client_type: type[EsiClient] | type[ZkillClient], error_type: type[RuntimeError]
) -> None:
    transport = _ResponseTransport(HttpResponse(429, {"retry-after": "86400"}, b"busy"))
    sleeps: list[float] = []
    client = client_type(transport=transport, max_retries=1, sleep=sleeps.append)
    with pytest.raises(error_type, match="try again later"):
        if isinstance(client, EsiClient):
            client.public_couriers(10)
        else:
            client.region_losses(10)
    assert sleeps == []
    assert transport.calls == 1


def test_body_and_gzip_expansion_have_size_bounds() -> None:
    with pytest.raises(ResponseLimitError):
        read_response_body(
            io.BytesIO(), {"content-length": str(MAX_RESPONSE_BYTES + 1)}, RequestBudget.start(1)
        )
    with pytest.raises(ZkillError, match="16 MiB"):
        client = ZkillClient(
            transport=_ResponseTransport(
                HttpResponse(
                    200,
                    {"content-encoding": "gzip"},
                    gzip.compress(b" " * (MAX_RESPONSE_BYTES + 1)),
                )
            )
        )
        client.region_losses(10)


def test_monotonic_budget_is_shared_across_waits_and_requests() -> None:
    clock = [0.0]
    budget = RequestBudget.start(3, lambda: clock[0])
    budget.wait(2, lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert budget.remaining() == 1
    with pytest.raises(TimeoutError, match="try again later"):
        budget.wait(2, lambda _: None)
    clock[0] = 4
    with pytest.raises(TimeoutError, match="overall"):
        budget.remaining()


@pytest.mark.parametrize(
    "client_type, error_type", [(EsiClient, EsiError), (ZkillClient, ZkillError)]
)
def test_cached_data_cannot_bypass_an_expired_collection_budget(
    client_type: type[EsiClient] | type[ZkillClient], error_type: type[RuntimeError]
) -> None:
    transport = _ResponseTransport(HttpResponse(200, {}, b"[]"))
    client = client_type(transport=transport)
    expired = RequestBudget(0, lambda: 1)
    with pytest.raises(error_type, match="overall"):
        if isinstance(client, EsiClient):
            client.public_couriers(10, budget=expired)
        else:
            client.region_losses(10, budget=expired)
    assert transport.calls == 0


def test_malformed_zkill_page_is_not_silently_shortened() -> None:
    client = ZkillClient(transport=_ResponseTransport(HttpResponse(200, {}, b"[null]")))
    with pytest.raises(ZkillError, match="malformed row"):
        client.region_losses(10)
