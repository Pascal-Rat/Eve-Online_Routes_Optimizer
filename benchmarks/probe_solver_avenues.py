"""Measure restricted exact neighborhoods and global duration refinement without changing defaults.

Run after installing the package: python -m benchmarks.probe_solver_avenues.
A restricted model's optimum/bound is local to its pool and is never a global reward certificate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from ortools.sat.python import cp_model

from eve_courier_optimizer import solver
from eve_courier_optimizer.bounds import build_selection_cuts, solve_system_relaxation
from eve_courier_optimizer.construction import improve_incumbent
from eve_courier_optimizer.domain import ActionKind, ActiveShipment, CollateralMode, TravelTimeModel
from eve_courier_optimizer.planning import PreparedProblem
from eve_courier_optimizer.sde import UniverseGraph
from eve_courier_optimizer.verification import PlannedAction, SimulationResult, simulate_and_verify

from .run_stress import prepare_case


def seed_route(p: PreparedProblem, g: UniverseGraph) -> tuple[tuple[int, ...], SimulationResult]:
    visits = solver._build_greedy_route_hint(p)
    ids = tuple(sorted({v.contract_id for v in visits if isinstance(v, PlannedAction)}))
    sim = simulate_and_verify(p.problem, g, visits, ids)
    empty = replace(p, problem=replace(p.problem, contracts=()), scores=())
    visits, sim = improve_incumbent(
        p, g, visits, sim, restart_visits=solver._build_greedy_route_hint(empty)
    )
    ids = tuple(sorted({v.contract_id for v in visits if isinstance(v, PlannedAction)}))
    assert sim.report.valid
    return ids, sim


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=53)
    parser.add_argument("--limits", type=float, nargs="+", default=(2, 10))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(solver.__file__).parent
    digest = hashlib.sha256()
    for source in sorted(root.rglob("*.py")):
        digest.update(str(source.relative_to(root)).encode() + b"\0")
        digest.update(source.read_bytes())
    metadata = dict(
        source_sha256=digest.hexdigest(),
        seed=args.seed,
        probe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    )

    def record(**row: object) -> None:
        encoded = json.dumps({**metadata, **row}, sort_keys=True)
        print(encoded, flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("a") as stream:
            stream.write(encoded + "\n")

    for name in ("empire_dst_2h", "clustered_rolling", "clustered_waypoints"):
        graph, prepared = prepare_case(name, seed=args.seed)
        selected, incumbent = seed_route(prepared, graph)
        unselected = sorted(
            (s for s in prepared.scores if s.contract.contract.contract_id not in selected),
            key=lambda s: (-s.reward_per_hour_isk, s.contract.contract.contract_id),
        )
        pool = tuple(
            sorted({*selected, *(s.contract.contract.contract_id for s in unselected[:12])})
        )
        reduced = solver._restrict_to_contract_selection(prepared, pool)
        for limit in args.limits:
            start = time.perf_counter()
            route = solver._build_model(reduced)
            route.model.add(route.total_reward_units >= incumbent.total_reward_units)
            solver._hint_verified_route(
                route, reduced, solver._simulation_visits(incumbent), selected
            )
            build_time = time.perf_counter() - start
            cp = solver._new_time_limited_solver(
                solver.SolverConfig(num_workers=4, random_seed=args.seed), limit
            )
            status = cp.solve(route.model)
            reward = incumbent.total_reward_units
            if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
                visits, ids = solver._extract_visits(route, cp)
                simulation = simulate_and_verify(prepared.problem, graph, visits, ids)
                assert simulation.report.valid
                reward = simulation.total_reward_units
            record(
                probe="restricted_reward",
                case=name,
                pool=len(pool),
                eligible=len(prepared.scores),
                initial_reward=incumbent.total_reward_units,
                reward=reward,
                seconds=limit,
                status=cp.status_name(status),
                build_seconds=build_time,
                elapsed=time.perf_counter() - start,
                cp_wall=cp.wall_time,
                variables=len(route.model.proto.variables),
                constraints=len(route.model.proto.constraints),
            )

    # Dense reward tie: one long job has the best individual reward/hour, while two shorter
    # jobs share travel and earn the same combined reward. Locked collateral admits either set.
    graph, original = prepare_case("corridor_capacity", seed=args.seed)
    base = original.problem.contracts[0]
    jobs = tuple(
        replace(
            base,
            contract=replace(
                base.contract,
                contract_id=i,
                origin_location_id=101,
                destination_location_id=105 if i == 1 else 103,
                reward_units=1000 if i == 1 else 500,
                collateral_units=100 if i == 1 else 50,
                volume_units=10,
            ),
            origin_system_id=1,
            destination_system_id=5 if i == 1 else 3,
        )
        for i in range(1, 22)
    )
    tie = replace(
        original,
        jump_matrix=graph.jump_matrix((1, 3, 5, 9), original.problem.constraints.security),
        problem=replace(
            original.problem,
            contracts=jobs,
            constraints=replace(original.problem.constraints, collateral_budget_units=100),
        ),
    )
    for global_search in (False, True):
        start = time.perf_counter()
        p = tie if global_search else solver._restrict_to_contract_selection(tie, (1,))
        route = solver._build_model(p)
        route.model.add(route.total_reward_units == 1000)
        route.model.minimize(route.finish_time_seconds)
        seed = (
            PlannedAction(ActionKind.PICKUP, 1),
            PlannedAction(ActionKind.DELIVERY, 1),
        )
        solver._hint_verified_route(route, p, seed, (1,))
        cp = solver._new_time_limited_solver(
            solver.SolverConfig(num_workers=4, random_seed=args.seed), 10
        )
        status = cp.solve(route.model)
        assert status in (cp_model.FEASIBLE, cp_model.OPTIMAL)
        visits, ids = solver._extract_visits(route, cp)
        sim = simulate_and_verify(tie.problem, graph, visits, ids)
        assert sim.report.valid and sim.total_reward_units == 1000
        record(
            probe="global_duration" if global_search else "fixed_duration",
            case="reward_tie",
            finish=sim.finish_seconds,
            reward=sim.total_reward_units,
            selected=ids,
            status=cp.status_name(status),
            elapsed=time.perf_counter() - start,
            cp_wall=cp.wall_time,
            variables=len(route.model.proto.variables),
        )

    # Mandatory accepted jobs can be routed cheaply in isolation, but insertion currently cannot
    # grow that seed: this probe measures the missing handoff separately from the full exact solve.
    graph, prepared = prepare_case("clustered_rolling", seed=args.seed)
    active_items = prepared.problem.contracts[:4]
    active = tuple(
        ActiveShipment(
            i,
            prepared.problem.constraints.snapshot_time + timedelta(seconds=500),
            picked=index % 2 == 0,
        )
        for index, i in enumerate(active_items)
    )
    prepared = replace(
        prepared,
        problem=replace(
            prepared.problem, contracts=prepared.problem.contracts[4:], active_shipments=active
        ),
        scores=tuple(s for s in prepared.scores if s.contract not in active_items),
    )
    start = time.perf_counter()
    oracle = solver._solve_reduced_exact_oracle(
        prepared,
        graph,
        (),
        solver.SolverConfig(num_workers=4, random_seed=args.seed),
        max_time_seconds=2,
    )
    record(
        probe="active_mandatory_seed",
        status=oracle.status_name,
        active=len(active),
        elapsed=time.perf_counter() - start,
        cp_wall=oracle.wall_time_seconds,
        verified=oracle.simulation is not None and oracle.simulation.report.valid,
    )

    # Isolate deadline-aware pair cuts from work bounds and primal effects.
    graph, original = prepare_case("corridor_capacity", seed=args.seed)
    jobs = tuple(
        replace(
            i,
            contract=replace(
                i.contract,
                volume_units=60,
                reward_units=100,
                days_to_complete=1,
                date_expired=original.problem.constraints.snapshot_time + timedelta(hours=1),
            ),
        )
        for i in original.problem.contracts[:20]
    )
    for mode in CollateralMode:
        p = replace(
            original,
            problem=replace(
                original.problem,
                contracts=jobs,
                constraints=replace(
                    original.problem.constraints,
                    horizon_seconds=180000,
                    collateral_mode=mode,
                    travel=TravelTimeModel(5000, 100),
                ),
            ),
        )
        start = time.perf_counter()
        unconstrained_dates = replace(
            p,
            problem=replace(
                p.problem,
                contracts=tuple(
                    replace(
                        i,
                        contract=replace(
                            i.contract,
                            days_to_complete=1000,
                            date_expired=p.problem.constraints.snapshot_time + timedelta(days=1000),
                        ),
                    )
                    for i in p.problem.contracts
                ),
            ),
        )
        old_cuts = build_selection_cuts(unconstrained_dates)
        old_bound = solve_system_relaxation(p, max_time_seconds=2, selection_cuts=old_cuts)
        cuts = build_selection_cuts(p)
        bound = solve_system_relaxation(p, max_time_seconds=2, selection_cuts=cuts)
        record(
            probe="deadline_master",
            mode=mode.value,
            pairs=len(cuts.pairs),
            old_pairs=len(old_cuts.pairs),
            old_bound=old_bound.upper_bound_units,
            bound=bound.upper_bound_units,
            status=bound.status_name,
            elapsed=time.perf_counter() - start,
        )


if __name__ == "__main__":
    main()
