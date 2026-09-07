"""Exact pickup/delivery circuit, event resources, and assignment extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ortools.sat.python import cp_model

from .bounds import add_resource_work_bounds
from .construction import build_greedy_route_hint
from .domain import ActionKind, CollateralMode, PlannedAction, PlannedVisit, PlannedWaypoint
from .planning import PreparedProblem

_START: Final = "start"
_END: Final = "end"
_START_NODE_ID: Final = 0
_END_NODE_ID: Final = 1


@dataclass(frozen=True, slots=True)
class RouteEvent:
    """One location or contract action that may appear in the optimized route."""

    node_id: int
    label: str
    action_kind: ActionKind | None
    contract_id: int | None
    system_id: int | None
    location_id: int | None
    cargo_delta: int = 0
    collateral_delta: int = 0
    parcel_delta: int = 0
    is_optional: bool = False


@dataclass(frozen=True, slots=True)
class EventCatalog:
    """Route events plus the node IDs needed to add contract constraints."""

    events: tuple[RouteEvent, ...]
    optional_pickups: dict[int, int]
    optional_deliveries: dict[int, int]
    committed_pickups: dict[int, int]
    committed_deliveries: dict[int, int]
    waypoints: tuple[int, ...]


def _build_route_event_catalog(prepared: PreparedProblem) -> EventCatalog:
    """Translate business objects into the events that can appear in the route.

    Optional public contracts contribute a pickup and a delivery that CP-SAT may skip together.
    Already accepted shipments and required waypoint systems contribute mandatory events.
    """

    problem = prepared.problem
    constraints = problem.constraints
    optional_contract_ids = {contract.contract_id for contract in problem.contracts}
    active_contract_ids = {shipment.contract.contract_id for shipment in problem.active_shipments}
    duplicate_contract_ids = optional_contract_ids & active_contract_ids
    if duplicate_contract_ids:
        raise ValueError(
            f"contracts cannot be both optional and active: {sorted(duplicate_contract_ids)}"
        )

    events: list[RouteEvent] = [
        RouteEvent(
            node_id=_START_NODE_ID,
            label=_START,
            action_kind=None,
            contract_id=None,
            system_id=constraints.start_system_id,
            location_id=None,
        ),
        RouteEvent(
            node_id=_END_NODE_ID,
            label=_END,
            action_kind=None,
            contract_id=None,
            system_id=constraints.terminal_system_id,
            location_id=None,
        ),
    ]

    def add_event(
        *,
        label: str,
        action_kind: ActionKind | None,
        contract_id: int | None,
        system_id: int,
        location_id: int | None,
        cargo_delta: int = 0,
        collateral_delta: int = 0,
        parcel_delta: int = 0,
        is_optional: bool = False,
    ) -> int:
        node_id = len(events)
        events.append(
            RouteEvent(
                node_id=node_id,
                label=label,
                action_kind=action_kind,
                contract_id=contract_id,
                system_id=system_id,
                location_id=location_id,
                cargo_delta=cargo_delta,
                collateral_delta=collateral_delta,
                parcel_delta=parcel_delta,
                is_optional=is_optional,
            )
        )
        return node_id

    optional_pickups: dict[int, int] = {}
    optional_deliveries: dict[int, int] = {}
    for contract in problem.contracts:
        contract_id = contract.contract_id
        optional_pickups[contract_id] = add_event(
            label=f"pickup:{contract_id}",
            action_kind=ActionKind.PICKUP,
            contract_id=contract_id,
            system_id=contract.origin_system_id,
            location_id=contract.origin_location_id,
            cargo_delta=contract.volume_units,
            collateral_delta=contract.collateral_units,
            parcel_delta=1,
            is_optional=True,
        )
        optional_deliveries[contract_id] = add_event(
            label=f"delivery:{contract_id}",
            action_kind=ActionKind.DELIVERY,
            contract_id=contract_id,
            system_id=contract.destination_system_id,
            location_id=contract.destination_location_id,
            cargo_delta=-contract.volume_units,
            collateral_delta=-contract.collateral_units,
            parcel_delta=-1,
            is_optional=True,
        )

    committed_pickups: dict[int, int] = {}
    committed_deliveries: dict[int, int] = {}
    for shipment in problem.active_shipments:
        contract = shipment.contract
        contract_id = contract.contract_id
        if not shipment.picked:
            committed_pickups[contract_id] = add_event(
                label=f"committed-pickup:{contract_id}",
                action_kind=ActionKind.PICKUP,
                contract_id=contract_id,
                system_id=contract.origin_system_id,
                location_id=contract.origin_location_id,
                cargo_delta=contract.volume_units,
                # The contract was accepted earlier, so its collateral is already locked.
                collateral_delta=0,
                parcel_delta=1,
            )
        committed_deliveries[contract_id] = add_event(
            label=f"active-delivery:{contract_id}",
            action_kind=ActionKind.DELIVERY,
            contract_id=contract_id,
            system_id=contract.destination_system_id,
            location_id=contract.destination_location_id,
            cargo_delta=-contract.volume_units,
            collateral_delta=-contract.collateral_units,
            parcel_delta=-1,
        )

    waypoints: list[int] = []
    waypoint_already_guaranteed = {constraints.start_system_id}
    if constraints.terminal_system_id is not None:
        waypoint_already_guaranteed.add(constraints.terminal_system_id)
    for system_id in sorted(constraints.required_system_ids - waypoint_already_guaranteed):
        waypoints.append(
            add_event(
                label=f"waypoint:{system_id}",
                action_kind=None,
                contract_id=None,
                system_id=system_id,
                location_id=None,
            )
        )

    return EventCatalog(
        events=tuple(events),
        optional_pickups=optional_pickups,
        optional_deliveries=optional_deliveries,
        committed_pickups=committed_pickups,
        committed_deliveries=committed_deliveries,
        waypoints=tuple(waypoints),
    )


class EventModel:
    """One optional-event circuit with explicit time, cargo, collateral, and parcel state."""

    start_node_id = _START_NODE_ID
    end_node_id = _END_NODE_ID

    def __init__(self, prepared: PreparedProblem) -> None:
        self.prepared = prepared
        self.constraints = prepared.problem.constraints
        self.catalog = _build_route_event_catalog(prepared)
        self.events = self.catalog.events
        self.optional_contracts = {c.contract_id: c for c in prepared.problem.contracts}
        self.commitments = {s.contract.contract_id: s for s in prepared.problem.active_shipments}
        self.model = cp_model.CpModel()
        self.pickup_by_delivery = {
            self.catalog.optional_deliveries[contract_id]: (
                self.catalog.optional_pickups[contract_id]
            )
            for contract_id in self.catalog.optional_pickups
        }
        self.pickup_by_delivery.update(
            {
                self.catalog.committed_deliveries[contract_id]: (
                    self.catalog.committed_pickups[contract_id]
                )
                for contract_id in self.catalog.committed_pickups
            }
        )
        self.delivery_by_pickup = {
            pickup_node_id: delivery_node_id
            for delivery_node_id, pickup_node_id in self.pickup_by_delivery.items()
        }

        self.earliest_arrivals = tuple(self._earliest_arrival(event) for event in self.events)
        self.latest_arrivals = tuple(self._latest_arrival(event) for event in self.events)

        self._add_circuit()
        self._create_event_state()
        self._propagate_route_resources()
        self._add_contract_constraints()
        self._maximize_reward()

    @property
    def finish_time_seconds(self) -> cp_model.IntVar:
        return self.arrival[self.end_node_id]

    def _travel_seconds(
        self,
        source_system_id: int,
        destination_system_id: int,
    ) -> int | None:
        jump_count = self.prepared.jump_matrix.get((source_system_id, destination_system_id))
        return None if jump_count is None else jump_count * self.constraints.travel.seconds_per_jump

    def _event_travel_seconds(
        self,
        from_event: RouteEvent,
        to_event: RouteEvent,
    ) -> int | None:
        if to_event.node_id == _END_NODE_ID and to_event.system_id is None:
            return 0
        if from_event.system_id is None or to_event.system_id is None:
            return None
        jump_count = self.prepared.jump_matrix.get((from_event.system_id, to_event.system_id))
        if jump_count is None:
            return None
        return jump_count * self.constraints.travel.seconds_per_jump

    def _earliest_arrival(self, event: RouteEvent) -> int:
        if event.node_id == _START_NODE_ID or event.system_id is None:
            return 0
        direct_travel_time = self._travel_seconds(
            self.constraints.start_system_id,
            event.system_id,
        )
        if (
            event.action_kind is not ActionKind.DELIVERY
            or event.node_id not in self.pickup_by_delivery
        ):
            return (
                self.constraints.horizon_seconds + 1
                if direct_travel_time is None
                else direct_travel_time
            )

        # Delivery requires travel via pickup, including pickup service, even with interleaving.
        pickup_event = self.events[self.pickup_by_delivery[event.node_id]]
        assert pickup_event.system_id is not None
        travel_to_pickup_seconds = self._travel_seconds(
            self.constraints.start_system_id,
            pickup_event.system_id,
        )
        travel_to_delivery_seconds = self._travel_seconds(
            pickup_event.system_id,
            event.system_id,
        )
        if travel_to_pickup_seconds is None or travel_to_delivery_seconds is None:
            return self.constraints.horizon_seconds + 1
        return (
            travel_to_pickup_seconds
            + self.constraints.travel.service_seconds
            + travel_to_delivery_seconds
        )

    def _latest_arrival(self, event: RouteEvent) -> int:
        if event.action_kind is None:
            return self.constraints.horizon_seconds
        latest_arrival_seconds = (
            self.constraints.horizon_seconds - self.constraints.travel.service_seconds
        )
        assert event.contract_id is not None
        optional_contract = self.optional_contracts.get(event.contract_id)
        if optional_contract is not None:
            if event.action_kind is ActionKind.PICKUP:
                minimum_delivery_travel_seconds = self._travel_seconds(
                    optional_contract.origin_system_id,
                    optional_contract.destination_system_id,
                )
                if minimum_delivery_travel_seconds is not None:
                    latest_arrival_seconds = min(
                        latest_arrival_seconds,
                        self.constraints.horizon_seconds
                        - 2 * self.constraints.travel.service_seconds
                        - minimum_delivery_travel_seconds,
                    )
                    if self.constraints.collateral_mode is CollateralMode.LOCKED:
                        latest_arrival_seconds = min(
                            latest_arrival_seconds,
                            optional_contract.days_to_complete * 86_400
                            - 2 * self.constraints.travel.service_seconds
                            - minimum_delivery_travel_seconds,
                        )
                if self.constraints.collateral_mode is CollateralMode.ROLLING:
                    latest_arrival_seconds = min(
                        latest_arrival_seconds,
                        optional_contract.last_pickup_second(self.constraints.snapshot_time),
                    )
            if (
                event.action_kind is ActionKind.DELIVERY
                and self.constraints.collateral_mode is CollateralMode.LOCKED
            ):
                latest_arrival_seconds = min(
                    latest_arrival_seconds,
                    optional_contract.days_to_complete * 86_400
                    - self.constraints.travel.service_seconds,
                )
            return latest_arrival_seconds
        active_shipment = self.commitments[event.contract_id]
        deadline_seconds = active_shipment.last_delivery_second(self.constraints.snapshot_time)
        if event.action_kind is ActionKind.DELIVERY:
            return min(
                latest_arrival_seconds, deadline_seconds - self.constraints.travel.service_seconds
            )
        minimum_delivery_travel_seconds = self._travel_seconds(
            active_shipment.contract.origin_system_id,
            active_shipment.contract.destination_system_id,
        )
        if minimum_delivery_travel_seconds is not None:
            latest_arrival_seconds = min(
                latest_arrival_seconds,
                self.constraints.horizon_seconds
                - 2 * self.constraints.travel.service_seconds
                - minimum_delivery_travel_seconds,
                deadline_seconds
                - 2 * self.constraints.travel.service_seconds
                - minimum_delivery_travel_seconds,
            )
        return latest_arrival_seconds

    def _add_circuit(self) -> None:
        self.contract_is_selected = {
            contract.contract_id: self.model.new_bool_var(f"select_{contract.contract_id}")
            for contract in self.prepared.problem.contracts
        }
        self.arc_is_used: dict[tuple[int, int], cp_model.IntVar] = {}
        circuit_arc_definitions: list[tuple[int, int, cp_model.IntVar]] = []
        self.event_is_skipped: dict[int, cp_model.IntVar] = {}

        # AddCircuit expects a cycle. This always-on artificial arc turns the desired start-to-end
        # path into a cycle without representing real travel or consuming time.
        end_to_start_arc_is_used = self.model.new_bool_var("end_to_start")
        self.model.add(end_to_start_arc_is_used == 1)
        self.arc_is_used[(_END_NODE_ID, _START_NODE_ID)] = end_to_start_arc_is_used
        circuit_arc_definitions.append((_END_NODE_ID, _START_NODE_ID, end_to_start_arc_is_used))

        for event in self.events[2:]:
            if event.is_optional:
                assert event.contract_id is not None
                event_is_skipped = self.model.new_bool_var(f"skip_{event.label}")
                self.event_is_skipped[event.node_id] = event_is_skipped
                self.model.add(event_is_skipped + self.contract_is_selected[event.contract_id] == 1)
                circuit_arc_definitions.append((event.node_id, event.node_id, event_is_skipped))
            else:
                # Register unreachable mandatory events so AddCircuit proves infeasibility
                # instead of silently omitting them.
                mandatory_event_self_loop = self.model.new_bool_var(f"forbid_skip_{event.label}")
                self.event_is_skipped[event.node_id] = mandatory_event_self_loop
                self.model.add(mandatory_event_self_loop == 0)
                circuit_arc_definitions.append(
                    (event.node_id, event.node_id, mandatory_event_self_loop)
                )

        for source_event in self.events:
            if source_event.node_id == _END_NODE_ID:
                continue
            for destination_event in self.events:
                if (
                    destination_event.node_id == _START_NODE_ID
                    or destination_event.node_id == source_event.node_id
                ):
                    continue
                if (
                    (
                        source_event.node_id == _START_NODE_ID
                        and destination_event.node_id in self.pickup_by_delivery
                    )
                    or (
                        source_event.node_id in self.delivery_by_pickup
                        and destination_event.node_id == _END_NODE_ID
                    )
                    or self.pickup_by_delivery.get(source_event.node_id)
                    == destination_event.node_id
                ):
                    # Pickup precedence excludes these arcs from every feasible route.
                    continue
                travel_time_seconds = self._event_travel_seconds(
                    source_event,
                    destination_event,
                )
                if travel_time_seconds is None:
                    continue
                source_service_time_seconds = (
                    self.constraints.travel.service_seconds
                    if source_event.action_kind is not None
                    else 0
                )
                if (
                    self.earliest_arrivals[source_event.node_id]
                    + source_service_time_seconds
                    + travel_time_seconds
                    > self.latest_arrivals[destination_event.node_id]
                ):
                    # Even the impossible-to-beat direct lower bound cannot reach the destination's
                    # hard time window. Omitting this arc is therefore proof-preserving.
                    continue
                is_arc_used = self.model.new_bool_var(
                    f"arc_{source_event.node_id}_{destination_event.node_id}"
                )
                self.arc_is_used[(source_event.node_id, destination_event.node_id)] = is_arc_used
                circuit_arc_definitions.append(
                    (source_event.node_id, destination_event.node_id, is_arc_used)
                )
        self.model.add_circuit(circuit_arc_definitions)

    def _create_event_state(self) -> None:
        self.arrival = [
            self.model.new_int_var(0, self.constraints.horizon_seconds, f"arrival_{event.node_id}")
            for event in self.events
        ]
        self.order = [
            self.model.new_int_var(0, len(self.events), f"order_{event.node_id}")
            for event in self.events
        ]
        self.model.add(self.arrival[_START_NODE_ID] == 0)
        self.model.add(self.order[_START_NODE_ID] == 0)

        # Make route-implied event windows explicit so presolve/propagation need not rediscover them
        # through the circuit. Optional bounds apply only when the contract is selected.
        for contract in self.prepared.problem.contracts:
            contract_id = contract.contract_id
            is_contract_selected = self.contract_is_selected[contract_id]
            contract_event_node_ids = (
                self.catalog.optional_pickups[contract_id],
                self.catalog.optional_deliveries[contract_id],
            )
            for node_id in contract_event_node_ids:
                event = self.events[node_id]
                self.model.add(
                    self.arrival[node_id] >= self.earliest_arrivals[event.node_id]
                ).only_enforce_if(is_contract_selected)
                self.model.add(
                    self.arrival[node_id] <= self.latest_arrivals[event.node_id]
                ).only_enforce_if(is_contract_selected)
        for node_id in (
            *self.catalog.committed_pickups.values(),
            *self.catalog.committed_deliveries.values(),
            *self.catalog.waypoints,
        ):
            event = self.events[node_id]
            self.model.add(self.arrival[node_id] >= self.earliest_arrivals[event.node_id])
            self.model.add(self.arrival[node_id] <= self.latest_arrivals[event.node_id])

        cargo_capacity_units = self.constraints.cargo_capacity_units
        initial_cargo_load_units = self.prepared.problem.initial_cargo_units
        self.cargo = [
            self.model.new_int_var(0, cargo_capacity_units, f"cargo_{event.node_id}")
            for event in self.events
        ]
        self.model.add(self.cargo[_START_NODE_ID] == initial_cargo_load_units)
        self.model.add(self.cargo[_END_NODE_ID] == 0)

        self.parcels: list[cp_model.IntVar] | None = None
        if self.constraints.max_simultaneous_contracts is not None:
            max_active_parcels = self.constraints.max_simultaneous_contracts
            initial_active_parcel_count = self.prepared.problem.initial_parcel_count
            self.parcels = [
                self.model.new_int_var(0, max_active_parcels, f"parcels_{event.node_id}")
                for event in self.events
            ]
            self.model.add(self.parcels[_START_NODE_ID] == initial_active_parcel_count)
            self.model.add(self.parcels[_END_NODE_ID] == 0)

        self.collateral: list[cp_model.IntVar] | None = None
        initial_locked_collateral_units = self.prepared.problem.initial_collateral_units
        if self.constraints.collateral_mode is CollateralMode.ROLLING:
            self.collateral = [
                self.model.new_int_var(
                    0,
                    self.constraints.collateral_budget_units,
                    f"collateral_{event.node_id}",
                )
                for event in self.events
            ]
            self.model.add(self.collateral[_START_NODE_ID] == initial_locked_collateral_units)
            self.model.add(self.collateral[_END_NODE_ID] == 0)
        else:
            self.model.add(
                initial_locked_collateral_units
                + sum(
                    contract.collateral_units * self.contract_is_selected[contract.contract_id]
                    for contract in self.prepared.problem.contracts
                )
                <= self.constraints.collateral_budget_units
            )

        if self.constraints.travel.service_seconds > 0:
            # Every selected optional contract contributes exactly two serviced actions. This simple
            # route-independent inequality is redundant with exact time propagation but materially
            # strengthens the relaxation on dense candidate sets.
            self.model.add(
                self.constraints.travel.service_seconds
                * (
                    self.prepared.problem.mandatory_action_count
                    + 2 * sum(self.contract_is_selected.values())
                )
                <= self.constraints.horizon_seconds
            )

    def _propagate_route_resources(self) -> None:
        route_travel_time_terms: list[cp_model.LinearExpr] = []
        for (source_node_id, destination_node_id), is_arc_used in self.arc_is_used.items():
            if (source_node_id, destination_node_id) == (
                _END_NODE_ID,
                _START_NODE_ID,
            ):
                continue
            source_event = self.events[source_node_id]
            destination_event = self.events[destination_node_id]
            travel_time_seconds = self._event_travel_seconds(
                source_event,
                destination_event,
            )
            assert travel_time_seconds is not None
            if travel_time_seconds:
                route_travel_time_terms.append(travel_time_seconds * is_arc_used)
            source_service_time_seconds = (
                self.constraints.travel.service_seconds
                if source_event.action_kind is not None
                else 0
            )
            self.model.add(
                self.arrival[destination_node_id]
                == self.arrival[source_node_id] + source_service_time_seconds + travel_time_seconds
            ).only_enforce_if(is_arc_used)
            self.model.add(
                self.order[destination_node_id] == self.order[source_node_id] + 1
            ).only_enforce_if(is_arc_used)
            self.model.add(
                self.cargo[destination_node_id]
                == self.cargo[source_node_id] + destination_event.cargo_delta
            ).only_enforce_if(is_arc_used)
            if self.parcels is not None:
                self.model.add(
                    self.parcels[destination_node_id]
                    == self.parcels[source_node_id] + destination_event.parcel_delta
                ).only_enforce_if(is_arc_used)
            if self.collateral is not None:
                self.model.add(
                    self.collateral[destination_node_id]
                    == self.collateral[source_node_id] + destination_event.collateral_delta
                ).only_enforce_if(is_arc_used)

        # This redundant equality exposes the complete travel budget to the linear relaxation.
        self.model.add(
            self.arrival[_END_NODE_ID]
            == sum(route_travel_time_terms)
            + self.constraints.travel.service_seconds
            * (
                self.prepared.problem.mandatory_action_count
                + 2 * sum(self.contract_is_selected.values())
            )
        )

    def _add_contract_constraints(self) -> None:
        for contract in self.prepared.problem.contracts:
            is_contract_selected = self.contract_is_selected[contract.contract_id]
            pickup_node_id = self.catalog.optional_pickups[contract.contract_id]
            delivery_node_id = self.catalog.optional_deliveries[contract.contract_id]
            self.model.add(
                self.order[delivery_node_id] >= self.order[pickup_node_id] + 1
            ).only_enforce_if(is_contract_selected)
            minimum_delivery_travel_seconds = self._travel_seconds(
                contract.origin_system_id,
                contract.destination_system_id,
            )
            assert minimum_delivery_travel_seconds is not None
            # Interleaving cannot beat the shortest path. This redundant inequality
            # strengthens propagation beyond the conditional arc constraints.
            self.model.add(
                self.arrival[delivery_node_id]
                >= self.arrival[pickup_node_id]
                + self.constraints.travel.service_seconds
                + minimum_delivery_travel_seconds
            ).only_enforce_if(is_contract_selected)
            if self.constraints.collateral_mode is CollateralMode.LOCKED:
                self.model.add(
                    self.arrival[delivery_node_id] + self.constraints.travel.service_seconds
                    <= contract.days_to_complete * 86_400
                ).only_enforce_if(is_contract_selected)
            else:
                latest_pickup_second = contract.last_pickup_second(self.constraints.snapshot_time)
                self.model.add(
                    self.arrival[pickup_node_id] <= latest_pickup_second
                ).only_enforce_if(is_contract_selected)
                self.model.add(
                    self.arrival[delivery_node_id] + self.constraints.travel.service_seconds
                    <= self.arrival[pickup_node_id] + contract.days_to_complete * 86_400
                ).only_enforce_if(is_contract_selected)

        for shipment in self.prepared.problem.active_shipments:
            contract_id = shipment.contract.contract_id
            delivery_node_id = self.catalog.committed_deliveries[contract_id]
            if not shipment.picked:
                pickup_node_id = self.catalog.committed_pickups[contract_id]
                self.model.add(self.order[delivery_node_id] >= self.order[pickup_node_id] + 1)
                minimum_delivery_travel_seconds = self._travel_seconds(
                    shipment.contract.origin_system_id,
                    shipment.contract.destination_system_id,
                )
                if minimum_delivery_travel_seconds is not None:
                    self.model.add(
                        self.arrival[delivery_node_id]
                        >= self.arrival[pickup_node_id]
                        + self.constraints.travel.service_seconds
                        + minimum_delivery_travel_seconds
                    )
            delivery_deadline_seconds = shipment.last_delivery_second(
                self.constraints.snapshot_time
            )
            self.model.add(
                self.arrival[delivery_node_id] + self.constraints.travel.service_seconds
                <= delivery_deadline_seconds
            )

    def _maximize_reward(self) -> None:
        committed_reward_units = self.prepared.problem.committed_reward_units
        maximum_reward_units = committed_reward_units + sum(
            contract.reward_units for contract in self.prepared.problem.contracts
        )
        self.total_reward_units = self.model.new_int_var(
            committed_reward_units,
            maximum_reward_units,
            "total_reward",
        )
        self.model.add(
            self.total_reward_units
            == committed_reward_units
            + sum(
                contract.reward_units * self.contract_is_selected[contract.contract_id]
                for contract in self.prepared.problem.contracts
            )
        )
        add_resource_work_bounds(self.model, self.prepared, self.contract_is_selected)
        self.model.maximize(self.total_reward_units)
        suggested_contract_ids = frozenset(
            v.contract_id
            for v in build_greedy_route_hint(self.prepared)
            if isinstance(v, PlannedAction)
        )
        for contract_id, is_contract_selected in self.contract_is_selected.items():
            self.model.add_hint(
                is_contract_selected,
                int(contract_id in suggested_contract_ids),
            )

    def hint(
        self,
        visits: tuple[PlannedVisit, ...],
        selected_ids: tuple[int, ...],
    ) -> None:
        """Give CP-SAT a complete assignment, not just contract-selection suggestions."""
        action_nodes = {
            (e.action_kind, e.contract_id): e.node_id
            for e in self.events
            if e.action_kind is not None
        }
        waypoint_nodes = {e.system_id: e.node_id for e in self.events[2:] if e.action_kind is None}
        nodes = [self.start_node_id]
        for visit in visits:
            if isinstance(visit, PlannedAction):
                node = action_nodes[visit.action, visit.contract_id]
                waypoint = waypoint_nodes.get(self.events[node].system_id)
                if waypoint is not None and waypoint not in nodes:
                    nodes.append(waypoint)
            else:
                node = waypoint_nodes[visit.system_id]
            if node not in nodes:
                nodes.append(node)
        nodes.append(self.end_node_id)
        used_arcs = set(zip(nodes, nodes[1:], strict=False))
        used_arcs.add((self.end_node_id, self.start_node_id))
        if not used_arcs <= self.arc_is_used.keys():
            raise RuntimeError("verified incumbent uses an arc removed from the exact model")
        self.model.clear_hints()  # type: ignore[no-untyped-call]
        selected_set = set(selected_ids)
        for contract_id, variable in self.contract_is_selected.items():
            self.model.add_hint(variable, int(contract_id in selected_set))
        for node, variable in self.event_is_skipped.items():
            self.model.add_hint(variable, int(node not in nodes))
        for arc, variable in self.arc_is_used.items():
            self.model.add_hint(variable, int(arc in used_arcs))
        arrival, order, cargo, parcels, collateral = ([0] * len(self.events) for _ in range(5))
        problem = self.prepared.problem
        cargo[0] = problem.initial_cargo_units
        parcels[0] = problem.initial_parcel_count
        collateral[0] = problem.initial_collateral_units
        travel = problem.constraints.travel
        for previous, node in zip(nodes, nodes[1:], strict=False):
            source, target = self.events[previous], self.events[node]
            assert source.system_id is not None
            jumps = (
                0
                if target.system_id is None
                else self.prepared.jump_matrix[source.system_id, target.system_id]
            )
            arrival[node] = (
                arrival[previous]
                + jumps * travel.seconds_per_jump
                + (travel.service_seconds if source.action_kind is not None else 0)
            )
            order[node] = order[previous] + 1
            cargo[node] = cargo[previous] + target.cargo_delta
            parcels[node] = parcels[previous] + target.parcel_delta
            collateral[node] = collateral[previous] + target.collateral_delta
        for variables, values in (
            (self.arrival, arrival),
            (self.order, order),
            (self.cargo, cargo),
            (self.parcels, parcels),
            (self.collateral, collateral),
        ):
            if variables is not None:
                for variable, value in zip(variables, values, strict=True):
                    self.model.add_hint(variable, value)
        reward = sum(s.contract.reward_units for s in self.prepared.problem.active_shipments)
        reward += sum(
            item.reward_units
            for item in self.prepared.problem.contracts
            if item.contract_id in selected_set
        )
        self.model.add_hint(self.total_reward_units, reward)
        self.model.add(self.total_reward_units >= reward)

    def extract(
        self,
        solver: cp_model.CpSolver,
    ) -> tuple[tuple[PlannedVisit, ...], tuple[int, ...]]:
        selected_contract_ids = tuple(
            sorted(
                contract_id
                for contract_id, is_contract_selected in self.contract_is_selected.items()
                if solver.value(is_contract_selected)
            )
        )
        successor_node_by_node_id: dict[int, int] = {}
        for (source_node_id, destination_node_id), is_arc_used in self.arc_is_used.items():
            if solver.value(is_arc_used):
                successor_node_by_node_id[source_node_id] = destination_node_id
        visits: list[PlannedVisit] = []
        current_node_id = self.start_node_id
        visited_node_ids = {current_node_id}
        while True:
            next_node_id = successor_node_by_node_id.get(current_node_id)
            if next_node_id is None:
                raise RuntimeError(f"solver route has no successor for node {current_node_id}")
            if next_node_id == self.end_node_id:
                break
            if next_node_id in visited_node_ids:
                raise RuntimeError("solver route contains a cycle before the end node")
            visited_node_ids.add(next_node_id)
            event = self.events[next_node_id]
            if event.action_kind is None:
                if event.system_id is None or event.contract_id is not None:
                    raise RuntimeError("unexpected anonymous event inside solver route")
                visits.append(PlannedWaypoint(event.system_id))
            else:
                if event.contract_id is None:
                    raise RuntimeError("contract action is missing its contract ID")
                visits.append(PlannedAction(event.action_kind, event.contract_id))
            current_node_id = next_node_id
        return tuple(visits), selected_contract_ids
