"""A rolling trip whose refresh unblocks a previously excluded pickup system."""

from dataclasses import replace
from datetime import datetime

from eve_courier_optimizer.domain import GateEvidence, GateThreatEvent, ThreatCategory
from eve_courier_optimizer.eve.zkill import ZkillClient
from eve_courier_optimizer.web.workspace import PlanningWorkspace
from tests.support.scenarios import make_contract, make_snapshot
from tests.support.web import (
    CourierTransport,
    EmptyThreatTransport,
    planning_payload,
    proposal_input,
    seed_snapshot,
)


def prepare_rolling_policy_refresh(app: PlanningWorkspace, now: datetime) -> None:
    event = GateThreatEvent(
        killmail_id=7001,
        occurred_at=now,
        system_id=2,
        region_id=10,
        gate_id=500,
        distance_to_gate_m=0,
        evidence=GateEvidence.ZKILL_LOCATION,
        categories=frozenset({ThreatCategory.ANY_GATE_PVP}),
        victim_ship_type_id=3001,
        player_attacker_count=1,
    )
    snapshot = replace(
        make_snapshot(now, make_contract(now, 9001, 102, 101)),
        threat_intel_fetched_at=now,
        threat_window_seconds=7200,
        threat_gate_radius_m=250_000,
        threat_coverage_region_ids=(10,),
        gate_threat_events=(event,),
    )
    seed_snapshot(app, snapshot)
    app.solve(
        {
            **planning_payload(),
            "collateral_mode": "rolling",
            "gank_awareness": True,
            "threat_categories": ["any_gate_pvp"],
        }
    )
    app.start_execution(proposal_input(app))
    assert app.trip is not None and not app.trip.active_shipments
    app.esi.transport = CourierTransport(now, origin=102, destination=101)
    app.planner.zkill = ZkillClient(transport=EmptyThreatTransport(), request_spacing_seconds=0)
