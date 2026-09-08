from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.compare_results import compare
from benchmarks.run_empire import FIXTURE as EMPIRE_FIXTURE
from benchmarks.run_empire import load_empire_graph
from benchmarks.run_frozen import EXPECTED_REWARDS, run_benchmark_scenarios
from eve_courier_optimizer.domain import ProofStatus
from eve_courier_optimizer.eve.snapshot_file import read_snapshot


def test_frozen_dst_and_blockade_runner_targets_prove_global_optimality() -> None:
    results = run_benchmark_scenarios(time_limit_seconds=10.0)
    assert [result.name for result in results] == [
        "dst_highsec_10b_1h_gank_aware",
        "br_high_low_5b_1h_gank_aware",
    ]
    for result in results:
        assert result.status is ProofStatus.PROVEN_OPTIMAL
        assert result.scope_untruncated
        assert result.feasibility_verified
        assert result.eligible_contracts > 0
        assert result.selected_contracts > 0
        assert result.reward_isk == EXPECTED_REWARDS[result.name]


def test_realistic_empire_fixture_has_complete_declared_scope() -> None:
    graph = load_empire_graph()
    snapshot = read_snapshot(EMPIRE_FIXTURE)

    empire_regions = graph.empire_region_ids()
    assert snapshot.sde_build_number == graph.metadata.build_number == 3_458_726
    assert set(snapshot.region_ids) == empire_regions
    assert set(snapshot.threat_coverage_region_ids) == empire_regions
    assert snapshot.threat_incomplete_region_ids == ()
    assert len(snapshot.region_ids) == 24
    assert len(snapshot.contracts) == 421
    assert len(snapshot.gate_threat_events) == 250


@pytest.mark.parametrize("changed", [{"fingerprint": "different"}, {"config": {"workers": 8}}])
def test_benchmark_comparison_rejects_non_equivalent_runs(
    tmp_path: Path, changed: dict[str, object]
) -> None:
    row = {
        "case": "example",
        "seed": 17,
        "seconds": 0.5,
        "workers": 4,
        "fingerprint": "fixed",
        "config": {"workers": 4},
        "verified": True,
        "reward": 100,
        "bound": 120,
        "elapsed": 0.6,
        "status": "feasible_not_proven",
    }
    baseline, candidate = tmp_path / "baseline.jsonl", tmp_path / "candidate.jsonl"
    baseline.write_text(json.dumps(row) + "\n")
    candidate.write_text(json.dumps(row) + "\n")
    assert "100 → 100" in compare([baseline], [candidate])
    candidate.write_text(json.dumps({**row, **changed}) + "\n")
    with pytest.raises(ValueError, match="non-equivalent"):
        compare([baseline], [candidate])


@pytest.mark.parametrize(
    ("case", "seed", "reward_floor"),
    [("empire_dst_rolling", 71, 5_800_000_000), ("clustered_locked", 71, 13_844)],
)
def test_route_diversification_preserves_best_known_stress_rewards(
    case: str, seed: int, reward_floor: int
) -> None:
    from benchmarks.run_stress import prepare_case
    from eve_courier_optimizer.optimization.search.route_insertion import (
        construct_incumbent,
        diversify_incumbent,
    )

    graph, problem = prepare_case(case, seed=seed)
    initial = construct_incumbent(problem, graph)
    assert initial is not None
    improved = diversify_incumbent(
        problem, graph, initial, reward_ceiling=None, time_budget_seconds=10
    )
    assert improved.simulation.report.valid
    assert improved.quality >= initial.quality
    assert improved.simulation.total_reward_units >= reward_floor
