"""Persistent accepted commitments, real progress, and replanning constraints."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from eve_courier_optimizer.domain import (
    ActiveShipment,
    CollateralMode,
    ContractSnapshot,
    PlanningConstraints,
    RoutableContract,
    SecurityPolicy,
    SolveResult,
    TravelTimeModel,
)
from eve_courier_optimizer.routing.security import (
    observed_security_policy,
)
from eve_courier_optimizer.routing.universe import UniverseGraph


@dataclass(frozen=True, slots=True)
class CourierTrip:
    """Everything needed to replan after a real pickup/delivery or market refresh."""

    current_time: datetime
    session_deadline: datetime
    current_system_id: int
    cargo_capacity_units: int
    collateral_budget_units: int
    collateral_mode: CollateralMode
    travel: TravelTimeModel
    security: SecurityPolicy
    terminal_system_id: int | None = None
    remaining_required_system_ids: frozenset[int] = frozenset()
    max_simultaneous_contracts: int | None = None
    active_shipments: tuple[ActiveShipment, ...] = ()
    completed_contract_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.current_time.tzinfo is None or self.session_deadline.tzinfo is None:
            raise ValueError("execution timestamps must be timezone-aware")
        if self.current_system_id <= 0:
            raise ValueError("execution system ID must be positive")
        if self.cargo_capacity_units < 0 or self.collateral_budget_units < 0:
            raise ValueError("execution resource limits cannot be negative")
        if self.terminal_system_id is not None and self.terminal_system_id <= 0:
            raise ValueError("execution terminal system ID must be positive")
        if any(system_id <= 0 for system_id in self.remaining_required_system_ids):
            raise ValueError("remaining required system IDs must be positive")
        if self.max_simultaneous_contracts is not None and self.max_simultaneous_contracts < 0:
            raise ValueError("max simultaneous contracts cannot be negative")
        if any(contract_id <= 0 for contract_id in self.completed_contract_ids):
            raise ValueError("completed contract IDs must be positive")
        if len(self.completed_contract_ids) != len(set(self.completed_contract_ids)):
            raise ValueError("completed contract IDs must be unique")
        active_ids = {shipment.contract.contract_id for shipment in self.active_shipments}
        if len(active_ids) != len(self.active_shipments):
            raise ValueError("active shipment contract IDs must be unique")
        if sum(s.contract.volume_units for s in self.active_shipments if s.picked) > (
            self.cargo_capacity_units
        ):
            raise ValueError("execution cargo exceeds cargo capacity")
        if sum(s.contract.collateral_units for s in self.active_shipments) > (
            self.collateral_budget_units
        ):
            raise ValueError("execution collateral exceeds budget")
        if active_ids & set(self.completed_contract_ids):
            raise ValueError("a contract cannot be both active and completed")
        picked_count = sum(1 for shipment in self.active_shipments if shipment.picked)
        if (
            self.max_simultaneous_contracts is not None
            and picked_count > self.max_simultaneous_contracts
        ):
            raise ValueError("execution state exceeds the simultaneous-contract limit")

    @classmethod
    def from_plan(
        cls,
        constraints: PlanningConstraints,
        contracts: tuple[RoutableContract, ...],
        active_shipments: tuple[ActiveShipment, ...],
        result: SolveResult,
        *,
        completed_contract_ids: tuple[int, ...] = (),
    ) -> CourierTrip:
        """Create the trip immediately after the plan's required initial acceptances.

        In locked mode the mathematical model assumes all selected public contracts are accepted at
        time zero, so they become mandatory commitments awaiting pickup. In rolling mode an optional
        contract is accepted when the pilot reaches its pickup and records it.
        """

        active_by_id = {shipment.contract.contract_id: shipment for shipment in active_shipments}
        if constraints.collateral_mode is CollateralMode.LOCKED:
            optional = {item.contract_id: item for item in contracts}
            for contract_id in result.selected_contract_ids:
                item = optional[contract_id]
                active_by_id[contract_id] = ActiveShipment(
                    contract=item,
                    deadline=constraints.snapshot_time + timedelta(days=item.days_to_complete),
                    picked=False,
                )
        return cls(
            current_time=constraints.snapshot_time,
            session_deadline=constraints.snapshot_time
            + timedelta(seconds=constraints.horizon_seconds),
            current_system_id=constraints.start_system_id,
            cargo_capacity_units=constraints.cargo_capacity_units,
            collateral_budget_units=constraints.collateral_budget_units,
            collateral_mode=constraints.collateral_mode,
            travel=constraints.travel,
            security=constraints.security,
            terminal_system_id=constraints.terminal_system_id,
            remaining_required_system_ids=(
                constraints.required_system_ids - {constraints.start_system_id}
            ),
            max_simultaneous_contracts=constraints.max_simultaneous_contracts,
            active_shipments=tuple(active_by_id[key] for key in sorted(active_by_id)),
            completed_contract_ids=completed_contract_ids,
        )

    def replanning_constraints(
        self,
        snapshot: ContractSnapshot,
        *,
        at: datetime | None = None,
    ) -> PlanningConstraints:
        if at is not None and at.tzinfo is None:
            raise ValueError("replanning time must be timezone-aware")
        effective_time = max(self.current_time, snapshot.fetched_at, at or self.current_time)
        if effective_time > self.session_deadline:
            raise ValueError("planning horizon has ended; extend the horizon before replanning")
        remaining = int((self.session_deadline - effective_time).total_seconds())
        security = self.security
        exemptions = {self.current_system_id}
        exemptions.update(self.remaining_required_system_ids)
        if self.terminal_system_id is not None:
            exemptions.add(self.terminal_system_id)
        for shipment in self.active_shipments:
            if not shipment.picked:
                exemptions.add(shipment.contract.origin_system_id)
            exemptions.add(shipment.contract.destination_system_id)
        # Refreshed observations may not strand already accepted obligations.
        security = observed_security_policy(
            snapshot,
            minimum_security=security.minimum_security,
            allowed_bands=security.allowed_bands,
            avoided_system_ids=security.avoided_system_ids,
            activity_threshold=security.gank_ship_kill_threshold,
            threat_categories=security.threat_categories,
            threat_min_events=security.threat_min_events,
            exempt_system_ids=frozenset(exemptions),
        )
        return PlanningConstraints(
            start_system_id=self.current_system_id,
            cargo_capacity_units=self.cargo_capacity_units,
            collateral_budget_units=self.collateral_budget_units,
            horizon_seconds=remaining,
            snapshot_time=effective_time,
            collateral_mode=self.collateral_mode,
            travel=self.travel,
            security=security,
            return_to_start=False,
            required_system_ids=self.remaining_required_system_ids,
            finish_system_id=self.terminal_system_id,
            max_simultaneous_contracts=self.max_simultaneous_contracts,
        )

    def pick_up(
        self,
        snapshot: ContractSnapshot | None,
        graph: UniverseGraph,
        contract_id: int,
        at: datetime,
    ) -> CourierTrip:
        """Record a successful in-game pickup/accept-and-pickup event."""

        if at.tzinfo is None or at < self.current_time:
            raise ValueError(
                "pickup time must be timezone-aware and cannot precede recorded progress"
            )
        active_by_id = {
            shipment.contract.contract_id: shipment for shipment in self.active_shipments
        }
        existing = active_by_id.get(contract_id)
        if existing is not None:
            if existing.picked:
                raise ValueError(f"contract {contract_id} is already picked up")
            if at > existing.deadline:
                raise ValueError(f"contract {contract_id} is past its delivery deadline")
            cargo_now = sum(
                shipment.contract.volume_units
                for shipment in self.active_shipments
                if shipment.picked
            )
            if cargo_now + existing.contract.volume_units > self.cargo_capacity_units:
                raise ValueError("pickup would exceed cargo capacity")
            if (
                self.max_simultaneous_contracts is not None
                and sum(1 for shipment in self.active_shipments if shipment.picked) + 1
                > self.max_simultaneous_contracts
            ):
                raise ValueError("pickup would exceed the simultaneous-contract limit")
            active_by_id[contract_id] = replace(existing, picked=True)
            return replace(
                self,
                current_time=at,
                current_system_id=existing.contract.origin_system_id,
                remaining_required_system_ids=(
                    self.remaining_required_system_ids - {existing.contract.origin_system_id}
                ),
                active_shipments=tuple(active_by_id[key] for key in sorted(active_by_id)),
            )

        if self.collateral_mode is CollateralMode.LOCKED:
            raise ValueError("locked-mode pickup must already exist as an accepted commitment")
        if snapshot is None:
            raise ValueError("a snapshot is required to accept a new rolling contract")
        public = next(
            (item for item in snapshot.contracts if item.contract_id == contract_id), None
        )
        if public is None:
            raise ValueError(f"contract {contract_id} is not present in the supplied snapshot")
        if at >= public.date_expired:
            raise ValueError(f"contract {contract_id} listing has expired")
        origin = graph.station_system(public.origin_location_id)
        destination = graph.station_system(public.destination_location_id)
        if origin is None or destination is None:
            raise ValueError("rolling pickup has an unsupported non-NPC-station endpoint")
        if not graph.system_allowed(origin, self.security) or not graph.system_allowed(
            destination, self.security
        ):
            raise ValueError("rolling pickup violates the execution security policy")
        locked_now = sum(shipment.contract.collateral_units for shipment in self.active_shipments)
        cargo_now = sum(
            shipment.contract.volume_units for shipment in self.active_shipments if shipment.picked
        )
        if locked_now + public.collateral_units > self.collateral_budget_units:
            raise ValueError("pickup would exceed collateral budget")
        if cargo_now + public.volume_units > self.cargo_capacity_units:
            raise ValueError("pickup would exceed cargo capacity")
        if (
            self.max_simultaneous_contracts is not None
            and sum(1 for shipment in self.active_shipments if shipment.picked) + 1
            > self.max_simultaneous_contracts
        ):
            raise ValueError("pickup would exceed the simultaneous-contract limit")
        routable = RoutableContract.resolve(public, origin, destination)
        active_by_id[contract_id] = ActiveShipment(
            contract=routable,
            deadline=at + timedelta(days=public.days_to_complete),
            picked=True,
        )
        return replace(
            self,
            current_time=at,
            current_system_id=origin,
            remaining_required_system_ids=self.remaining_required_system_ids - {origin},
            active_shipments=tuple(active_by_id[key] for key in sorted(active_by_id)),
        )

    def deliver(self, contract_id: int, at: datetime) -> CourierTrip:
        if at.tzinfo is None or at < self.current_time:
            raise ValueError(
                "delivery time must be timezone-aware and cannot precede recorded progress"
            )
        active_by_id = {
            shipment.contract.contract_id: shipment for shipment in self.active_shipments
        }
        shipment = active_by_id.get(contract_id)
        if shipment is None:
            raise ValueError(f"contract {contract_id} is not an active commitment")
        if not shipment.picked:
            raise ValueError(f"contract {contract_id} has not been picked up")
        if at > shipment.deadline:
            raise ValueError(f"contract {contract_id} was delivered after its modeled deadline")
        del active_by_id[contract_id]
        completed = tuple(sorted((*self.completed_contract_ids, contract_id)))
        return replace(
            self,
            current_time=at,
            current_system_id=shipment.contract.destination_system_id,
            remaining_required_system_ids=(
                self.remaining_required_system_ids - {shipment.contract.destination_system_id}
            ),
            active_shipments=tuple(active_by_id[key] for key in sorted(active_by_id)),
            completed_contract_ids=completed,
        )

    def reach_system(self, system_id: int, at: datetime) -> CourierTrip:
        """Record that the pilot actually reached a required waypoint or final system."""

        allowed_markers = set(self.remaining_required_system_ids)
        if self.terminal_system_id is not None:
            allowed_markers.add(self.terminal_system_id)
        if system_id not in allowed_markers:
            raise ValueError("system is not a pending required waypoint or finish")
        if at.tzinfo is None or at < self.current_time:
            raise ValueError("route-system time must be timezone-aware and cannot precede progress")
        return replace(
            self,
            current_time=at,
            current_system_id=system_id,
            remaining_required_system_ids=self.remaining_required_system_ids - {system_id},
        )

    def extend_horizon(
        self,
        *,
        additional_seconds: int,
        at: datetime,
    ) -> CourierTrip:
        """Extend the planning budget without changing any accepted contract deadline."""

        if additional_seconds <= 0:
            raise ValueError("horizon extension must be positive")
        if at.tzinfo is None or at < self.current_time:
            raise ValueError("extension time must be timezone-aware and cannot precede progress")
        return replace(
            self,
            current_time=at,
            session_deadline=max(at, self.session_deadline) + timedelta(seconds=additional_seconds),
        )
