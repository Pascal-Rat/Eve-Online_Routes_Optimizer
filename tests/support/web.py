from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from eve_courier_optimizer.domain import ContractSnapshot
from eve_courier_optimizer.eve.http import HttpResponse
from eve_courier_optimizer.web.operations import ScanObservation
from eve_courier_optimizer.web.workspace import PlanningWorkspace


class CourierTransport:
    """A one-page courier feed for positive region IDs; unexpected requests fail."""

    def __init__(self, now: datetime, *, origin: int = 101, destination: int = 102) -> None:
        self.now = now
        self.origin = origin
        self.destination = destination
        self.calls = 0

    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        del headers, timeout_seconds
        self.calls += 1
        request = urlsplit(url)
        if request.scheme != "https" or request.netloc != "esi.evetech.net":
            raise AssertionError(f"unexpected ESI host: {url}")
        if request.path == "/universe/system_kills/" and not request.query:
            activity = [
                {"system_id": 1, "ship_kills": 12, "pod_kills": 2, "npc_kills": 40},
                {"system_id": 2, "ship_kills": 3, "pod_kills": 0, "npc_kills": 12},
                {"system_id": 4, "ship_kills": 15, "pod_kills": 1, "npc_kills": 5},
            ]
            return HttpResponse(200, {}, json.dumps(activity).encode())
        if re.fullmatch(r"/contracts/public/[1-9]\d*/", request.path) is None or parse_qs(
            request.query
        ) != {"page": ["1"]}:
            raise AssertionError(f"unexpected ESI request: {url}")
        payload = [
            {
                "contract_id": 9001,
                "start_location_id": self.origin,
                "end_location_id": self.destination,
                "volume": 0.01,
                "collateral": 1.0,
                "reward": 5.0,
                "date_expired": (self.now + timedelta(days=1)).isoformat(),
                "date_issued": (self.now - timedelta(hours=1)).isoformat(),
                "days_to_complete": 1,
                "title": "Alpha to Beta test load",
                "type": "courier",
            }
        ]
        return HttpResponse(200, {"x-pages": "1"}, json.dumps(payload).encode())


def planning_payload() -> dict[str, object]:
    return {
        "start": "Alpha",
        "cargo_m3": "1",
        "collateral_isk": "2",
        "hours": "0.1",
        "security": "highsec",
        "collateral_mode": "locked",
        "avoid_systems": [],
        "seconds_per_jump": "10",
        "service_seconds": "1",
        "time_limit": "10",
        "workers": "1",
        "max_candidates": None,
    }


def post_json(url: str, payload: object, *, origin: str | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=2) as response:  # noqa: S310 - loopback test server
        return cast(dict[str, Any], json.load(response))


class SlowTransport(CourierTransport):
    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        time.sleep(5)
        return super().get(url, headers, timeout_seconds)


class EmptyThreatTransport:
    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        return HttpResponse(200, {}, b"[]")


def proposal_input(
    app: PlanningWorkspace, body: dict[str, object] | None = None
) -> dict[str, object]:
    """Capture the proposal a simulated tab has just reviewed."""
    return {"expected_revision": app.revision, "proposal_id": app.proposal_id, **(body or {})}


def seed_snapshot(app: PlanningWorkspace, snapshot: ContractSnapshot) -> None:
    app.publish(ScanObservation(snapshot), expected_revision=app.revision)
